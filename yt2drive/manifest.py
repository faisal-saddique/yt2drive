"""Deduplication manifest.

Design notes
------------
The manifest is a single JSON file that lives *inside the destination folder*
(``<dest>/.yt2drive/manifest.json``). Keeping it next to the audio means the
dedup state travels with the library: a fresh Colab session, a different
machine, or a reinstall all pick up exactly where the last run left off, with
no local state to lose.

JSON rather than SQLite on purpose — the destination is usually a FUSE mount
(Google Drive), where SQLite's locking and mmap behaviour is unreliable. A flat
file written atomically sidesteps that entirely, stays human-readable, and is
trivially small even for thousands of tracks.

Three independent layers guard against re-downloading:

1. ``video_id`` — the primary key. Immutable, survives retitling.
2. Filesystem reconciliation — if the manifest is lost or a file was added by
   hand, ``reconcile()`` recovers state by parsing the ``[videoid]`` suffix off
   existing filenames.
3. ``title_key`` — optional near-duplicate catch for the same track uploaded
   twice under different IDs.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional

MANIFEST_VERSION = 1
MANIFEST_DIRNAME = ".yt2drive"
MANIFEST_FILENAME = "manifest.json"

# Matches the "[dQw4w9WgXcQ]" suffix that target_name() writes.
_ID_SUFFIX_RE = re.compile(r"\[([A-Za-z0-9_-]{11})\](?=\.[A-Za-z0-9]+$)")

AUDIO_EXTS = {".m4a", ".mp3", ".opus", ".ogg", ".webm", ".flac", ".wav", ".aac"}

STATUS_OK = "ok"
STATUS_FAILED = "failed"          # transient — will be retried
STATUS_UNAVAILABLE = "unavailable"  # private/deleted/geo-blocked — permanent
STATUS_DUPLICATE = "duplicate"    # skipped because title_key already present

# A transient failure retries automatically on the next run, but not forever:
# after this many attempts it is parked until an explicit --retry-failed.
MAX_ATTEMPTS = 5


@dataclass
class Entry:
    video_id: str
    title: str = ""
    uploader: str = ""
    filename: str = ""          # relative to the destination root
    filesize: int = 0
    duration: int = 0
    status: str = STATUS_OK
    error: str = ""
    attempts: int = 0
    title_key: str = ""
    duplicate_of: str = ""
    downloaded_at: float = 0.0
    playlist_id: str = ""

    @property
    def is_present(self) -> bool:
        return self.status == STATUS_OK and bool(self.filename)

    @classmethod
    def from_dict(cls, data: dict) -> "Entry":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


class Manifest:
    """Thread-safe, atomically-persisted record of everything already fetched."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.dir = self.root / MANIFEST_DIRNAME
        self.path = self.dir / MANIFEST_FILENAME
        self._entries: Dict[str, Entry] = {}
        self._lock = threading.RLock()
        self._dirty = False
        self.created_at = time.time()

    # ---------------------------------------------------------------- loading

    def load(self) -> "Manifest":
        if not self.path.exists():
            return self
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # A truncated manifest is recoverable — reconcile() rebuilds from
            # the filenames on disk. Preserve the bad file for inspection.
            try:
                self.path.rename(self.path.with_suffix(".json.corrupt"))
            except OSError:
                pass
            return self

        self.created_at = raw.get("created_at", time.time())
        for item in raw.get("entries", []):
            try:
                entry = Entry.from_dict(item)
            except TypeError:
                continue
            if entry.video_id:
                self._entries[entry.video_id] = entry
        return self

    # ---------------------------------------------------------------- queries

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[Entry]:
        with self._lock:
            return iter(list(self._entries.values()))

    def get(self, video_id: str) -> Optional[Entry]:
        with self._lock:
            return self._entries.get(video_id)

    def has(self, video_id: str) -> bool:
        with self._lock:
            return video_id in self._entries

    def is_done(self, video_id: str, retry_failed: bool = False) -> bool:
        """True if this ID should be skipped on a sync pass."""
        with self._lock:
            entry = self._entries.get(video_id)
            if entry is None:
                return False
            if entry.status in (STATUS_OK, STATUS_DUPLICATE):
                return True
            if entry.status == STATUS_UNAVAILABLE:
                # Permanent by nature; only --retry-failed reconsiders it.
                return not retry_failed
            if entry.status == STATUS_FAILED:
                # Transient: retry on the next run automatically, up to a cap so
                # a permanently broken video can't be retried on every sync.
                if retry_failed:
                    return False
                return entry.attempts >= MAX_ATTEMPTS
            return False

    def title_keys(self) -> Dict[str, str]:
        """Map of normalised title -> video_id, for near-duplicate detection."""
        with self._lock:
            return {
                e.title_key: e.video_id
                for e in self._entries.values()
                if e.title_key and e.status == STATUS_OK
            }

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        with self._lock:
            for e in self._entries.values():
                out[e.status] = out.get(e.status, 0) + 1
        return out

    def total_bytes(self) -> int:
        with self._lock:
            return sum(e.filesize for e in self._entries.values() if e.status == STATUS_OK)

    # ---------------------------------------------------------------- mutation

    def put(self, entry: Entry, save: bool = True) -> None:
        with self._lock:
            existing = self._entries.get(entry.video_id)
            if existing is not None:
                entry.attempts = max(entry.attempts, existing.attempts)
            self._entries[entry.video_id] = entry
            self._dirty = True
        if save:
            self.save()

    def record_failure(self, video_id: str, error: str, permanent: bool = False,
                       title: str = "", playlist_id: str = "") -> Entry:
        with self._lock:
            entry = self._entries.get(video_id) or Entry(video_id=video_id)
            entry.title = title or entry.title
            entry.playlist_id = playlist_id or entry.playlist_id
            entry.status = STATUS_UNAVAILABLE if permanent else STATUS_FAILED
            entry.error = (error or "")[:500]
            entry.attempts += 1
            self._entries[video_id] = entry
            self._dirty = True
        self.save()
        return entry

    # ---------------------------------------------------------------- persist

    def save(self, force: bool = False) -> None:
        """Write atomically: temp file in the same directory, then os.replace."""
        with self._lock:
            if not self._dirty and not force:
                return
            payload = {
                "version": MANIFEST_VERSION,
                "created_at": self.created_at,
                "updated_at": time.time(),
                "entries": [asdict(e) for e in self._entries.values()],
            }
            self.dir.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.dir), prefix=".manifest-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False, indent=1)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
                self._dirty = False
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

    # ---------------------------------------------------------------- recovery

    def reconcile(self, verbose: bool = False) -> Dict[str, int]:
        """Re-sync the manifest with what is actually on disk.

        Handles both directions:
        - files present on disk but missing from the manifest (adopted)
        - manifest rows whose file has been deleted or moved (marked missing)

        This is what makes the tool safe to point at a folder that already has
        audio in it, and what recovers a lost or corrupted manifest.
        """
        stats = {"adopted": 0, "missing": 0, "resized": 0}
        on_disk: Dict[str, Path] = {}

        if self.root.exists():
            for path in self.root.rglob("*"):
                if not path.is_file():
                    continue
                if MANIFEST_DIRNAME in path.parts:
                    continue
                if path.suffix.lower() not in AUDIO_EXTS:
                    continue
                match = _ID_SUFFIX_RE.search(path.name)
                if match:
                    on_disk[match.group(1)] = path

        with self._lock:
            for video_id, path in on_disk.items():
                rel = str(path.relative_to(self.root))
                size = path.stat().st_size
                entry = self._entries.get(video_id)
                if entry is None:
                    # Adopt a file the manifest never knew about. The title is
                    # whatever precedes the "[videoid]" suffix in the filename.
                    found = _ID_SUFFIX_RE.search(path.name)
                    title = path.name[:found.start()].strip(" -_") if found else path.stem
                    self._entries[video_id] = Entry(
                        video_id=video_id,
                        title=title or video_id,
                        filename=rel,
                        filesize=size,
                        status=STATUS_OK,
                        downloaded_at=path.stat().st_mtime,
                    )
                    stats["adopted"] += 1
                    self._dirty = True
                else:
                    if entry.filename != rel or entry.status != STATUS_OK:
                        entry.filename = rel
                        entry.status = STATUS_OK
                        entry.error = ""
                        self._dirty = True
                    if entry.filesize != size:
                        entry.filesize = size
                        stats["resized"] += 1
                        self._dirty = True

            for video_id, entry in self._entries.items():
                if entry.status != STATUS_OK:
                    continue
                if video_id in on_disk:
                    continue
                # Manifest says we have it, disk disagrees — queue for re-fetch.
                entry.status = STATUS_FAILED
                entry.error = "file missing from destination"
                entry.filename = ""
                entry.filesize = 0
                stats["missing"] += 1
                self._dirty = True

        self.save()
        return stats
