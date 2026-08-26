"""yt-dlp engine: playlist diffing, parallel fetch, tagging, staged writes."""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from yt_dlp import YoutubeDL
from yt_dlp.postprocessor import PostProcessor
from yt_dlp.utils import DownloadError, ExtractorError

from .manifest import (
    STATUS_DUPLICATE,
    STATUS_OK,
    Entry,
    Manifest,
)
from .naming import safe_filename, target_name, title_key

# --------------------------------------------------------------------------
# Audio profiles
#
# Default is m4a: YouTube already serves AAC (~128 kbps, itag 140) alongside
# Opus, so asking for m4a means ffmpeg does a stream *copy* rather than a
# re-encode. That is the sweet spot the brief asked for — no generational
# quality loss, small files, and playback on essentially anything made in the
# last twenty years (iOS, Android, car heads, Windows, Sonos).
#
# opus is ~30% smaller again at equivalent quality but is not universally
# supported. mp3 is the compatibility floor and is the only profile that always
# re-encodes, so it is the only one that loses quality.
# --------------------------------------------------------------------------

AUDIO_PROFILES: Dict[str, dict] = {
    "m4a": {
        "ext": "m4a",
        "format": "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio/best",
        "codec": "m4a",
        "quality": "0",
        "transcodes": False,
    },
    "opus": {
        "ext": "opus",
        "format": "bestaudio[ext=webm][acodec=opus]/bestaudio[acodec=opus]/bestaudio/best",
        "codec": "opus",
        "quality": "0",
        "transcodes": False,
    },
    "mp3": {
        "ext": "mp3",
        "format": "bestaudio/best",
        "codec": "mp3",
        "quality": "0",  # V0 VBR — smaller than 320 CBR at transparent quality
        "transcodes": True,
    },
}

DEFAULT_PROFILE = "m4a"

# Errors that mean "this video will never work without extra auth" — recorded
# permanently (skipped from now on) so we never waste a request on it again.
# Age-restriction lives here, not in _BOTCHECK_MARKERS: it's a property of the
# one video, not of the session, so it must not abort the rest of the batch —
# only --cookies/--cookies-from-browser (and --retry-failed to revisit it)
# can ever get past it.
_PERMANENT_MARKERS = (
    "private video",
    "video unavailable",
    "this video is unavailable",
    "removed by the uploader",
    "account associated with this video has been terminated",
    "video has been removed",
    "video is not available",
    "deleted video",
    "no longer available",
    "unavailable in your country",
    "blocked it in your country",
    "sign in to confirm your age",
    "age-restricted",
)

# Errors that mean YouTube is challenging the *session* — retrying harder
# makes it worse, so the run aborts and tells the user to supply cookies.
# Keep this list scoped to session-level challenges only; a per-video gate
# (e.g. age-restriction) belongs in _PERMANENT_MARKERS instead, since it
# shouldn't stop the rest of the playlist from downloading.
_BOTCHECK_MARKERS = (
    "sign in to confirm you're not a bot",
    "confirm you're not a bot",
    "please sign in",
    "http error 429",
    "too many requests",
)


class BotCheckError(RuntimeError):
    """YouTube is asking us to authenticate. Abort rather than hammer it."""


@dataclass
class SyncOptions:
    dest: Path
    profile: str = DEFAULT_PROFILE
    workers: int = 3
    cookies: Optional[Path] = None
    cookies_from_browser: Optional[str] = None
    embed_metadata: bool = True
    embed_thumbnail: bool = True
    dedupe_by_title: bool = False
    retry_failed: bool = False
    limit: Optional[int] = None
    dry_run: bool = False
    rate_limit: Optional[str] = None
    fragments: int = 4
    sleep_interval: float = 0.0
    staging: Optional[Path] = None
    verbose: bool = False


@dataclass
class PlaylistItem:
    video_id: str
    title: str = ""
    uploader: str = ""
    duration: int = 0
    unavailable: bool = False


@dataclass
class SyncResult:
    playlist_id: str = ""
    playlist_title: str = ""
    total_in_playlist: int = 0
    already_present: int = 0
    downloaded: int = 0
    skipped_duplicate: int = 0
    failed: int = 0
    unavailable: int = 0
    bytes_added: int = 0
    errors: List[Tuple[str, str]] = field(default_factory=list)
    aborted: bool = False


# --------------------------------------------------------------------------
# Playlist enumeration
# --------------------------------------------------------------------------

def _base_opts(opts: SyncOptions) -> dict:
    ydl: dict = {
        "quiet": not opts.verbose,
        "no_warnings": not opts.verbose,
        "noprogress": True,
        "consoletitle": False,
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "socket_timeout": 30,
        "noplaylist": True,
        "ignoreerrors": False,
    }
    if opts.cookies:
        ydl["cookiefile"] = str(opts.cookies)
    if opts.cookies_from_browser:
        ydl["cookiesfrombrowser"] = (opts.cookies_from_browser,)
    if opts.rate_limit:
        ydl["ratelimit"] = _parse_rate(opts.rate_limit)
    return ydl


def _parse_rate(value: str) -> Optional[int]:
    value = value.strip().upper().rstrip("B")
    mult = 1
    if value.endswith("K"):
        mult, value = 1024, value[:-1]
    elif value.endswith("M"):
        mult, value = 1024 * 1024, value[:-1]
    elif value.endswith("G"):
        mult, value = 1024 * 1024 * 1024, value[:-1]
    try:
        return int(float(value) * mult)
    except ValueError:
        return None


def list_playlist(url: str, opts: SyncOptions) -> Tuple[str, str, List[PlaylistItem]]:
    """Enumerate a playlist without touching each video.

    ``extract_flat`` pulls the whole playlist in a couple of requests instead of
    running the full extractor per entry. On a 200-track playlist where nothing
    has changed, this turns a re-sync from ~200 requests into ~2 — which is both
    the speed win and the main reason repeated runs don't trip rate limiting.
    """
    ydl_opts = _base_opts(opts)
    ydl_opts.update({
        "extract_flat": "in_playlist",
        "skip_download": True,
        "noplaylist": False,
        "ignoreerrors": True,
    })
    if opts.limit:
        ydl_opts["playlistend"] = opts.limit

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as exc:
        _raise_if_botcheck(str(exc))
        raise

    if not info:
        raise RuntimeError(f"Could not read playlist: {url}")

    playlist_id = info.get("id") or ""
    playlist_title = info.get("title") or playlist_id

    items: List[PlaylistItem] = []
    seen = set()
    for entry in info.get("entries") or []:
        if not entry:
            continue
        vid = entry.get("id")
        if not vid or vid in seen:
            continue  # playlists can legitimately list the same video twice
        seen.add(vid)
        title = entry.get("title") or ""
        items.append(PlaylistItem(
            video_id=vid,
            title=title,
            uploader=entry.get("channel") or entry.get("uploader") or "",
            duration=int(entry.get("duration") or 0),
            unavailable=title in ("[Private video]", "[Deleted video]", "[Unavailable video]"),
        ))
    return playlist_id, playlist_title, items


def list_channel(url: str, opts: SyncOptions) -> Tuple[str, str, List[Tuple[str, str, str]]]:
    """Enumerate every playlist on a channel's /playlists page.

    Returns ``(channel_id, channel_name, playlists)`` where each playlist is
    ``(playlist_id, playlist_title, playlist_url)``. Point ``url`` at the
    channel's playlists tab (e.g. ``.../@handle/playlists``) — a bare channel
    URL lists uploaded videos instead, which this does not handle.
    """
    ydl_opts = _base_opts(opts)
    ydl_opts.update({
        "extract_flat": "in_playlist",
        "skip_download": True,
        "noplaylist": False,
        "ignoreerrors": True,
    })

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as exc:
        _raise_if_botcheck(str(exc))
        raise

    if not info:
        raise RuntimeError(f"Could not read channel: {url}")

    channel_id = info.get("channel_id") or info.get("id") or ""
    channel_name = info.get("channel") or info.get("uploader") or info.get("title") or channel_id

    playlists: List[Tuple[str, str, str]] = []
    for entry in info.get("entries") or []:
        if not entry:
            continue
        playlist_url = entry.get("url")
        playlist_id = entry.get("id") or ""
        # A channel's uploads/shorts/live tabs list individual 11-char video
        # IDs, not playlists — skip anything that isn't actually a playlist.
        if not playlist_url or len(playlist_id) <= 11:
            continue
        playlists.append((playlist_id, entry.get("title") or playlist_id, playlist_url))
    return channel_id, channel_name, playlists


def _raise_if_botcheck(message: str) -> None:
    low = message.lower()
    if any(marker in low for marker in _BOTCHECK_MARKERS):
        raise BotCheckError(message)


def _is_permanent(message: str) -> bool:
    low = message.lower()
    return any(marker in low for marker in _PERMANENT_MARKERS)


# --------------------------------------------------------------------------
# Single-video download
# --------------------------------------------------------------------------

def _download_opts(opts: SyncOptions, staging: Path) -> dict:
    profile = AUDIO_PROFILES[opts.profile]
    ydl_opts = _base_opts(opts)

    postprocessors: List[dict] = [{
        "key": "FFmpegExtractAudio",
        "preferredcodec": profile["codec"],
        "preferredquality": profile["quality"],
        "nopostoverwrites": False,
    }]
    if opts.embed_metadata:
        postprocessors.append({
            "key": "FFmpegMetadata",
            "add_metadata": True,
            "add_chapters": True,
        })
    if opts.embed_thumbnail:
        # YouTube serves webp; m4a/mp3 cover art needs jpeg.
        postprocessors.insert(0, {
            "key": "FFmpegThumbnailsConvertor",
            "format": "jpg",
            "when": "before_dl",
        })
        postprocessors.append({
            "key": "EmbedThumbnail",
            "already_have_thumbnail": False,
        })

    ydl_opts.update({
        "format": profile["format"],
        "outtmpl": {"default": str(staging / "%(id)s.%(ext)s")},
        "paths": {"home": str(staging)},
        "writethumbnail": opts.embed_thumbnail,
        "postprocessors": postprocessors,
        # Split each stream into parallel chunks — the single biggest throughput
        # win on a fat pipe like a Colab VM.
        "concurrent_fragment_downloads": max(1, opts.fragments),
        "continuedl": True,
        "overwrites": True,
        "noplaylist": True,
        "ignoreerrors": False,
    })
    if opts.sleep_interval:
        ydl_opts["sleep_interval"] = opts.sleep_interval
        ydl_opts["max_sleep_interval"] = opts.sleep_interval * 2
    return ydl_opts


def _resolve_output(info: dict, staging: Path, video_id: str, ext: str) -> Optional[Path]:
    """Find what yt-dlp actually produced, whatever the postprocessors renamed."""
    for download in info.get("requested_downloads") or []:
        for key in ("filepath", "_filename", "filename"):
            candidate = download.get(key)
            if candidate and Path(candidate).exists():
                return Path(candidate)

    exact = staging / f"{video_id}.{ext}"
    if exact.exists():
        return exact

    matches = [
        p for p in staging.glob(f"{video_id}.*")
        if p.is_file() and p.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".part", ".ytdl"}
    ]
    if matches:
        return max(matches, key=lambda p: p.stat().st_size)
    return None


class _AlbumTagger(PostProcessor):
    """Stamps every download with Album = the destination folder's name.

    Players group audio by Artist/Album in their library view rather than by
    folder, so without this, tracks from different synced playlists blur
    together. Setting Album lets each library show up as its own group in
    any tag-aware player, with no separate playlist file to import.
    """

    def __init__(self, album: str):
        super().__init__()
        self._album = album

    def run(self, info):
        info["album"] = self._album
        return [], info


def download_one(item: PlaylistItem, opts: SyncOptions, staging_root: Path) -> Entry:
    """Fetch one video into the destination. Raises on failure."""
    profile = AUDIO_PROFILES[opts.profile]
    ext = profile["ext"]

    # Each job gets its own staging directory so parallel workers can never
    # collide over yt-dlp's temp files.
    staging = Path(tempfile.mkdtemp(prefix=f"{item.video_id}-", dir=str(staging_root)))
    try:
        ydl_opts = _download_opts(opts, staging)
        url = f"https://www.youtube.com/watch?v={item.video_id}"
        with YoutubeDL(ydl_opts) as ydl:
            if opts.embed_metadata:
                ydl.add_post_processor(_AlbumTagger(opts.dest.name), when="pre_process")
            info = ydl.extract_info(url, download=True)
        if not info:
            raise RuntimeError("extractor returned no info")

        produced = _resolve_output(info, staging, item.video_id, ext)
        if produced is None:
            raise RuntimeError("download produced no audio file")

        title = info.get("title") or item.title or item.video_id
        uploader = info.get("uploader") or info.get("channel") or item.uploader or ""
        final_name = target_name(title, item.video_id, produced.suffix)
        destination = opts.dest / final_name

        # Move (not copy) into place, and only once the file is complete and
        # tagged. On a Google Drive FUSE mount this matters: writing directly to
        # the mount during download is slow and leaves half-files behind if the
        # session drops. Staging locally means Drive only ever sees finished
        # audio, and a killed run leaves the library clean.
        opts.dest.mkdir(parents=True, exist_ok=True)
        _safe_move(produced, destination)

        size = destination.stat().st_size
        return Entry(
            video_id=item.video_id,
            title=title,
            uploader=uploader,
            filename=str(destination.relative_to(opts.dest)),
            filesize=size,
            duration=int(info.get("duration") or item.duration or 0),
            status=STATUS_OK,
            title_key=title_key(title, uploader),
            downloaded_at=time.time(),
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _safe_move(src: Path, dst: Path) -> None:
    """Move across filesystems, replacing any partial file already at dst."""
    tmp = dst.with_name(dst.name + ".incoming")
    try:
        if tmp.exists():
            tmp.unlink()
    except OSError:
        pass
    shutil.move(str(src), str(tmp))
    os.replace(str(tmp), str(dst))


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def sync_playlist(
    url: str,
    opts: SyncOptions,
    manifest: Manifest,
    on_event: Optional[Callable[[str, dict], None]] = None,
) -> SyncResult:
    """Diff a playlist against the manifest and fetch only what's new."""
    emit = on_event or (lambda kind, data: None)

    playlist_id, playlist_title, items = list_playlist(url, opts)
    emit("playlist", {"id": playlist_id, "title": playlist_title, "count": len(items)})

    result = _sync_items(items, playlist_id, opts, manifest, emit)
    result.playlist_id = playlist_id
    result.playlist_title = playlist_title
    result.total_in_playlist = len(items)
    return result


def sync_channel(
    url: str,
    opts: SyncOptions,
    manifest: Manifest,
    on_event: Optional[Callable[[str, dict], None]] = None,
) -> SyncResult:
    """Merge every playlist on a channel into ``opts.dest``, deduped by video ID.

    A video that appears in several of the channel's playlists is only ever
    queued once — first-seen order across the channel's playlist listing wins.
    """
    emit = on_event or (lambda kind, data: None)

    channel_id, channel_name, playlists = list_channel(url, opts)

    merged: List[PlaylistItem] = []
    seen_ids: set = set()
    for playlist_id, playlist_title, playlist_url in playlists:
        try:
            _, _, items = list_playlist(playlist_url, opts)
        except BotCheckError:
            raise
        except Exception as exc:  # noqa: BLE001 — one broken playlist shouldn't sink the channel
            emit("channel_playlist_skipped", {
                "id": playlist_id, "title": playlist_title, "error": str(exc),
            })
            continue
        for item in items:
            if item.video_id in seen_ids:
                continue
            seen_ids.add(item.video_id)
            merged.append(item)

    emit("channel", {
        "id": channel_id, "name": channel_name,
        "playlists": len(playlists), "videos": len(merged),
    })

    result = _sync_items(merged, channel_id, opts, manifest, emit)
    result.playlist_id = channel_id
    result.playlist_title = channel_name
    result.total_in_playlist = len(merged)
    return result


def _sync_items(
    items: List[PlaylistItem],
    playlist_id: str,
    opts: SyncOptions,
    manifest: Manifest,
    emit: Callable[[str, dict], None],
) -> SyncResult:
    """Diff a flat list of items against the manifest and fetch only what's new."""
    result = SyncResult()

    known_titles = manifest.title_keys() if opts.dedupe_by_title else {}
    pending: List[PlaylistItem] = []
    queued_ids: set = set()

    for item in items:
        if item.video_id in queued_ids:
            continue  # the same video listed twice in one playlist
        if manifest.is_done(item.video_id, retry_failed=opts.retry_failed):
            result.already_present += 1
            continue
        if item.unavailable:
            manifest.record_failure(
                item.video_id, f"listed as {item.title}", permanent=True,
                title=item.title, playlist_id=playlist_id,
            )
            result.unavailable += 1
            emit("unavailable", {"id": item.video_id, "title": item.title})
            continue
        if opts.dedupe_by_title:
            key = title_key(item.title, item.uploader)
            if key and key in known_titles:
                original = known_titles[key]
                manifest.put(Entry(
                    video_id=item.video_id, title=item.title, uploader=item.uploader,
                    status=STATUS_DUPLICATE, title_key=key, duplicate_of=original,
                    playlist_id=playlist_id,
                ))
                result.skipped_duplicate += 1
                emit("duplicate", {"id": item.video_id, "title": item.title, "of": original})
                continue
            if key:
                known_titles[key] = item.video_id
        pending.append(item)
        queued_ids.add(item.video_id)

    emit("plan", {
        "pending": len(pending),
        "present": result.already_present,
        "duplicates": result.skipped_duplicate,
    })

    if opts.dry_run or not pending:
        return result

    staging_root = Path(opts.staging) if opts.staging else Path(tempfile.mkdtemp(prefix="yt2drive-"))
    staging_root.mkdir(parents=True, exist_ok=True)
    owns_staging = opts.staging is None

    abort = threading.Event()
    lock = threading.Lock()

    def worker(item: PlaylistItem) -> None:
        if abort.is_set():
            return
        try:
            entry = download_one(item, opts, staging_root)
            entry.playlist_id = playlist_id
            manifest.put(entry)
            with lock:
                result.downloaded += 1
                result.bytes_added += entry.filesize
            emit("downloaded", {
                "id": item.video_id, "title": entry.title,
                "bytes": entry.filesize, "file": entry.filename,
            })
        except BotCheckError as exc:
            abort.set()
            with lock:
                result.aborted = True
                result.errors.append((item.video_id, str(exc)))
            emit("botcheck", {"id": item.video_id, "error": str(exc)})
        except (DownloadError, ExtractorError, OSError, RuntimeError) as exc:
            message = str(exc)
            try:
                _raise_if_botcheck(message)
            except BotCheckError:
                abort.set()
                with lock:
                    result.aborted = True
                    result.errors.append((item.video_id, message))
                emit("botcheck", {"id": item.video_id, "error": message})
                return
            permanent = _is_permanent(message)
            manifest.record_failure(
                item.video_id, message, permanent=permanent,
                title=item.title, playlist_id=playlist_id,
            )
            with lock:
                if permanent:
                    result.unavailable += 1
                else:
                    result.failed += 1
                result.errors.append((item.video_id, message))
            emit("failed", {
                "id": item.video_id, "title": item.title,
                "error": message, "permanent": permanent,
            })

    try:
        workers = max(1, min(opts.workers, len(pending)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(worker, item) for item in pending]
            for future in as_completed(futures):
                future.result()
    finally:
        manifest.save(force=True)
        if owns_staging:
            shutil.rmtree(staging_root, ignore_errors=True)

    return result
