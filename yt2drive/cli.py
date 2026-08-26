"""Command line interface for yt2drive."""

from __future__ import annotations

import argparse
import dataclasses
import shutil
import sys
import threading
from pathlib import Path
from typing import Dict, List, Optional

from . import __version__
from .downloader import (
    AUDIO_PROFILES,
    DEFAULT_PROFILE,
    BotCheckError,
    SyncOptions,
    list_channel,
    list_playlist,
    sync_channel,
    sync_playlist,
)
from .manifest import (
    STATUS_DUPLICATE,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    Manifest,
)
from .naming import safe_filename

BOTCHECK_HELP = """
YouTube challenged this session ("confirm you're not a bot"). This is normal
from datacenter IPs (Colab, cloud VMs, CI runners) and is fixed by giving
yt2drive a logged-in session:

  1. Install a "Get cookies.txt LOCALLY" browser extension.
  2. Open youtube.com while signed in, export cookies.txt.
  3. Re-run with:  --cookies /path/to/cookies.txt

Running from your own machine, --cookies-from-browser chrome does it directly.
Lowering --workers to 1 and adding --sleep 2 also helps.
"""


def write_m3u(dest: Path, manifest: Manifest) -> None:
    """Write ``<dest>/<dest folder name>.m3u8`` — a standalone playlist file
    listing every track in this library, in download order. VLC (and most
    players) treat an .m3u as its own playlist regardless of tag metadata,
    which is what makes each library show up as a separate playlist rather
    than getting merged by Artist/Album grouping.
    """
    tracks = [e for e in manifest if e.status == STATUS_OK and e.filename]
    if not tracks:
        return
    tracks.sort(key=lambda e: e.downloaded_at)
    lines = ["#EXTM3U"]
    for e in tracks:
        duration = int(e.duration) if e.duration else -1
        title = f"{e.uploader} - {e.title}" if e.uploader else e.title
        lines.append(f"#EXTINF:{duration},{title}")
        lines.append(e.filename)
    (dest / f"{dest.name}.m3u8").write_text("\n".join(lines) + "\n", encoding="utf-8")


def human_bytes(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024.0:
            return f"{size:,.1f} {unit}" if unit != "B" else f"{size:,.0f} B"
        size /= 1024.0
    return f"{size:,.1f} PB"


class Reporter:
    """Prints progress as the sync proceeds. Thread-safe."""

    def __init__(self, quiet: bool = False):
        self.quiet = quiet
        self.lock = threading.Lock()
        self.done = 0
        self.pending = 0

    def __call__(self, kind: str, data: dict) -> None:
        if self.quiet and kind not in ("botcheck", "failed"):
            return
        with self.lock:
            if kind == "playlist":
                print(f"\nPlaylist: {data['title']}  ({data['count']} videos)")
            elif kind == "channel":
                print(
                    f"\nChannel: {data['name']}  "
                    f"({data['playlists']} playlist(s), {data['videos']} unique video(s))"
                )
            elif kind == "channel_playlist_skipped":
                first_line = data["error"].strip().splitlines()[0][:80]
                print(f"  (skipped playlist {data['title'][:50]}: {first_line})", file=sys.stderr)
            elif kind == "plan":
                self.pending = data["pending"]
                print(
                    f"  already have: {data['present']}"
                    f"   duplicates skipped: {data['duplicates']}"
                    f"   to download: {data['pending']}"
                )
                if data["pending"]:
                    print()
            elif kind == "downloaded":
                self.done += 1
                counter = f"[{self.done}/{self.pending}]"
                print(f"  {counter:>10} ok   {data['title'][:70]}  ({human_bytes(data['bytes'])})")
            elif kind == "duplicate":
                print(f"             dup  {data['title'][:70]}  (same as {data['of']})")
            elif kind == "unavailable":
                print(f"             --   {data['id']}  {data['title'][:60]}")
            elif kind == "failed":
                self.done += 1
                tag = "gone" if data["permanent"] else "fail"
                first_line = data["error"].strip().splitlines()[0][:90]
                print(f"             {tag} {data['title'][:60]}  {first_line}", file=sys.stderr)
            elif kind == "botcheck":
                print("\n!! aborting: YouTube bot check", file=sys.stderr)


# --------------------------------------------------------------------------


def cmd_sync(args: argparse.Namespace) -> int:
    base_dest = Path(args.dest).expanduser().resolve()
    base_dest.mkdir(parents=True, exist_ok=True)

    if shutil.which("ffmpeg") is None:
        print(
            "error: ffmpeg not found on PATH. It is required to extract and tag audio.\n"
            "  Debian/Ubuntu/Colab:  apt-get install -y ffmpeg\n"
            "  macOS:                brew install ffmpeg\n"
            "  Windows:              winget install Gyan.FFmpeg",
            file=sys.stderr,
        )
        return 2

    base_opts = SyncOptions(
        dest=base_dest,
        profile=args.format,
        workers=args.workers,
        cookies=Path(args.cookies).expanduser() if args.cookies else None,
        cookies_from_browser=args.cookies_from_browser,
        embed_metadata=not args.no_metadata,
        embed_thumbnail=not args.no_thumbnail,
        dedupe_by_title=args.dedupe_by_title,
        retry_failed=args.retry_failed,
        limit=args.limit,
        dry_run=args.dry_run,
        rate_limit=args.rate_limit,
        fragments=args.fragments,
        sleep_interval=args.sleep,
        staging=Path(args.staging).expanduser() if args.staging else None,
        verbose=args.verbose,
    )

    reporter = Reporter(quiet=args.quiet)
    totals = {"downloaded": 0, "bytes": 0, "failed": 0, "present": 0, "dupes": 0}
    libraries: List[Path] = []
    library_manifests: Dict[Path, Manifest] = {}
    exit_code = 0

    # Without --auto-folder, every playlist shares one destination/manifest so
    # tracks common to several playlists are only ever fetched once.
    shared_manifest = None if args.auto_folder else Manifest(base_dest).load()

    for url in args.urls:
        if args.auto_folder:
            try:
                _, playlist_title, _ = list_playlist(url, base_opts)
            except BotCheckError:
                print(BOTCHECK_HELP, file=sys.stderr)
                return 3
            except Exception as exc:  # noqa: BLE001 — surface any extractor failure cleanly
                print(f"error reading {url}: {exc}", file=sys.stderr)
                exit_code = 1
                continue
            dest = base_dest / safe_filename(playlist_title, fallback="playlist")
        else:
            dest = base_dest
        dest.mkdir(parents=True, exist_ok=True)
        opts = dataclasses.replace(base_opts, dest=dest)

        manifest = shared_manifest or Manifest(dest).load()
        if args.reconcile:
            stats = manifest.reconcile()
            if any(stats.values()):
                print(
                    f"reconciled: adopted {stats['adopted']} existing file(s), "
                    f"{stats['missing']} missing, {stats['resized']} resized"
                )

        try:
            result = sync_playlist(url, opts, manifest, on_event=reporter)
        except BotCheckError:
            print(BOTCHECK_HELP, file=sys.stderr)
            return 3
        except Exception as exc:  # noqa: BLE001 — surface any extractor failure cleanly
            print(f"error reading {url}: {exc}", file=sys.stderr)
            exit_code = 1
            continue
        finally:
            manifest.save(force=True)

        if dest not in libraries:
            libraries.append(dest)
        library_manifests[dest] = manifest

        totals["downloaded"] += result.downloaded
        totals["bytes"] += result.bytes_added
        totals["failed"] += result.failed
        totals["present"] += result.already_present
        totals["dupes"] += result.skipped_duplicate

        if result.aborted:
            print(BOTCHECK_HELP, file=sys.stderr)
            exit_code = 3
            break
        if result.failed:
            exit_code = max(exit_code, 1)

    if args.dry_run:
        print("\n(dry run — nothing downloaded)")
    else:
        for lib in libraries:
            write_m3u(lib, library_manifests[lib])
        print(
            f"\nDone. {totals['downloaded']} new "
            f"({human_bytes(totals['bytes'])}), "
            f"{totals['present']} already present, "
            f"{totals['dupes']} duplicates, "
            f"{totals['failed']} failed."
        )
        if len(libraries) > 1:
            print("Libraries:")
            for lib in libraries:
                print(f"  {lib}")
        else:
            print(f"Library: {libraries[0] if libraries else base_dest}")
        if totals["failed"]:
            print("Re-run with --retry-failed to retry the failures.")
    return exit_code


def cmd_channel(args: argparse.Namespace) -> int:
    base_dest = Path(args.dest).expanduser().resolve()
    base_dest.mkdir(parents=True, exist_ok=True)

    if shutil.which("ffmpeg") is None:
        print(
            "error: ffmpeg not found on PATH. It is required to extract and tag audio.\n"
            "  Debian/Ubuntu/Colab:  apt-get install -y ffmpeg\n"
            "  macOS:                brew install ffmpeg\n"
            "  Windows:              winget install Gyan.FFmpeg",
            file=sys.stderr,
        )
        return 2

    opts = SyncOptions(
        dest=base_dest,  # placeholder; replaced once the channel name is known
        profile=args.format,
        workers=args.workers,
        cookies=Path(args.cookies).expanduser() if args.cookies else None,
        cookies_from_browser=args.cookies_from_browser,
        embed_metadata=not args.no_metadata,
        embed_thumbnail=not args.no_thumbnail,
        dedupe_by_title=args.dedupe_by_title,
        retry_failed=args.retry_failed,
        dry_run=args.dry_run,
        rate_limit=args.rate_limit,
        fragments=args.fragments,
        sleep_interval=args.sleep,
        staging=Path(args.staging).expanduser() if args.staging else None,
        verbose=args.verbose,
    )

    try:
        _, channel_name, playlists = list_channel(args.url, opts)
    except BotCheckError:
        print(BOTCHECK_HELP, file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001 — surface any extractor failure cleanly
        print(f"error reading channel: {exc}", file=sys.stderr)
        return 1

    if not playlists:
        print(
            "error: no playlists found at that URL. Point --url at the channel's "
            "playlists tab, e.g. https://www.youtube.com/@handle/playlists",
            file=sys.stderr,
        )
        return 1

    dest = base_dest / safe_filename(channel_name, fallback="channel")
    dest.mkdir(parents=True, exist_ok=True)
    opts = dataclasses.replace(opts, dest=dest)

    manifest = Manifest(dest).load()
    if args.reconcile:
        stats = manifest.reconcile()
        if any(stats.values()):
            print(
                f"reconciled: adopted {stats['adopted']} existing file(s), "
                f"{stats['missing']} missing, {stats['resized']} resized"
            )

    reporter = Reporter(quiet=args.quiet)
    try:
        result = sync_channel(args.url, opts, manifest, on_event=reporter)
    except BotCheckError:
        print(BOTCHECK_HELP, file=sys.stderr)
        return 3
    finally:
        manifest.save(force=True)

    if args.dry_run:
        print("\n(dry run — nothing downloaded)")
    else:
        write_m3u(dest, manifest)
        print(
            f"\nDone. {result.downloaded} new "
            f"({human_bytes(result.bytes_added)}), "
            f"{result.already_present} already present, "
            f"{result.skipped_duplicate} duplicates, "
            f"{result.failed} failed."
        )
        print(f"Library: {dest}")
        if result.failed:
            print("Re-run with --retry-failed to retry the failures.")

    if result.aborted:
        print(BOTCHECK_HELP, file=sys.stderr)
        return 3
    return 1 if result.failed else 0


def cmd_status(args: argparse.Namespace) -> int:
    dest = Path(args.dest).expanduser().resolve()
    manifest = Manifest(dest).load()
    counts = manifest.counts()

    if not len(manifest):
        print(f"No manifest at {dest}. Run 'yt2drive sync' first, or 'yt2drive verify' to adopt existing files.")
        return 0

    print(f"Library: {dest}")
    print(f"  tracks:      {counts.get(STATUS_OK, 0)}")
    print(f"  size:        {human_bytes(manifest.total_bytes())}")
    if counts.get(STATUS_DUPLICATE):
        print(f"  duplicates:  {counts[STATUS_DUPLICATE]}")
    if counts.get(STATUS_FAILED):
        print(f"  failed:      {counts[STATUS_FAILED]}  (retry with --retry-failed)")
    if counts.get(STATUS_UNAVAILABLE):
        print(f"  unavailable: {counts[STATUS_UNAVAILABLE]}  (private/deleted/blocked)")

    if args.failures:
        print()
        for entry in manifest:
            if entry.status in (STATUS_FAILED, STATUS_UNAVAILABLE):
                first_line = entry.error.strip().splitlines()[0] if entry.error else ""
                print(f"  {entry.video_id}  {entry.title[:55]:<55} {first_line[:70]}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    dest = Path(args.dest).expanduser().resolve()
    manifest = Manifest(dest).load()
    stats = manifest.reconcile()
    print(f"Library: {dest}")
    print(f"  adopted existing files: {stats['adopted']}")
    print(f"  missing from disk:      {stats['missing']}  (will re-download on next sync)")
    print(f"  size corrections:       {stats['resized']}")
    print(f"  tracks tracked:         {manifest.counts().get(STATUS_OK, 0)}")
    return 0


# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yt2drive",
        description="Sync YouTube playlists to a folder as tagged audio, skipping anything already there.",
    )
    parser.add_argument("--version", action="version", version=f"yt2drive {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    # --- sync
    p = sub.add_parser("sync", help="download everything new from one or more playlists")
    p.add_argument("urls", nargs="+", metavar="URL", help="playlist or video URL(s)")
    p.add_argument("-d", "--dest", required=True, help="destination folder (e.g. a mounted Drive folder)")
    p.add_argument(
        "--auto-folder", action="store_true",
        help="create a subfolder named after each playlist inside --dest, instead of writing straight into it",
    )
    p.add_argument(
        "-f", "--format", choices=sorted(AUDIO_PROFILES), default=DEFAULT_PROFILE,
        help=f"audio profile (default: {DEFAULT_PROFILE} — native AAC, no re-encode)",
    )
    p.add_argument("-w", "--workers", type=int, default=3, help="videos downloaded in parallel (default: 3)")
    p.add_argument("--fragments", type=int, default=4, help="parallel chunks per video (default: 4)")
    p.add_argument("--cookies", help="path to a cookies.txt export (fixes bot checks)")
    p.add_argument("--cookies-from-browser", metavar="BROWSER", help="pull cookies live, e.g. chrome, firefox, edge")
    p.add_argument("--dedupe-by-title", action="store_true", help="also skip the same track re-uploaded under a different ID")
    p.add_argument("--retry-failed", action="store_true",
                   help="also retry entries marked unavailable, and failures that hit the attempt cap")
    p.add_argument("--no-metadata", action="store_true", help="do not write title/artist/album tags")
    p.add_argument("--no-thumbnail", action="store_true", help="do not embed cover art")
    p.add_argument("--limit", type=int, help="only consider the first N playlist entries")
    p.add_argument("--rate-limit", metavar="RATE", help="cap download speed, e.g. 2M")
    p.add_argument("--sleep", type=float, default=0.0, metavar="SECS", help="pause between videos to stay under rate limits")
    p.add_argument("--staging", help="scratch dir for in-progress downloads (default: system temp)")
    p.add_argument("--no-reconcile", dest="reconcile", action="store_false", help="skip the pre-run filesystem scan")
    p.add_argument("-n", "--dry-run", action="store_true", help="show what would be downloaded, then stop")
    p.add_argument("-q", "--quiet", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true", help="show raw yt-dlp output")
    p.set_defaults(func=cmd_sync, reconcile=True)

    # --- channel
    p = sub.add_parser(
        "channel",
        help="merge every playlist on a channel into one deduped folder named after the channel",
    )
    p.add_argument("url", metavar="URL", help="channel's playlists page, e.g. https://www.youtube.com/@handle/playlists")
    p.add_argument("-d", "--dest", required=True, help="parent folder — a subfolder named after the channel is created inside it")
    p.add_argument(
        "-f", "--format", choices=sorted(AUDIO_PROFILES), default=DEFAULT_PROFILE,
        help=f"audio profile (default: {DEFAULT_PROFILE} — native AAC, no re-encode)",
    )
    p.add_argument("-w", "--workers", type=int, default=3, help="videos downloaded in parallel (default: 3)")
    p.add_argument("--fragments", type=int, default=4, help="parallel chunks per video (default: 4)")
    p.add_argument("--cookies", help="path to a cookies.txt export (fixes bot checks)")
    p.add_argument("--cookies-from-browser", metavar="BROWSER", help="pull cookies live, e.g. chrome, firefox, edge")
    p.add_argument("--dedupe-by-title", action="store_true", help="also skip the same track re-uploaded under a different ID")
    p.add_argument("--retry-failed", action="store_true",
                   help="also retry entries marked unavailable, and failures that hit the attempt cap")
    p.add_argument("--no-metadata", action="store_true", help="do not write title/artist/album tags")
    p.add_argument("--no-thumbnail", action="store_true", help="do not embed cover art")
    p.add_argument("--rate-limit", metavar="RATE", help="cap download speed, e.g. 2M")
    p.add_argument("--sleep", type=float, default=0.0, metavar="SECS", help="pause between videos to stay under rate limits")
    p.add_argument("--staging", help="scratch dir for in-progress downloads (default: system temp)")
    p.add_argument("--no-reconcile", dest="reconcile", action="store_false", help="skip the pre-run filesystem scan")
    p.add_argument("-n", "--dry-run", action="store_true", help="show what would be downloaded, then stop")
    p.add_argument("-q", "--quiet", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true", help="show raw yt-dlp output")
    p.set_defaults(func=cmd_channel, reconcile=True)

    # --- status
    p = sub.add_parser("status", help="summarise what the library already contains")
    p.add_argument("-d", "--dest", required=True)
    p.add_argument("--failures", action="store_true", help="list failed and unavailable entries")
    p.set_defaults(func=cmd_status)

    # --- verify
    p = sub.add_parser("verify", help="re-sync the manifest with the files actually on disk")
    p.add_argument("-d", "--dest", required=True)
    p.set_defaults(func=cmd_verify)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted — progress saved, re-run to resume", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
