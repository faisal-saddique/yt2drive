"""Filename sanitisation and title normalisation for near-duplicate detection."""

from __future__ import annotations

import re
import unicodedata

# Characters that are illegal on Windows/macOS/Linux or on Google Drive.
_ILLEGAL = r'<>:"/\\|?*\x00-\x1f'
_ILLEGAL_RE = re.compile(f"[{_ILLEGAL}]")
_WS_RE = re.compile(r"\s+")

# Reserved device names on Windows. Drive itself does not care, but a synced
# folder on a Windows box will choke on these.
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

MAX_STEM = 120  # leave room for " [videoid].m4a" inside the 255-byte limit


def safe_filename(stem: str, fallback: str = "untitled") -> str:
    """Turn an arbitrary video title into a portable filename stem."""
    stem = unicodedata.normalize("NFC", stem or "")
    stem = _ILLEGAL_RE.sub("_", stem)
    # Collapse whitespace and strip characters that break trailing-space rules.
    stem = _WS_RE.sub(" ", stem).strip().strip(". ")
    if stem.upper() in _RESERVED:
        stem = f"_{stem}"
    if len(stem) > MAX_STEM:
        stem = stem[:MAX_STEM].rstrip()
    return stem or fallback


# --- near-duplicate detection -------------------------------------------------
#
# The same track often appears in a playlist twice under different uploads:
#   "Artist - Song (Official Music Video)"
#   "Artist - Song [HD] (Lyrics)"
# Normalising away the decoration lets us catch that when --dedupe-by-title is on.

_NOISE_PATTERNS = [
    r"\(\s*official[^)]*\)", r"\[\s*official[^\]]*\]",
    r"\(\s*lyrics?[^)]*\)", r"\[\s*lyrics?[^\]]*\]",
    r"\(\s*audio\s*\)", r"\[\s*audio\s*\]",
    r"\(\s*video\s*\)", r"\[\s*video\s*\]",
    r"\(\s*hd\s*\)", r"\[\s*hd\s*\]",
    r"\(\s*4k\s*\)", r"\[\s*4k\s*\]",
    r"\(\s*hq\s*\)", r"\[\s*hq\s*\]",
    r"\(\s*remaster(ed)?[^)]*\)", r"\[\s*remaster(ed)?[^\]]*\]",
    r"\(\s*full\s+(song|album|video)[^)]*\)",
    r"\(\s*with\s+lyrics[^)]*\)",
    r"\bofficial\s+(music\s+)?video\b",
    r"\bofficial\s+audio\b",
    r"\blyric(s)?\s+video\b",
    r"\bmusic\s+video\b",
    r"\bfull\s+song\b",
    r"\bhd\s+video\b",
]
_NOISE_RE = re.compile("|".join(_NOISE_PATTERNS), re.IGNORECASE)
_FEAT_RE = re.compile(r"\b(feat\.?|ft\.?|featuring)\b.*$", re.IGNORECASE)
_NONWORD_RE = re.compile(r"[^\w\s]", re.UNICODE)


def title_key(title: str, uploader: str = "") -> str:
    """A normalised key for spotting the same track under a different upload.

    Deliberately lossy. Only used when --dedupe-by-title is enabled, because it
    can occasionally collapse a studio version and a live version into one key.
    """
    text = unicodedata.normalize("NFKD", (title or "").lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = _NOISE_RE.sub(" ", text)
    text = _FEAT_RE.sub(" ", text)
    text = _NONWORD_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()

    if uploader:
        up = unicodedata.normalize("NFKD", uploader.lower())
        up = "".join(c for c in up if not unicodedata.combining(c))
        up = _NONWORD_RE.sub(" ", up)
        up = _WS_RE.sub(" ", up).strip()
        # Drop the channel name if the title already repeats it.
        if up and text.startswith(up + " "):
            text = text[len(up) + 1:].strip()

    return text


def target_name(title: str, video_id: str, ext: str) -> str:
    """Final on-disk name: stable across playlist reordering, unique by video ID."""
    return f"{safe_filename(title, fallback=video_id)} [{video_id}].{ext.lstrip('.')}"
