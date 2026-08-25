"""Tests for the diff/dedup/orchestration layer, with the network stubbed out."""

import threading
import time

import pytest

from yt2drive import downloader
from yt2drive.downloader import (
    BotCheckError,
    PlaylistItem,
    SyncOptions,
    _is_permanent,
    _raise_if_botcheck,
    sync_playlist,
)
from yt2drive.manifest import STATUS_DUPLICATE, STATUS_OK, Entry, Manifest
from yt2drive.naming import target_name, title_key


@pytest.fixture
def opts(tmp_path):
    return SyncOptions(dest=tmp_path / "lib", workers=2, staging=tmp_path / "staging")


def stub_playlist(monkeypatch, items, pid="PL123", title="Test Playlist"):
    monkeypatch.setattr(downloader, "list_playlist", lambda url, o: (pid, title, list(items)))


def stub_downloads(monkeypatch, record=None, fail_ids=(), error="network exploded"):
    """Replace the real fetch with one that writes a small file."""
    calls = record if record is not None else []
    lock = threading.Lock()

    def fake(item, o, staging_root):
        with lock:
            calls.append(item.video_id)
        if item.video_id in fail_ids:
            raise RuntimeError(error)
        o.dest.mkdir(parents=True, exist_ok=True)
        name = target_name(item.title, item.video_id, "m4a")
        path = o.dest / name
        path.write_bytes(b"\0" * 2048)
        return Entry(
            video_id=item.video_id, title=item.title, uploader=item.uploader,
            filename=name, filesize=2048, status=STATUS_OK,
            title_key=title_key(item.title, item.uploader), downloaded_at=time.time(),
        )

    monkeypatch.setattr(downloader, "download_one", fake)
    return calls


# ---------------------------------------------------------------- core diffing

def test_downloads_everything_on_first_run(monkeypatch, opts):
    items = [PlaylistItem(video_id=f"vid{i:08d}", title=f"Song {i}") for i in range(5)]
    stub_playlist(monkeypatch, items)
    calls = stub_downloads(monkeypatch)

    manifest = Manifest(opts.dest).load()
    result = sync_playlist("url", opts, manifest)

    assert result.downloaded == 5
    assert len(calls) == 5
    assert len(list(opts.dest.glob("*.m4a"))) == 5


def test_second_run_downloads_nothing(monkeypatch, opts):
    """The whole point: re-running a sync must be a no-op."""
    items = [PlaylistItem(video_id=f"vid{i:08d}", title=f"Song {i}") for i in range(5)]
    stub_playlist(monkeypatch, items)

    manifest = Manifest(opts.dest).load()
    stub_downloads(monkeypatch)
    sync_playlist("url", opts, manifest)

    calls = stub_downloads(monkeypatch)
    manifest2 = Manifest(opts.dest).load()
    result = sync_playlist("url", opts, manifest2)

    assert calls == []
    assert result.downloaded == 0
    assert result.already_present == 5


def test_only_new_additions_are_fetched(monkeypatch, opts):
    first = [PlaylistItem(video_id=f"vid{i:08d}", title=f"Song {i}") for i in range(3)]
    stub_playlist(monkeypatch, first)
    stub_downloads(monkeypatch)
    sync_playlist("url", opts, Manifest(opts.dest).load())

    grown = first + [PlaylistItem(video_id="vid00000099", title="Brand New")]
    stub_playlist(monkeypatch, grown)
    calls = stub_downloads(monkeypatch)
    result = sync_playlist("url", opts, Manifest(opts.dest).load())

    assert calls == ["vid00000099"]
    assert result.downloaded == 1
    assert result.already_present == 3


def test_dedup_survives_a_lost_manifest(monkeypatch, opts):
    """Files on disk are the source of truth even if the manifest is deleted."""
    items = [PlaylistItem(video_id=f"vid{i:08d}", title=f"Song {i}") for i in range(4)]
    stub_playlist(monkeypatch, items)
    stub_downloads(monkeypatch)
    sync_playlist("url", opts, Manifest(opts.dest).load())

    (opts.dest / ".yt2drive" / "manifest.json").unlink()

    calls = stub_downloads(monkeypatch)
    manifest = Manifest(opts.dest).load()
    manifest.reconcile()
    result = sync_playlist("url", opts, manifest)

    assert calls == []
    assert result.already_present == 4


def test_duplicate_ids_within_one_playlist_download_once(monkeypatch, opts):
    items = [
        PlaylistItem(video_id="vid00000001", title="Song"),
        PlaylistItem(video_id="vid00000001", title="Song"),
    ]
    stub_playlist(monkeypatch, items)
    calls = stub_downloads(monkeypatch)
    sync_playlist("url", opts, Manifest(opts.dest).load())
    assert calls == ["vid00000001"]


def test_shared_tracks_across_two_playlists_download_once(monkeypatch, opts):
    shared = PlaylistItem(video_id="shared00001", title="Shared Song")
    manifest = Manifest(opts.dest).load()

    stub_playlist(monkeypatch, [shared, PlaylistItem(video_id="only0000001", title="A")])
    stub_downloads(monkeypatch)
    sync_playlist("url-a", opts, manifest)

    stub_playlist(monkeypatch, [shared, PlaylistItem(video_id="only0000002", title="B")])
    calls = stub_downloads(monkeypatch)
    sync_playlist("url-b", opts, manifest)

    assert calls == ["only0000002"]


# ---------------------------------------------------------------- title dedup

def test_title_dedup_skips_reuploads(monkeypatch, tmp_path):
    opts = SyncOptions(dest=tmp_path / "lib", workers=1, dedupe_by_title=True,
                       staging=tmp_path / "staging")
    items = [
        PlaylistItem(video_id="vid00000001", title="Artist - Track (Official Music Video)"),
        PlaylistItem(video_id="vid00000002", title="Artist - Track [HD] (Lyrics)"),
    ]
    stub_playlist(monkeypatch, items)
    calls = stub_downloads(monkeypatch)

    manifest = Manifest(opts.dest).load()
    result = sync_playlist("url", opts, manifest)

    assert calls == ["vid00000001"]
    assert result.skipped_duplicate == 1
    assert manifest.get("vid00000002").status == STATUS_DUPLICATE
    assert manifest.get("vid00000002").duplicate_of == "vid00000001"


def test_title_dedup_is_off_by_default(monkeypatch, opts):
    items = [
        PlaylistItem(video_id="vid00000001", title="Artist - Track (Official Music Video)"),
        PlaylistItem(video_id="vid00000002", title="Artist - Track [HD] (Lyrics)"),
    ]
    stub_playlist(monkeypatch, items)
    calls = stub_downloads(monkeypatch)
    sync_playlist("url", opts, Manifest(opts.dest).load())
    assert len(calls) == 2


# ---------------------------------------------------------------- failures

def test_private_videos_are_recorded_and_never_retried(monkeypatch, opts):
    items = [
        PlaylistItem(video_id="vid00000001", title="Good"),
        PlaylistItem(video_id="vid00000002", title="[Private video]", unavailable=True),
    ]
    stub_playlist(monkeypatch, items)
    calls = stub_downloads(monkeypatch)

    manifest = Manifest(opts.dest).load()
    result = sync_playlist("url", opts, manifest)

    assert calls == ["vid00000001"]
    assert result.unavailable == 1

    calls2 = stub_downloads(monkeypatch)
    sync_playlist("url", opts, Manifest(opts.dest).load())
    assert calls2 == []


def test_transient_failures_retry_on_next_run(monkeypatch, opts):
    items = [PlaylistItem(video_id="vid00000001", title="Flaky")]
    stub_playlist(monkeypatch, items)

    stub_downloads(monkeypatch, fail_ids={"vid00000001"})
    result = sync_playlist("url", opts, Manifest(opts.dest).load())
    assert result.failed == 1
    assert result.downloaded == 0

    calls = stub_downloads(monkeypatch)
    result = sync_playlist("url", opts, Manifest(opts.dest).load())
    assert calls == ["vid00000001"]
    assert result.downloaded == 1


def test_one_failure_does_not_stop_the_batch(monkeypatch, opts):
    items = [PlaylistItem(video_id=f"vid{i:08d}", title=f"S{i}") for i in range(6)]
    stub_playlist(monkeypatch, items)
    stub_downloads(monkeypatch, fail_ids={"vid00000003"})

    result = sync_playlist("url", opts, Manifest(opts.dest).load())
    assert result.downloaded == 5
    assert result.failed == 1


def test_bot_check_aborts_the_whole_run(monkeypatch, opts):
    """Hammering a rate-limited endpoint makes things worse — stop immediately."""
    items = [PlaylistItem(video_id=f"vid{i:08d}", title=f"S{i}") for i in range(20)]
    stub_playlist(monkeypatch, items)

    def fake(item, o, staging_root):
        raise BotCheckError("Sign in to confirm you're not a bot")

    monkeypatch.setattr(downloader, "download_one", fake)
    opts.workers = 1
    result = sync_playlist("url", opts, Manifest(opts.dest).load())

    assert result.aborted is True
    assert result.downloaded == 0


def test_bot_check_hidden_in_a_generic_error_is_still_caught(monkeypatch, opts):
    items = [PlaylistItem(video_id="vid00000001", title="S")]
    stub_playlist(monkeypatch, items)

    def fake(item, o, staging_root):
        raise RuntimeError("ERROR: [youtube] HTTP Error 429: Too Many Requests")

    monkeypatch.setattr(downloader, "download_one", fake)
    result = sync_playlist("url", opts, Manifest(opts.dest).load())
    assert result.aborted is True


# ---------------------------------------------------------------- classifiers

@pytest.mark.parametrize("message", [
    "ERROR: [youtube] abc: Private video. Sign in if you've been granted access",
    "Video unavailable. This video has been removed by the uploader",
    "This video is unavailable",
    "The account associated with this video has been terminated",
])
def test_permanent_errors_are_classified(message):
    assert _is_permanent(message) is True


@pytest.mark.parametrize("message", [
    "unable to download video data: HTTP Error 503",
    "Read timed out",
    "ffmpeg exited with code 1",
])
def test_transient_errors_are_not_permanent(message):
    assert _is_permanent(message) is False


@pytest.mark.parametrize("message", [
    "Sign in to confirm you're not a bot",
    "ERROR: HTTP Error 429: Too Many Requests",
])
def test_botcheck_detection(message):
    with pytest.raises(BotCheckError):
        _raise_if_botcheck(message)


def test_ordinary_errors_do_not_trigger_botcheck():
    _raise_if_botcheck("HTTP Error 404: Not Found")  # must not raise


# ---------------------------------------------------------------- misc

def test_dry_run_downloads_nothing(monkeypatch, tmp_path):
    opts = SyncOptions(dest=tmp_path / "lib", dry_run=True, staging=tmp_path / "s")
    items = [PlaylistItem(video_id=f"vid{i:08d}", title=f"S{i}") for i in range(3)]
    stub_playlist(monkeypatch, items)
    calls = stub_downloads(monkeypatch)

    result = sync_playlist("url", opts, Manifest(opts.dest).load())
    assert calls == []
    assert result.total_in_playlist == 3


def test_parallel_workers_all_complete(monkeypatch, tmp_path):
    opts = SyncOptions(dest=tmp_path / "lib", workers=8, staging=tmp_path / "s")
    items = [PlaylistItem(video_id=f"vid{i:08d}", title=f"S{i}") for i in range(30)]
    stub_playlist(monkeypatch, items)
    calls = stub_downloads(monkeypatch)

    result = sync_playlist("url", opts, Manifest(opts.dest).load())
    assert result.downloaded == 30
    assert len(set(calls)) == 30
    assert len(Manifest(opts.dest).load().title_keys()) == 30


def test_manifest_is_persisted_after_each_download(monkeypatch, opts):
    """A killed session must not lose everything it already fetched."""
    items = [PlaylistItem(video_id=f"vid{i:08d}", title=f"S{i}") for i in range(3)]
    stub_playlist(monkeypatch, items)
    stub_downloads(monkeypatch, fail_ids={"vid00000002"})
    sync_playlist("url", opts, Manifest(opts.dest).load())

    on_disk = Manifest(opts.dest).load()
    assert on_disk.is_done("vid00000000")
    assert on_disk.is_done("vid00000001")
