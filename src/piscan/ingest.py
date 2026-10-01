"""Watch the scanner's flash, copy new scans into the Store, then delete them from the flash.

The order that matters, per file: copy, fsync, hash the copy, re-hash the source, record in the
Store, and only then delete the source. A crash anywhere leaves the page safe on the scanner or
already recorded (and deduplicated by hash on the next pass).
"""

from __future__ import annotations

import enum
import hashlib
import logging
import os
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from piscan.store import Store

log = logging.getLogger(__name__)

CHUNK = 1024 * 1024
MIN_FREE = 100 * 1024 * 1024


class ScannerState(enum.Enum):
    OFF = "off"
    SCANNING = "scanning"
    IMPORTING = "importing"
    READY = "ready"
    PROBLEM = "problem"


@dataclass(frozen=True)
class ScannerStatus:
    state: ScannerState
    message: str
    done: int = 0
    total: int = 0


class MountError(Exception):
    pass


class DeviceProbe:
    def __init__(self, device: Path, sys_block: Path = Path("/sys/class/block")):
        self.device = device
        self.sys_block = sys_block

    def present(self) -> bool:
        # exists() follows the symlink, so a dangling by-id link counts as absent.
        return self.device.exists()

    def size(self) -> int:
        """Size in 512-byte sectors, or 0 if unreadable (which is also how a scan looks)."""
        try:
            # The node name (sda1, sdb1...) can change between plugs, so resolve every time.
            name = self.device.resolve().name
            return int((self.sys_block / name / "size").read_text().strip())
        except (OSError, ValueError):
            return 0


class Mounter:
    def __init__(self, mountpoint: Path, run=subprocess.run):
        self.mountpoint = mountpoint
        self._run = run

    def _cmd(self, args: list[str]) -> None:
        try:
            proc = self._run(args, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            raise MountError(f"{args[0]} failed: {e}") from e
        if proc.returncode != 0:
            detail = (proc.stderr or "").strip() or f"exit {proc.returncode}"
            raise MountError(f"{args[0]} failed: {detail}")

    def mount(self) -> None:
        self._cmd(["mount", str(self.mountpoint)])

    def unmount(self) -> None:
        self._cmd(["sync"])
        self._cmd(["umount", str(self.mountpoint)])


def is_complete_jpeg(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            if f.read(2) != b"\xff\xd8":
                return False
            f.seek(-2, 2)
            return f.read(2) == b"\xff\xd9"
    except OSError:
        # Seeking before the start of a 0 or 1 byte file lands here too.
        return False


class PassOutcome(enum.Enum):
    OK = "ok"
    STORAGE_FULL = "storage_full"
    MEDIA_LOST = "media_lost"


@dataclass(frozen=True)
class PassResult:
    outcome: PassOutcome
    imported: int = 0
    deduped: int = 0
    skipped: int = 0
    failed: int = 0
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome is PassOutcome.OK and self.failed == 0


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _copy_fsync(src: Path, dst: Path) -> None:
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while chunk := fin.read(CHUNK):
            fout.write(chunk)
        fout.flush()
        os.fsync(fout.fileno())


def _list_scans(flash_root: Path) -> list[Path]:
    folder = flash_root / "DOXIE" / "JPEG"
    try:
        entries = list(folder.iterdir())
    except FileNotFoundError:
        return []
    return sorted(
        (
            p
            for p in entries
            if p.suffix.lower() == ".jpg" and not p.name.startswith(".") and p.is_file()
        ),
        key=lambda p: p.name,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def run_pass(
    flash_root: Path,
    store: Store,
    on_progress: Callable[[int, int], None],
    min_free: int = MIN_FREE,
    now: Callable[[], datetime] = _utc_now,
) -> PassResult:
    files = _list_scans(flash_root)
    total = len(files)
    imported = deduped = skipped = failed = 0
    on_progress(0, total)

    def result(outcome: PassOutcome, message: str = "") -> PassResult:
        return PassResult(outcome, imported, deduped, skipped, failed, message)

    for done, src in enumerate(files):
        if store.free_bytes() < min_free:
            log.error("Pi storage below %d bytes, stopping pass", min_free)
            return result(PassOutcome.STORAGE_FULL, "Pi storage full")

        tmp: Path | None = None
        try:
            if not is_complete_jpeg(src):
                log.info("skip %s: not a complete JPEG yet", src.name)
                skipped += 1
                continue

            tmp = store.new_tmp_path()
            _copy_fsync(src, tmp)
            sha = _sha256(tmp)
            if _sha256(src) != sha:
                log.warning("skip %s: source changed during copy", src.name)
                skipped += 1
                continue

            try:
                page = store.add_page(tmp, sha, now())
            except Exception:
                # Typically Pillow failing on the thumbnail. Source stays on the scanner.
                log.exception("failed %s: could not record page %s", src.name, sha)
                failed += 1
                continue

            if page is None:
                log.info("dedupe %s: %s already recorded", src.name, sha)
                deduped += 1
            else:
                log.info("imported %s as %s", src.name, sha)
                imported += 1
            # Recorded (or already was), so the scanner's copy is no longer needed.
            src.unlink()
        except OSError as e:
            log.error("I/O error on %s: %s", src.name, e)
            return result(PassOutcome.MEDIA_LOST, f"Lost contact with scanner storage: {e}")
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
            on_progress(done + 1, total)

    return result(PassOutcome.OK)


class Ingest:
    def __init__(
        self,
        probe: DeviceProbe,
        mounter: Mounter,
        store: Store,
        interval: float = 2.0,
    ):
        self.probe = probe
        self.mounter = mounter
        self.store = store
        self.interval = interval
        self._lock = threading.Lock()
        self._status = ScannerStatus(ScannerState.OFF, "Scanner off / unplugged")
        self._last_size: int | None = None  # None means the device was absent last tick
        self._problem: str | None = None

    def status(self) -> ScannerStatus:
        with self._lock:
            return self._status

    def _set(self, status: ScannerStatus) -> None:
        with self._lock:
            self._status = status

    def tick(self) -> None:
        if not self.probe.present():
            self._last_size = None
            self._problem = None
            self._set(ScannerStatus(ScannerState.OFF, "Scanner off / unplugged"))
            return

        size = self.probe.size()
        previous = self._last_size
        self._last_size = size
        if size == 0:
            self._set(ScannerStatus(ScannerState.SCANNING, "Scanning..."))
            return

        if previous is None or previous == 0:
            self._do_pass()
        self._set(self._idle_status())

    def _idle_status(self) -> ScannerStatus:
        if self._problem:
            return ScannerStatus(ScannerState.PROBLEM, f"Problem: {self._problem}")
        return ScannerStatus(ScannerState.READY, "Ready")

    def _progress(self, done: int, total: int) -> None:
        self._set(
            ScannerStatus(
                ScannerState.IMPORTING, f"Importing... {done} of {total}", done, total
            )
        )

    def _do_pass(self) -> None:
        self._set(ScannerStatus(ScannerState.IMPORTING, "Importing..."))
        try:
            self.mounter.mount()
        except MountError as e:
            log.error("mount failed: %s", e)
            self._problem = str(e)
            return

        problem: str | None = None
        try:
            res = run_pass(self.mounter.mountpoint, self.store, self._progress)
            log.info(
                "pass finished: %s, %d imported, %d deduped, %d skipped, %d failed",
                res.outcome.value,
                res.imported,
                res.deduped,
                res.skipped,
                res.failed,
            )
            if res.outcome is not PassOutcome.OK:
                problem = res.message
            elif res.failed:
                problem = f"{res.failed} page(s) could not be imported"
        except Exception as e:
            log.exception("pass crashed")
            problem = f"import failed: {e}"
        finally:
            try:
                self.mounter.unmount()
            except MountError as e:
                log.error("unmount failed: %s", e)
                problem = problem or str(e)
        self._problem = problem

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("ingest tick failed")
            stop.wait(self.interval)
