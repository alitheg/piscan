# piscan - task breakdown

Spec (binding authority): `docs/superpowers/specs/2026-10-01-piscan-design.md`. Read the sections named in each task. This file only fixes task boundaries and the interfaces between tasks.

## Global Constraints

- Python **3.11+** (Raspberry Pi OS bookworm ships 3.11). `src/` layout, package `piscan`, built with hatchling. Pure Python wheel.
- Runtime deps: `fastapi`, `uvicorn`, `jinja2`, `python-multipart`, `httpx`, `pillow`, `img2pdf`, `pikepdf`. Dev: `pytest`, `ruff`. Nothing else without a reason in the report.
- Tests: pytest, hardware-free, no network. Synthetic JPEGs generated with Pillow - never commit real scans.
- Every filesystem path (data dir, mountpoint, sysfs root, device path) must be injectable so tests run in `tmp_path`.
- Comments and docs: UK English, regular hyphens (-) never em-dashes, sparse comments that explain *why*. Match the voice in `~/.claude/alastair-voice-style-guide.md` for comments (direct, no fluff).
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- `ruff check` and `pytest` must pass before committing.

## Task 1: Project scaffold, config, CI

Spec sections: Architecture, Configuration.

- `pyproject.toml` (hatchling, deps above, console script `piscan = "piscan.cli:main"`), `src/piscan/__init__.py` with `__version__`, `.gitignore` (include `.superpowers/`, venvs, caches, `dist/`), minimal `README.md` (one paragraph + link to spec; Task 8 fills it in).
- `src/piscan/config.py`: frozen dataclass `Config` with `paperless_url: str`, `paperless_token: str`, `scanner_device: Path`, `mountpoint: Path`, `web_port: int = 8080`, `data_dir: Path = Path("/var/lib/piscan")`. `load_config(path: Path) -> Config` reads the TOML layout in the spec (`[paperless] url/token`, `[scanner] device/mountpoint`, `[web] port`, optional `[storage] data_dir`), defaulting mountpoint `/mnt/doxie` and device to the spec's by-id path. Clear `ConfigError` on missing url/token. Strip a trailing `/` from the URL.
- `src/piscan/cli.py`: `main(argv=None)` with argparse subcommands `serve` and `check`, both taking `--config` (default `/etc/piscan.toml`). Stubs that load the config and exit 0 for now - Task 6 implements them.
- `.github/workflows/ci.yml`: on push and PR, Python 3.11 and 3.13, `pip install -e .[dev]`, `ruff check`, `pytest`.
- Tests for config loading (full file, defaults, missing token error, trailing slash).

## Task 2: Store

Spec sections: Storage, Ingest step 5 (rename + insert semantics).

`src/piscan/store.py`, class `Store(data_dir: Path)`. Creates `data_dir/piscan.db` (WAL) and `data_dir/pages/`. Thread-safe (one connection guarded by a lock, `check_same_thread=False`).

Dataclasses `Page(id, sha256, path, thumb_path, rotation, arrived_at, draft_id, position)` and `Draft(id, created_at, status, pages: list[Page], paperless_task_id, paperless_document_id, error, sent_at)`. Statuses are the strings `inbox`, `sending`, `sent`, `failed`.

Methods (raise `KeyError` for unknown ids):
- `has_sha(sha256) -> bool`
- `add_page(tmp_file: Path, sha256: str, arrived_at: datetime) -> Page | None` - renames `tmp_file` to `pages/<sha>.jpg` (same filesystem, atomic), writes thumbnail `pages/<sha>.thumb.jpg` (long side 300 px, use Pillow `draft()` for speed), then inserts the page and a new one-page draft (`created_at = arrived_at`) in one transaction. If the sha is already recorded, delete `tmp_file` and return `None`. Re-importing the same content over an orphan file (file present, no row) must work.
- `new_tmp_path() -> Path` - a unique temp file path inside `pages/` (so the rename is same-filesystem).
- `list_drafts(statuses: Iterable[str]) -> list[Draft]` ordered by `created_at`, pages by `position`.
- `get_draft(id) -> Draft`
- `merge(draft_ids: list[int]) -> int` - all pages move into the earliest-created draft, ordered by `arrived_at`, positions renumbered; the other drafts are deleted. Only `inbox`/`failed` drafts can be merged (else `ValueError`).
- `split(page_id) -> int` - page moves to a new draft (`created_at` = page's `arrived_at`); renumber the old draft.
- `move_page(page_id, delta: int)` - swap with neighbour, no-op at the ends.
- `rotate(page_id)` - `(rotation + 90) % 360`.
- `delete_page(page_id)` and `delete_draft(draft_id)` - remove files too; deleting the last page deletes the draft.
- `set_sending(draft_id)`, `set_task(draft_id, task_id)`, `mark_sent(draft_id, document_id: int | None, now)` (deletes page files and page rows' files, keeps the draft row with `sent_at`), `mark_failed(draft_id, error)`, `reset_to_inbox(draft_id)`.
- `purge_sent(older_than: timedelta, now)` - delete `sent` drafts older than that.
- `free_bytes() -> int` (disk free in `data_dir`).

Tests: every method, plus an invariant test that random sequences of merge/split/move/delete never lose or duplicate a page.

## Task 3: Ingest

Spec sections: Ingest (all), Scanner status, Failures (storage, mount, media removed).

`src/piscan/ingest.py`:
- `ScannerState` enum: `OFF`, `SCANNING`, `IMPORTING`, `READY`, `PROBLEM`. Frozen dataclass `ScannerStatus(state, message: str, done: int = 0, total: int = 0)`.
- `DeviceProbe(device: Path, sys_block: Path = Path("/sys/class/block"))` - `present() -> bool`, `size() -> int` (512-byte sectors from `<sys_block>/<resolved name>/size`, 0 if unreadable).
- `Mounter(mountpoint: Path, run=subprocess.run)` - `mount()` runs `mount <mountpoint>` (fstab `user` entry), `unmount()` runs `umount <mountpoint>`; `sync` before unmount. Raise `MountError` on failure.
- `is_complete_jpeg(path) -> bool` (starts `FFD8`, ends `FFD9`).
- `run_pass(flash_root: Path, store: Store, on_progress: Callable[[int,int],None], min_free: int = 100*1024*1024, now=datetime.now) -> PassResult` - steps 1-6 of the spec over `flash_root/DOXIE/JPEG/*.JPG` (case-insensitive glob, filename order, ignore everything else). Copy with fsync into `store.new_tmp_path()`, re-hash source, `store.add_page`, then delete source. Stop with a storage-full result when `store.free_bytes() < min_free`. An `OSError` mid-pass aborts the pass (result says media lost) without deleting anything not yet recorded.
- `Ingest(probe, mounter, store, interval=2.0)` - `tick()` implements the trigger rules (start a pass when the device appears, or size returns after being 0) and the status table; `status() -> ScannerStatus`; `run(stop: threading.Event)` loops `tick()`. A pass is always mount -> run_pass -> unmount, unmount attempted even on failure. Mount failure -> `PROBLEM` with the error, retried on the next trigger.
- Tests with a fake probe/mounter and a temp "flash" folder: happy path, truncated JPEG left in place, hash mismatch (source changes during copy), duplicate content, reused filename, low disk, OSError mid-pass, a simulated crash after `add_page` but before source delete (next pass dedupes and deletes), status transitions including OFF -> READY -> SCANNING (size 0) -> IMPORTING -> READY.

## Task 4: PDF, Paperless client, sender

Spec sections: Sending, Failures (Paperless rows).

- `src/piscan/pdf.py`: `build_pdf(pages: list[tuple[Path, int]]) -> bytes` - `img2pdf` from the original JPEGs (no re-encode), then `pikepdf` sets each page's `/Rotate` to the given rotation.
- `src/piscan/paperless.py`: `PaperlessClient(url, token, transport: httpx.BaseTransport | None = None, timeout=30)`; `upload(pdf: bytes, filename: str) -> str` (POST `/api/documents/post_document/` multipart field `document`, `Authorization: Token <token>`, returns the task UUID from the JSON string body); `task(task_id) -> TaskResult(state: "pending"|"success"|"failure", document_id: int | None, error: str | None)` (GET `/api/tasks/?task_id=`; Paperless returns a list; map `SUCCESS`/`FAILURE`, anything else pending; `related_document` is the doc id, `result` the error text); `ping() -> bool` (GET `/api/` with auth, 2xx). Raise `PaperlessError` with a human-readable message on connection errors/non-2xx ("can't reach Paperless", "Paperless rejected the token", ...).
- `src/piscan/sender.py`: `Sender(store, client, clock=datetime.now, poll_interval=3.0, task_timeout=timedelta(minutes=15))`. `enqueue(draft_id)` (marks `sending`, puts on a queue), `retry(draft_id)` (only `failed`), `recover()` (on start: `sending` drafts with a task id resume polling, without one go back to `inbox`), `run(stop: threading.Event)` single worker, one draft at a time: build PDF, upload, `set_task`, poll until success/failure/timeout -> `mark_sent` / `mark_failed`. Filename `piscan-<draft created_at %Y%m%d-%H%M%S>-<id>.pdf`. Also a `health()` -> `bool | None` updated by `check_health()` (callers run it every 60 s).
- Tests: PDF page count, `/Rotate` values, embedded image streams byte-identical to the source JPEGs; client against `httpx.MockTransport` (upload, task pending/success/failure, 401, connection error); sender success, failure keeps pages, timeout, connection error, recovery of `sending` with and without task id. Drive the worker deterministically (e.g. a `process_one()` method the loop calls) rather than with real sleeps.

## Task 5: Web UI

Spec sections: Web UI, Sending (draft states shown).

`src/piscan/web/` with `app.py`, `templates/`, `static/`. `create_app(store, sender, scanner_status: Callable[[], ScannerStatus], paperless_health: Callable[[], bool | None], paperless_url: str) -> FastAPI`.

- Vendor htmx 2.x minified into `static/htmx.min.js` (pin the version in a comment; no CDN at runtime - the Pi may be offline from the internet).
- Routes: `GET /` (full page), `GET /fragments/status`, `GET /fragments/drafts` (polled every 2 s by htmx), `GET /drafts/{id}` (draft view), `POST /merge` and `POST /delete` (form field `draft_id` repeated), `POST /drafts/{id}/send`, `POST /send-all` (inbox drafts in on-screen order), `POST /drafts/{id}/retry`, `POST /pages/{id}/rotate|up|down|split|delete`, `GET /pages/{id}/thumb` and `/pages/{id}/image`. Actions respond with the updated fragment for htmx, or redirect for non-htmx requests.
- Phone-first layout per the spec's sketch: status bar (scanner state + Paperless dot + draft count), draft cards with thumbnails rotated via CSS, tick boxes, action bar shown when something is ticked, *Send all*, collapsed *Recently sent (24 h)* with a link to `<paperless_url>/documents/<id>/details`. Delete asks for confirmation (`hx-confirm`). Failed drafts show the error and *Retry*.
- Tests with `TestClient` and a real `Store` in `tmp_path` plus fake sender/status callables: each route's effect, 404s for unknown ids, send-all order, status fragment text for each `ScannerState`.

## Task 6: Wiring, CLI, systemd unit

Spec sections: Architecture, Configuration, Failures (logging), Install step 7 (the check).

- `cli.py`: `piscan serve` builds `Store`, `DeviceProbe`, `Mounter`, `Ingest`, `PaperlessClient`, `Sender`; calls `sender.recover()`; runs ingest and sender workers in daemon threads, a housekeeping thread (health check every 60 s, `purge_sent(24h)` hourly); serves the app with uvicorn on `0.0.0.0:<port>`; stops threads cleanly on SIGTERM. Logging to stderr (journald picks it up) with module names.
- `piscan check`: loads config, pings Paperless, prints a clear OK/failure line, exit code 0/1. Used by the installer.
- `packaging/piscan.service`: `User=piscan`, `ExecStart=/opt/piscan/venv/bin/piscan serve --config /etc/piscan.toml`, `Restart=on-failure`, `StateDirectory=piscan`, sensible hardening that still allows running `mount`/`umount` via the fstab `user` option (do not set `NoNewPrivileges`, since `mount` is setuid).
- Tests: CLI argument handling, `check` with a mocked client (both outcomes), and a smoke test that `serve`'s wiring builds an app and starts/stops the threads with fakes (no real uvicorn bind).

## Task 7: Installer and release workflow

Spec sections: Install and release.

- `install.sh` (bash, `set -euo pipefail`, shellcheck clean), steps 1-7 of the spec. Options: `--version X.Y.Z`, `--wheel PATH` (install a local wheel instead of downloading - for testing), `--no-systemd` (skip systemd and fstab-dependent steps, for container tests). GitHub repo `alitheg/piscan`. Prompts read from `/dev/tty`; if there's no tty and no config exists, fail with a message explaining how to create `/etc/piscan.toml`. Idempotent: second run with the same version changes nothing; never overwrites `/etc/piscan.toml`; fstab line added once (match on the device path). Config file mode 0640 owned `root:piscan` (it holds the token).
- `.github/workflows/release.yml`: on tag `v*`, build the wheel (`python -m build`), check the tag matches `__version__`, create the GitHub release with the wheel attached.
- `tests/installer/` with a script that runs the installer in a `debian:bookworm` container (docker) with `--wheel` and `--no-systemd`: fresh install, second run reports no changes, config preserved, fstab line present once. Document how to run it; it's not part of the default pytest run.
- Run shellcheck and the container test; report results.

## Task 8: README and hardware checklist

Spec sections: Hardware, Install and release, Testing (hardware checklist).

- `README.md`: what it does, hardware (Zero 2 W, OTG, 2.5 A supply, 64-bit Pi OS Lite), getting a Paperless API token, the install one-liner (gist URL placeholder `GIST_URL` until Task 7's gist exists - note it), upgrading/pinning, where config/data/logs live, troubleshooting (scanner flat battery behaviour, `journalctl -u piscan`), and the hardware test checklist from the spec as a checklist.
- UK English, hyphens, in Alastair's voice.
