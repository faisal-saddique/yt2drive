import json

from yt2drive.manifest import (
    STATUS_FAILED,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    Entry,
    Manifest,
)


def make_track(root, video_id, title="Track", size=1024):
    path = root / f"{title} [{video_id}].m4a"
    path.write_bytes(b"\0" * size)
    return path


def test_roundtrip(tmp_path):
    m = Manifest(tmp_path)
    m.put(Entry(video_id="aaaaaaaaaaa", title="One", filename="One [aaaaaaaaaaa].m4a", filesize=10))
    reloaded = Manifest(tmp_path).load()
    assert len(reloaded) == 1
    assert reloaded.get("aaaaaaaaaaa").title == "One"


def test_manifest_lives_inside_destination(tmp_path):
    m = Manifest(tmp_path)
    m.put(Entry(video_id="aaaaaaaaaaa"))
    assert (tmp_path / ".yt2drive" / "manifest.json").exists()


def test_is_done_semantics(tmp_path):
    m = Manifest(tmp_path)
    m.put(Entry(video_id="ok000000000", status=STATUS_OK))
    m.put(Entry(video_id="fail0000000", status=STATUS_FAILED))
    m.put(Entry(video_id="gone0000000", status=STATUS_UNAVAILABLE))

    assert m.is_done("ok000000000") is True
    assert m.is_done("fail0000000") is False       # transient failures retry by default
    assert m.is_done("gone0000000") is True        # permanent, skipped by default
    assert m.is_done("gone0000000", retry_failed=True) is False
    assert m.is_done("never_seen1") is False


def test_corrupt_manifest_is_quarantined_not_fatal(tmp_path):
    d = tmp_path / ".yt2drive"
    d.mkdir()
    (d / "manifest.json").write_text("{ this is not json")
    m = Manifest(tmp_path).load()
    assert len(m) == 0
    assert (d / "manifest.json.corrupt").exists()


def test_reconcile_adopts_preexisting_files(tmp_path):
    """Pointing at a folder that already has audio must not re-download it."""
    make_track(tmp_path, "abcdefghijk", "Existing Song")
    m = Manifest(tmp_path).load()
    stats = m.reconcile()

    assert stats["adopted"] == 1
    assert m.is_done("abcdefghijk") is True
    assert m.get("abcdefghijk").title == "Existing Song"


def test_reconcile_recovers_a_deleted_manifest(tmp_path):
    for i, vid in enumerate(["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"]):
        make_track(tmp_path, vid, f"Song {i}")
    m = Manifest(tmp_path).load()
    m.reconcile()
    assert len({e.video_id for e in m}) == 3
    assert all(m.is_done(v) for v in ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"])


def test_reconcile_flags_files_deleted_behind_our_back(tmp_path):
    path = make_track(tmp_path, "aaaaaaaaaaa", "Gone Song")
    m = Manifest(tmp_path).load()
    m.reconcile()
    assert m.is_done("aaaaaaaaaaa")

    path.unlink()
    stats = m.reconcile()
    assert stats["missing"] == 1
    assert m.is_done("aaaaaaaaaaa") is False  # queued for re-download


def test_reconcile_ignores_non_audio_and_own_metadata(tmp_path):
    (tmp_path / "notes [abcdefghijk].txt").write_text("x")
    m = Manifest(tmp_path).load()
    m.reconcile()
    assert len(m) == 0


def test_reconcile_picks_up_renamed_files(tmp_path):
    old = make_track(tmp_path, "aaaaaaaaaaa", "Old Name")
    m = Manifest(tmp_path).load()
    m.reconcile()
    new = tmp_path / "New Name [aaaaaaaaaaa].m4a"
    old.rename(new)
    m.reconcile()
    assert m.get("aaaaaaaaaaa").filename == "New Name [aaaaaaaaaaa].m4a"
    assert m.is_done("aaaaaaaaaaa") is True


def test_reconcile_finds_files_in_subfolders(tmp_path):
    sub = tmp_path / "Playlist A"
    sub.mkdir()
    make_track(sub, "aaaaaaaaaaa", "Nested")
    m = Manifest(tmp_path).load()
    m.reconcile()
    assert m.is_done("aaaaaaaaaaa")


def test_title_keys_only_returns_successful_entries(tmp_path):
    m = Manifest(tmp_path)
    m.put(Entry(video_id="ok000000000", status=STATUS_OK, title_key="song one"))
    m.put(Entry(video_id="fail0000000", status=STATUS_FAILED, title_key="song two"))
    assert m.title_keys() == {"song one": "ok000000000"}


def test_failures_stop_retrying_after_the_attempt_cap(tmp_path):
    """A permanently broken video must not be retried on every single sync."""
    from yt2drive.manifest import MAX_ATTEMPTS

    m = Manifest(tmp_path)
    for _ in range(MAX_ATTEMPTS - 1):
        m.record_failure("aaaaaaaaaaa", "boom")
    assert m.is_done("aaaaaaaaaaa") is False  # still retrying

    m.record_failure("aaaaaaaaaaa", "boom")
    assert m.is_done("aaaaaaaaaaa") is True   # capped out
    assert m.is_done("aaaaaaaaaaa", retry_failed=True) is False  # forced retry


def test_record_failure_increments_attempts(tmp_path):
    m = Manifest(tmp_path)
    m.record_failure("aaaaaaaaaaa", "boom")
    m.record_failure("aaaaaaaaaaa", "boom again")
    assert m.get("aaaaaaaaaaa").attempts == 2
    assert m.get("aaaaaaaaaaa").status == STATUS_FAILED


def test_save_is_atomic_and_leaves_no_temp_files(tmp_path):
    m = Manifest(tmp_path)
    for i in range(50):
        m.put(Entry(video_id=f"vid{i:08d}", title=f"T{i}"))
    leftovers = list((tmp_path / ".yt2drive").glob(".manifest-*"))
    assert leftovers == []
    data = json.loads((tmp_path / ".yt2drive" / "manifest.json").read_text())
    assert len(data["entries"]) == 50


def test_total_bytes_counts_only_present_files(tmp_path):
    m = Manifest(tmp_path)
    m.put(Entry(video_id="a" * 11, status=STATUS_OK, filesize=100))
    m.put(Entry(video_id="b" * 11, status=STATUS_FAILED, filesize=999))
    assert m.total_bytes() == 100
