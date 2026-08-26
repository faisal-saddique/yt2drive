"""Integration tests that run the real yt-dlp + ffmpeg pipeline.

YouTube itself is not reachable from CI, so these serve a synthetic media file
over localhost and push it through the same options dict and postprocessor
chain that a real download uses. That proves the ffmpeg wiring, the audio
extraction, the tag writing and the atomic move are all correct — everything
except the YouTube extractor itself.
"""

import functools
import http.server
import json
import shutil
import socketserver
import subprocess
import threading
from pathlib import Path

import pytest

from yt2drive.downloader import (
    AUDIO_PROFILES,
    SyncOptions,
    _AlbumTagger,
    _download_opts,
    _resolve_output,
    _safe_move,
)

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


@pytest.fixture(scope="module")
def media_dir(tmp_path_factory):
    """A 3-second AAC-in-m4a file, standing in for YouTube's itag 140 stream."""
    d = tmp_path_factory.mktemp("media")
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
         "-c:a", "aac", "-b:a", "128k", str(d / "sample.m4a")],
        check=True,
    )
    return d


@pytest.fixture(scope="module")
def server(media_dir):
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(media_dir))

    class Quiet(socketserver.TCPServer):
        allow_reuse_address = True

    httpd = Quiet(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def ffprobe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


# ---------------------------------------------------------------- options

def test_every_profile_builds_valid_ytdlp_options(tmp_path):
    from yt_dlp import YoutubeDL

    for profile in AUDIO_PROFILES:
        opts = SyncOptions(dest=tmp_path / "lib", profile=profile)
        ydl_opts = _download_opts(opts, tmp_path)
        with YoutubeDL(ydl_opts):  # constructing validates the postprocessor chain
            pass


def test_postprocessors_run_in_the_right_order(tmp_path):
    opts = SyncOptions(dest=tmp_path / "lib", embed_metadata=True, embed_thumbnail=True)
    keys = [pp["key"] for pp in _download_opts(opts, tmp_path)["postprocessors"]]

    # Thumbnails must be converted to jpeg before download completes, audio must
    # be extracted before tags are written, and cover art embedded last.
    assert keys.index("FFmpegThumbnailsConvertor") < keys.index("FFmpegExtractAudio")
    assert keys.index("FFmpegExtractAudio") < keys.index("FFmpegMetadata")
    assert keys.index("FFmpegMetadata") < keys.index("EmbedThumbnail")


def test_flags_disable_the_right_postprocessors(tmp_path):
    opts = SyncOptions(dest=tmp_path / "lib", embed_metadata=False, embed_thumbnail=False)
    keys = [pp["key"] for pp in _download_opts(opts, tmp_path)["postprocessors"]]
    assert keys == ["FFmpegExtractAudio"]


def test_default_profile_does_not_transcode():
    """The whole point of defaulting to m4a: ffmpeg stream-copies YouTube's AAC."""
    assert AUDIO_PROFILES["m4a"]["transcodes"] is False
    assert "bestaudio[ext=m4a]" in AUDIO_PROFILES["m4a"]["format"]


def test_rate_limit_parsing(tmp_path):
    from yt2drive.downloader import _parse_rate
    assert _parse_rate("2M") == 2 * 1024 * 1024
    assert _parse_rate("500K") == 500 * 1024
    assert _parse_rate("1.5M") == int(1.5 * 1024 * 1024)
    assert _parse_rate("nonsense") is None


# ---------------------------------------------------------------- real run

def test_real_download_produces_playable_tagged_audio(server, tmp_path):
    from yt_dlp import YoutubeDL

    staging = tmp_path / "staging"
    staging.mkdir()
    dest = tmp_path / "lib"
    dest.mkdir()

    opts = SyncOptions(dest=dest, profile="m4a", embed_metadata=True, embed_thumbnail=False)
    ydl_opts = _download_opts(opts, staging)

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"{server}/sample.m4a", download=True)

    produced = _resolve_output(info, staging, info["id"], "m4a")
    assert produced is not None and produced.exists()

    probe = ffprobe(produced)
    audio = [s for s in probe["streams"] if s["codec_type"] == "audio"]
    assert len(audio) == 1
    assert audio[0]["codec_name"] == "aac"
    assert float(probe["format"]["duration"]) == pytest.approx(3.0, abs=0.5)


def test_real_download_gets_tagged_with_the_destination_folder_name(server, tmp_path):
    """Players group audio by Album, not by folder — so every download must be
    stamped Album=<destination folder name> for a synced library to show up as
    its own group, and this must survive the real ffmpeg postprocessor chain."""
    from yt_dlp import YoutubeDL

    staging = tmp_path / "staging"
    staging.mkdir()
    dest = tmp_path / "My Cool Playlist"
    dest.mkdir()

    opts = SyncOptions(dest=dest, profile="m4a", embed_metadata=True, embed_thumbnail=False)
    ydl_opts = _download_opts(opts, staging)

    with YoutubeDL(ydl_opts) as ydl:
        ydl.add_post_processor(_AlbumTagger(opts.dest.name), when="pre_process")
        info = ydl.extract_info(f"{server}/sample.m4a", download=True)

    produced = _resolve_output(info, staging, info["id"], "m4a")
    assert produced is not None and produced.exists()

    probe = ffprobe(produced)
    assert probe["format"]["tags"]["album"] == "My Cool Playlist"


def test_safe_move_is_atomic_and_leaves_no_partials(tmp_path):
    src = tmp_path / "src.m4a"
    src.write_bytes(b"payload")
    dst = tmp_path / "dest" / "final.m4a"
    dst.parent.mkdir()

    _safe_move(src, dst)

    assert dst.read_bytes() == b"payload"
    assert not src.exists()
    assert list(dst.parent.glob("*.incoming")) == []


def test_safe_move_replaces_a_leftover_partial(tmp_path):
    dst = tmp_path / "final.m4a"
    (tmp_path / "final.m4a.incoming").write_bytes(b"junk from a killed run")
    src = tmp_path / "src.m4a"
    src.write_bytes(b"good")

    _safe_move(src, dst)

    assert dst.read_bytes() == b"good"
    assert list(tmp_path.glob("*.incoming")) == []


def test_resolve_output_ignores_thumbnail_sidecars(tmp_path):
    (tmp_path / "abc.jpg").write_bytes(b"x" * 5000)
    (tmp_path / "abc.webp").write_bytes(b"x" * 5000)
    (tmp_path / "abc.m4a").write_bytes(b"x" * 100)

    found = _resolve_output({}, tmp_path, "abc", "m4a")
    assert found is not None
    assert found.suffix == ".m4a"


def test_resolve_output_ignores_partial_files(tmp_path):
    (tmp_path / "abc.m4a.part").write_bytes(b"x" * 9999)
    assert _resolve_output({}, tmp_path, "abc", "m4a") is None
