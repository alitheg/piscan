import hashlib
import subprocess
import threading
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from PIL import Image

from piscan import ingest
from piscan.ingest import (
    DeviceProbe,
    Ingest,
    Mounter,
    MountError,
    PassOutcome,
    ScannerState,
    is_complete_jpeg,
    run_pass,
)
from piscan.store import Store

T0 = datetime(2026, 10, 1, 9, 0, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


@pytest.fixture
def flash(tmp_path):
    d = tmp_path / "flash"
    (d / "DOXIE" / "JPEG").mkdir(parents=True)
    return d


def jpeg_bytes(seed: int, tmp_path) -> bytes:
    p = tmp_path / f"_gen{seed}.jpg"
    Image.new("RGB", (300, 200), (seed % 256, (seed * 5) % 256, 80)).save(p, "JPEG")
    return p.read_bytes()


def put(flash, tmp_path, name: str, seed: int):
    path = flash / "DOXIE" / "JPEG" / name
    path.write_bytes(jpeg_bytes(seed, tmp_path))
    return path


def run(flash, store, **kw):
    progress = []
    res = run_pass(flash, store, lambda d, t: progress.append((d, t)), now=lambda: T0, **kw)
    return res, progress


def stored_shas(store):
    return sorted(
        p.sha256 for d in store.list_drafts(["inbox", "sending", "sent", "failed"]) for p in d.pages
    )


def test_happy_path(flash, store, tmp_path):
    a = put(flash, tmp_path, "IMG_0002.JPG", 2)
    b = put(flash, tmp_path, "IMG_0001.jpg", 1)
    (flash / "DOXIE" / "JPEG" / "notes.txt").write_text("ignore me")
    sha_a = hashlib.sha256(a.read_bytes()).hexdigest()
    sha_b = hashlib.sha256(b.read_bytes()).hexdigest()

    res, progress = run(flash, store)

    assert res.ok and res.imported == 2
    assert stored_shas(store) == sorted([sha_a, sha_b])
    assert not a.exists() and not b.exists()
    assert (flash / "DOXIE" / "JPEG" / "notes.txt").exists()
    assert progress[0] == (0, 2) and progress[-1] == (2, 2)
    assert not list(store.pages_dir.glob("*.tmp"))


def test_missing_folder_is_empty_pass(tmp_path, store):
    res, _ = run(tmp_path, store)
    assert res.ok and res.imported == 0


def test_truncated_jpeg_left_in_place(flash, store, tmp_path):
    good = jpeg_bytes(1, tmp_path)
    bad = flash / "DOXIE" / "JPEG" / "IMG_0001.JPG"
    bad.write_bytes(good[:-10])
    res, _ = run(flash, store)
    assert res.ok and res.skipped == 1 and res.imported == 0
    assert bad.exists() and stored_shas(store) == []


def test_is_complete_jpeg(tmp_path):
    p = tmp_path / "x.jpg"
    for data, expected in [(b"", False), (b"\xff", False), (b"\xff\xd8\xff\xd9", True),
                           (b"\xff\xd8abc", False), (b"abc\xff\xd9", False)]:
        p.write_bytes(data)
        assert is_complete_jpeg(p) is expected


def test_hash_mismatch_when_source_changes_during_copy(flash, store, tmp_path, monkeypatch):
    src = put(flash, tmp_path, "IMG_0001.JPG", 1)
    real = ingest._copy_fsync

    def copy_then_change(s, d):
        real(s, d)
        s.write_bytes(jpeg_bytes(99, tmp_path))

    monkeypatch.setattr(ingest, "_copy_fsync", copy_then_change)
    res, _ = run(flash, store)
    assert res.skipped == 1 and res.imported == 0
    assert src.exists() and stored_shas(store) == []
    assert not list(store.pages_dir.glob("*.tmp"))

    # Next pass with a stable source imports it.
    monkeypatch.setattr(ingest, "_copy_fsync", real)
    res, _ = run(flash, store)
    assert res.imported == 1 and not src.exists()


def test_duplicate_content_deletes_source_once(flash, store, tmp_path):
    a = put(flash, tmp_path, "IMG_0001.JPG", 1)
    b = put(flash, tmp_path, "IMG_0002.JPG", 1)
    res, _ = run(flash, store)
    assert res.imported == 1 and res.deduped == 1
    assert len(stored_shas(store)) == 1
    assert not a.exists() and not b.exists()


def test_reused_filename_with_new_content(flash, store, tmp_path):
    put(flash, tmp_path, "IMG_0001.JPG", 1)
    run(flash, store)
    put(flash, tmp_path, "IMG_0001.JPG", 2)
    res, _ = run(flash, store)
    assert res.imported == 1
    assert len(stored_shas(store)) == 2


def test_low_disk_stops_pass_and_keeps_sources(flash, store, tmp_path):
    a = put(flash, tmp_path, "IMG_0001.JPG", 1)
    res, _ = run(flash, store, min_free=10**18)
    assert res.outcome is PassOutcome.STORAGE_FULL
    assert "storage full" in res.message
    assert a.exists() and stored_shas(store) == []


def test_oserror_mid_pass_aborts_without_deleting_unrecorded(flash, store, tmp_path, monkeypatch):
    a = put(flash, tmp_path, "IMG_0001.JPG", 1)
    b = put(flash, tmp_path, "IMG_0002.JPG", 2)
    c = put(flash, tmp_path, "IMG_0003.JPG", 3)
    real = ingest._copy_fsync

    def flaky(s, d):
        if s.name == "IMG_0002.JPG":
            raise OSError(5, "Input/output error")
        real(s, d)

    monkeypatch.setattr(ingest, "_copy_fsync", flaky)
    res, _ = run(flash, store)
    assert res.outcome is PassOutcome.MEDIA_LOST
    assert res.imported == 1
    assert not a.exists() and b.exists() and c.exists()
    assert len(stored_shas(store)) == 1
    assert not list(store.pages_dir.glob("*.tmp"))


def test_undecodable_page_is_failed_and_pass_continues(flash, store, tmp_path):
    bad = flash / "DOXIE" / "JPEG" / "IMG_0001.JPG"
    bad.write_bytes(b"\xff\xd8" + b"not an image" * 50 + b"\xff\xd9")
    good = put(flash, tmp_path, "IMG_0002.JPG", 2)
    res, _ = run(flash, store)
    assert res.failed == 1 and res.imported == 1 and not res.ok
    assert bad.exists() and not good.exists()


class SimulatedCrash(BaseException):
    pass


def test_crash_after_add_page_before_delete_is_deduped_next_pass(flash, store, tmp_path, monkeypatch):
    src = put(flash, tmp_path, "IMG_0001.JPG", 1)
    real = store.add_page

    def add_then_die(*a, **kw):
        real(*a, **kw)
        raise SimulatedCrash

    monkeypatch.setattr(store, "add_page", add_then_die)
    with pytest.raises(SimulatedCrash):
        run(flash, store)
    assert src.exists() and len(stored_shas(store)) == 1

    monkeypatch.undo()
    res, _ = run(flash, store)
    assert res.deduped == 1 and res.imported == 0
    assert not src.exists() and len(stored_shas(store)) == 1


def test_orphan_file_without_row_is_reimported(flash, store, tmp_path):
    src = put(flash, tmp_path, "IMG_0001.JPG", 1)
    sha = hashlib.sha256(src.read_bytes()).hexdigest()
    (store.pages_dir / f"{sha}.jpg").write_bytes(b"partial")  # crash between rename and insert
    res, _ = run(flash, store)
    assert res.imported == 1 and stored_shas(store) == [sha]


# -- probe and mounter -----------------------------------------------------


def test_probe_resolves_symlink_each_time(tmp_path):
    sys_block = tmp_path / "sys"
    (sys_block / "sda1").mkdir(parents=True)
    (sys_block / "sda1" / "size").write_text("1234\n")
    (sys_block / "sdb1").mkdir()
    (sys_block / "sdb1" / "size").write_text("0\n")
    dev_a = tmp_path / "dev" / "sda1"
    dev_a.parent.mkdir()
    dev_a.touch()
    dev_b = tmp_path / "dev" / "sdb1"
    dev_b.touch()
    link = tmp_path / "by-id"
    probe = DeviceProbe(link, sys_block)

    assert not probe.present() and probe.size() == 0
    link.symlink_to(dev_a)
    assert probe.present() and probe.size() == 1234
    link.unlink()
    link.symlink_to(dev_b)
    assert probe.size() == 0


def test_probe_partition_vanishing_mid_scan_reads_as_present_and_empty(tmp_path):
    # What a Pi Zero W showed: while the Doxie writes a scan the kernel drops sda1 and its
    # -part1 link, but the whole-disk link stays.
    sys_block = tmp_path / "sys"
    (sys_block / "sda1").mkdir(parents=True)
    (sys_block / "sda1" / "size").write_text("978944\n")
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "sda").touch()
    (dev / "sda1").touch()
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    disk_link = by_id / "usb-S2Flash_USB_Mass_Storage_0123-0:0"
    part_link = by_id / "usb-S2Flash_USB_Mass_Storage_0123-0:0-part1"
    disk_link.symlink_to(dev / "sda")
    part_link.symlink_to(dev / "sda1")
    probe = DeviceProbe(part_link, sys_block)
    assert probe.present() and probe.size() == 978944

    part_link.unlink()
    (dev / "sda1").unlink()
    (sys_block / "sda1" / "size").unlink()
    (sys_block / "sda1").rmdir()
    assert probe.present()
    assert probe.size() == 0

    disk_link.unlink()
    assert not probe.present()


def test_probe_unreadable_size_is_zero(tmp_path):
    dev = tmp_path / "sda1"
    dev.touch()
    assert DeviceProbe(dev, tmp_path / "nope").size() == 0


def test_mounter_commands_and_errors(tmp_path):
    calls = []
    codes = {"mount": 0, "sync": 0, "umount": 0}

    def fake_run(args, **kw):
        calls.append(args)
        return SimpleNamespace(returncode=codes[args[0]], stderr="busy")

    m = Mounter(tmp_path / "mnt", run=fake_run)
    m.mount()
    m.unmount()
    assert calls == [
        ["mount", str(tmp_path / "mnt")],
        ["sync"],
        ["umount", str(tmp_path / "mnt")],
    ]
    codes["mount"] = 32
    with pytest.raises(MountError, match="busy"):
        m.mount()
    codes["umount"] = 1
    with pytest.raises(MountError):
        m.unmount()


def test_mounter_wraps_oserror(tmp_path):
    def boom(args, **kw):
        raise subprocess.TimeoutExpired(args, 1)

    with pytest.raises(MountError):
        Mounter(tmp_path, run=boom).mount()


# -- Ingest ----------------------------------------------------------------


class FakeProbe:
    def __init__(self):
        self.is_present = False
        self.sectors = 1000

    def present(self):
        return self.is_present

    def size(self):
        return self.sectors


class FakeMounter:
    def __init__(self, mountpoint):
        self.mountpoint = mountpoint
        self.events = []
        self.fail_mount = None
        self.fail_unmount = None

    def mount(self):
        self.events.append("mount")
        if self.fail_mount:
            raise MountError(self.fail_mount)

    def unmount(self):
        self.events.append("unmount")
        if self.fail_unmount:
            raise MountError(self.fail_unmount)


@pytest.fixture
def rig(flash, store):
    probe = FakeProbe()
    mounter = FakeMounter(flash)
    return probe, mounter, Ingest(probe, mounter, store)


def test_status_transitions(rig, flash, store, tmp_path):
    probe, mounter, ing = rig
    states = []
    real_progress = ing._progress

    def spy(done, total):
        real_progress(done, total)
        states.append(ing.status())

    ing._progress = spy

    ing.tick()
    assert ing.status().state is ScannerState.OFF

    put(flash, tmp_path, "IMG_0001.JPG", 1)
    probe.is_present = True
    ing.tick()  # appears: pass runs
    assert ing.status().state is ScannerState.READY
    assert mounter.events == ["mount", "unmount"]
    assert any(s.state is ScannerState.IMPORTING and s.total == 1 for s in states)
    assert states[-1].message == "Importing... 1 of 1"

    ing.tick()  # re-armed by the import: one confirming pass, which finds nothing
    assert mounter.events == ["mount", "unmount"] * 2
    ing.tick()  # idle, no new pass
    assert mounter.events == ["mount", "unmount"] * 2

    probe.sectors = 0
    ing.tick()
    assert ing.status().state is ScannerState.SCANNING
    ing.tick()
    assert ing.status().state is ScannerState.SCANNING
    assert mounter.events == ["mount", "unmount"] * 2

    put(flash, tmp_path, "IMG_0002.JPG", 2)
    probe.sectors = 1000
    ing.tick()  # size returns: pass
    assert ing.status().state is ScannerState.READY
    assert mounter.events == ["mount", "unmount"] * 3
    assert len(stored_shas(store)) == 2

    probe.is_present = False
    ing.tick()
    assert ing.status().state is ScannerState.OFF


def test_already_plugged_in_at_start_with_scan_in_progress(rig):
    probe, mounter, ing = rig
    probe.is_present = True
    probe.sectors = 0
    ing.tick()
    assert ing.status().state is ScannerState.SCANNING and mounter.events == []
    probe.sectors = 1000
    ing.tick()
    assert mounter.events == ["mount", "unmount"]


def test_mount_failure_is_problem_and_retried_on_next_trigger(rig):
    probe, mounter, ing = rig
    mounter.fail_mount = "mount failed: wrong fs type"
    probe.is_present = True
    ing.tick()
    st = ing.status()
    assert st.state is ScannerState.PROBLEM and "wrong fs type" in st.message
    assert mounter.events == ["mount"]  # no unmount: nothing mounted
    ing.tick()
    assert mounter.events == ["mount"]  # no retry without a trigger

    probe.sectors = 0
    ing.tick()
    probe.sectors = 1000
    mounter.fail_mount = None
    ing.tick()
    assert ing.status().state is ScannerState.READY


def test_problem_persists_until_good_pass_and_off_overrides(rig, flash, store, tmp_path, monkeypatch):
    probe, mounter, ing = rig
    put(flash, tmp_path, "IMG_0001.JPG", 1)
    monkeypatch.setattr(store, "free_bytes", lambda: 0)
    monkeypatch.setattr(ingest, "run_pass", lambda *a, **k: run_pass(*a, min_free=1, **k))
    probe.is_present = True
    ing.tick()
    assert ing.status().state is ScannerState.PROBLEM
    assert "storage full" in ing.status().message
    assert mounter.events == ["mount", "unmount"]
    ing.tick()
    assert ing.status().state is ScannerState.PROBLEM

    probe.is_present = False
    ing.tick()
    assert ing.status().state is ScannerState.OFF


def test_unmount_attempted_when_pass_crashes(rig, monkeypatch):
    probe, mounter, ing = rig

    def boom(*a, **k):
        raise RuntimeError("kaput")

    monkeypatch.setattr(ingest, "run_pass", boom)
    probe.is_present = True
    ing.tick()
    assert mounter.events == ["mount", "unmount"]
    assert ing.status().state is ScannerState.PROBLEM


def test_unmount_failure_is_problem(rig):
    probe, mounter, ing = rig
    mounter.fail_unmount = "umount failed: busy"
    probe.is_present = True
    ing.tick()
    assert ing.status().state is ScannerState.PROBLEM
    assert "busy" in ing.status().message


def test_run_loop_stops(rig):
    ing = rig[2]
    ing.interval = 0.01
    stop = threading.Event()
    t = threading.Thread(target=ing.run, args=(stop,))
    t.start()
    stop.set()
    t.join(timeout=2)
    assert not t.is_alive()


# -- fix round 1 -----------------------------------------------------------


def test_media_lost_rearms_so_next_nonzero_tick_retries(rig, monkeypatch):
    probe, mounter, ing = rig
    outcomes = iter([PassOutcome.MEDIA_LOST, PassOutcome.OK])
    monkeypatch.setattr(
        ingest,
        "run_pass",
        lambda *a, **k: ingest.PassResult(next(outcomes), message="Lost contact"),
    )
    probe.is_present = True
    ing.tick()
    assert ing.status().state is ScannerState.PROBLEM
    ing.tick()  # size never seen at 0, but the pass is retried
    assert mounter.events == ["mount", "unmount"] * 2
    assert ing.status().state is ScannerState.READY
    ing.tick()
    assert mounter.events == ["mount", "unmount"] * 2


def test_unmount_still_attempted_when_sync_fails(tmp_path):
    calls = []

    def fake_run(args, **kw):
        calls.append(args[0])
        return SimpleNamespace(returncode=1 if args[0] == "sync" else 0, stderr="sync boom")

    with pytest.raises(MountError, match="sync boom"):
        Mounter(tmp_path, run=fake_run).unmount()
    assert calls == ["sync", "umount"]


def test_is_complete_jpeg_propagates_real_io_errors(tmp_path):
    with pytest.raises(FileNotFoundError):
        is_complete_jpeg(tmp_path / "missing.jpg")


def test_pi_side_write_error_is_distinguished(flash, store, tmp_path, monkeypatch):
    src = put(flash, tmp_path, "IMG_0001.JPG", 1)

    def full(s, d):
        raise ingest.PiWriteError("No space left on device")

    monkeypatch.setattr(ingest, "_copy_fsync", full)
    res, _ = run(flash, store)
    assert res.outcome is PassOutcome.PI_WRITE_FAILED
    assert "Pi storage" in res.message and "scanner" not in res.message
    assert src.exists()


def test_copy_wraps_destination_errors_only(tmp_path):
    src = tmp_path / "s.jpg"
    src.write_bytes(b"data")
    with pytest.raises(ingest.PiWriteError):
        ingest._copy_fsync(src, tmp_path / "no-such-dir" / "d.tmp")
    with pytest.raises(FileNotFoundError):
        ingest._copy_fsync(tmp_path / "missing", tmp_path / "d.tmp")


def test_incomplete_check_comes_before_free_space(flash, store, tmp_path):
    (flash / "DOXIE" / "JPEG" / "IMG_0001.JPG").write_bytes(b"\xff\xd8 partial")
    res, _ = run(flash, store, min_free=10**18)
    assert res.outcome is PassOutcome.OK and res.skipped == 1


def test_import_rearms_for_one_more_pass(rig, flash, tmp_path):
    probe, mounter, ing = rig
    put(flash, tmp_path, "IMG_0001.JPG", 1)
    probe.is_present = True
    ing.tick()  # imports one file
    assert mounter.events == ["mount", "unmount"]
    put(flash, tmp_path, "IMG_0002.JPG", 2)  # written entirely during the pass
    ing.tick()  # no size dip seen, but the import re-armed
    assert mounter.events == ["mount", "unmount"] * 2
    assert not list((flash / "DOXIE" / "JPEG").iterdir())
    ing.tick()  # that import re-armed again; this pass finds nothing
    assert mounter.events == ["mount", "unmount"] * 3
    ing.tick()  # so it stops
    assert mounter.events == ["mount", "unmount"] * 3


def test_deduped_pass_also_rearms(rig, flash, store, tmp_path):
    probe, mounter, ing = rig
    put(flash, tmp_path, "IMG_0001.JPG", 1)
    run(flash, store)
    put(flash, tmp_path, "IMG_0001.JPG", 1)  # same content, left behind by a crash
    probe.is_present = True
    ing.tick()
    ing.tick()
    assert mounter.events == ["mount", "unmount"] * 2
    ing.tick()
    assert mounter.events == ["mount", "unmount"] * 2


def test_storage_full_retries_on_a_timer_and_recovers(flash, store, tmp_path, monkeypatch):
    now = [1000.0]
    probe, mounter = FakeProbe(), FakeMounter(flash)
    ing = Ingest(probe, mounter, store, retry_interval=60, monotonic=lambda: now[0])
    put(flash, tmp_path, "IMG_0001.JPG", 1)
    free = [0]
    monkeypatch.setattr(store, "free_bytes", lambda: free[0])
    monkeypatch.setattr(ingest, "run_pass", lambda *a, **k: run_pass(*a, min_free=1, **k))
    probe.is_present = True
    ing.tick()
    assert "storage full" in ing.status().message
    assert mounter.events == ["mount", "unmount"]

    now[0] += 59
    ing.tick()  # too soon
    assert mounter.events == ["mount", "unmount"]

    now[0] += 2
    ing.tick()  # due, still full: the timer restarts
    assert mounter.events == ["mount", "unmount"] * 2
    assert ing.status().state is ScannerState.PROBLEM

    free[0] = 10**9
    now[0] += 61
    ing.tick()
    assert mounter.events == ["mount", "unmount"] * 3
    assert ing.status().state is ScannerState.READY
    assert len(stored_shas(store)) == 1
    now[0] += 600
    ing.tick()  # the import re-armed once, nothing else is pending
    ing.tick()
    assert mounter.events == ["mount", "unmount"] * 4


def test_pi_write_failure_also_retries(rig, monkeypatch):
    probe, mounter, ing = rig
    now = [0.0]
    ing._monotonic = lambda: now[0]
    outcomes = iter([PassOutcome.PI_WRITE_FAILED, PassOutcome.OK])
    monkeypatch.setattr(
        ingest, "run_pass", lambda *a, **k: ingest.PassResult(next(outcomes), message="x")
    )
    probe.is_present = True
    ing.tick()
    now[0] = 61
    ing.tick()
    assert mounter.events == ["mount", "unmount"] * 2
    assert ing.status().state is ScannerState.READY


def test_status_message_is_the_bare_reason(rig):
    probe, mounter, ing = rig
    mounter.fail_mount = "mount failed: wrong fs type"
    probe.is_present = True
    ing.tick()
    assert ing.status().message == "mount failed: wrong fs type"


def test_progress_with_no_files_is_plain(rig):
    ing = rig[2]
    ing._progress(0, 0)
    assert ing.status().message == "Importing..."
