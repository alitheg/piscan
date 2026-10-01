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
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from piscan.store import Store

log = logging.getLogger(__name__)

CHUNK = 1024 * 1024
MIN_FREE = 100 * 1024 * 1024
RETRY_INTERVAL = 60.0


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
        # A failed sync must not stop us trying to unmount.
        sync_error: MountError | None = None
        try:
            self._cmd(["sync"])
        except MountError as e:
            log.error("%s", e)
            sync_error = e
        self._cmd(["umount", str(self.mountpoint)])
        if sync_error:
            raise sync_error


def is_complete_jpeg(path: Path) -> bool:
    """False for a short or unfinished file. Real I/O errors propagate (media lost)."""
    with open(path, "rb") as f:
        if f.seek(0, 2) < 4:
            return False
        f.seek(0)
        if f.read(2) != b"\xff\xd8":
            return False
        f.seek(-2, 2)
        return f.read(2) == b"\xff\xd9"


class PassOutcome(enum.Enum):
    OK = "ok"
    STORAGE_FULL = "storage_full"
    MEDIA_LOST = "media_lost"
    PI_WRITE_FAILED = "pi_write_failed"


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


class PiWriteError(Exception):
    """The Pi-side copy failed (disk full, bad SD card), not the scanner media."""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _copy_fsync(src: Path, dst: Path) -> None:
    # Source reads raise plain OSError (media lost); destination failures become PiWriteError.
    with open(src, "rb") as fin:
        try:
            fout = open(dst, "wb")  # noqa: SIM115 - closed by the with below
        except OSError as e:
            raise PiWriteError(str(e)) from e
        with fout:
            while chunk := fin.read(CHUNK):
                try:
                    fout.write(chunk)
                except OSError as e:
                    raise PiWriteError(str(e)) from e
            try:
                fout.flush()
                os.fsync(fout.fileno())
            except OSError as e:
                raise PiWriteError(str(e)) from e


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
        tmp: Path | None = None
        try:
            if not is_complete_jpeg(src):
                log.info("skip %s: not a complete JPEG yet", src.name)
                skipped += 1
                continue

            if store.free_bytes() < min_free:
                log.error("Pi storage below %d bytes, stopping pass", min_free)
                return result(PassOutcome.STORAGE_FULL, "Pi storage full")

            tmp = store.new_tmp_path()
            _copy_fsync(src, tmp)
            try:
                sha = _sha256(tmp)
            except OSError as e:
                raise PiWriteError(str(e)) from e
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
        except PiWriteError as e:
            log.error("Pi-side write error on %s: %s", src.name, e)
            return result(PassOutcome.PI_WRITE_FAILED, f"Pi storage write failed: {e}")
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
        retry_interval: float = RETRY_INTERVAL,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.probe = probe
        self.mounter = mounter
        self.store = store
        self.interval = interval
        self.retry_interval = retry_interval
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._status = ScannerStatus(ScannerState.OFF, "Scanner off / unplugged")
        self._last_size: int | None = None  # None means the device was absent last tick
        self._problem: str | None = None
        # Set after a Pi-side failure (storage full, write error) so the pass is retried on a
        # timer: nothing else would trigger one, and sending drafts frees the space again.
        self._retry_at: float | None = None

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
            self._retry_at = None
            self._set(ScannerStatus(ScannerState.OFF, "Scanner off / unplugged"))
            return

        size = self.probe.size()
        previous = self._last_size
        self._last_size = size
        if size == 0:
            self._set(ScannerStatus(ScannerState.SCANNING, "Scanning..."))
            return

        retry_due = self._retry_at is not None and self._monotonic() >= self._retry_at
        if previous is None or previous == 0 or retry_due:
            self._do_pass()
        self._set(self._idle_status())

    def _idle_status(self) -> ScannerStatus:
        if self._problem:
            return ScannerStatus(ScannerState.PROBLEM, self._problem)
        return ScannerStatus(ScannerState.READY, "Ready")

    def _progress(self, done: int, total: int) -> None:
        text = f"Importing... {done} of {total}" if total else "Importing..."
        self._set(ScannerStatus(ScannerState.IMPORTING, text, done, total))

    def _do_pass(self) -> None:
        self._retry_at = None
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
            if res.outcome is PassOutcome.MEDIA_LOST:
                # The scanner likely started a scan mid-pass and the size dip went unseen
                # because tick() was blocked. Re-arm so the next non-zero tick retries.
                self._last_size = 0
            if res.outcome in (PassOutcome.STORAGE_FULL, PassOutcome.PI_WRITE_FAILED):
                self._retry_at = self._monotonic() + self.retry_interval
            elif res.outcome is PassOutcome.OK and (res.imported or res.deduped):
                # A scan written wholly inside this pass never showed as a size dip, so run
                # one more pass. It stops once a pass finds nothing.
                self._last_size = 0
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
