# piscan - design

Date: 1 October 2026
Status: approved in conversation, awaiting spec review

## Goal

Feed paper into a Doxie Go X2, then from a phone tidy the scanned pages into documents and send them to Paperless-ngx - no computer, no Doxie software.

Success looks like: scan a stack, open the Pi's web page on a phone, merge/rotate as needed, tap *Send all*, and the documents appear in Paperless. The scanner's flash ends up empty and nothing is lost if the Pi reboots part way through.

## Context: the scanner

Established by interrogating the real device (see project memory `doxie-go-x2-usb-identity`):

- USB `2740:0004` ("Apparent Doxie Go", firmware `v6011`), plain USB mass storage (`usb-storage`), two SCSI LUNs:
  - LUN 0 - internal flash, SCSI vendor `S2Flash`, 501 MB, one FAT32 partition labelled `DOXIE`. This is where scans go.
    `by-id`: `usb-S2Flash_USB_Mass_Storage_012345115962181711-0:0-part1`
  - LUN 1 - SD card slot, SCSI vendor `SCAN2PC`, 0 bytes with no card. Out of scope.
- Scans are written as `DOXIE/JPEG/IMG_NNNN.JPG` - JPEG, 912x1264, tagged 300 dpi, quality 80, about 130 KB.
- The scanner's clock is unset, so file timestamps are 2010-05-01. Never use them.
- Scanning works while connected over USB. During the write the flash's size drops to 0 and returns about 7 s later (a media change).
- Raw scans can come out rotated 90 degrees, with a black strip past the paper edge and show-through from the reverse.
- The flash also holds `DOXIE/README.TXT`, old macOS metadata and an unexplained `A0100452.exe`. All ignored.
- The battery is weak. It needs a reliable USB supply to stay up.

## Scope

In:
- A Pi Zero 2 W with the Doxie permanently attached, acting as a bridge to a separate Paperless-ngx server.
- A web inbox on the Pi: each page arrives as its own draft. Merge, split, reorder, rotate, delete, send, send all.
- Deleting each page from the scanner after a verified copy.
- An installer (`curl | sudo bash` from a gist) that installs or upgrades from the latest GitHub release.

Out:
- OCR, cropping, deskewing or other image processing on the Pi. Paperless does OCR, deskew and orientation detection.
- Title/tag entry on the Pi. Paperless matching handles that.
- Authentication and remote access. Home LAN only, no login.
- The SD card slot and the Doxie's Wi-Fi.
- Setting up Paperless-ngx itself.

Possible later change: rotate A4 portrait pages by default, if test scans show they consistently arrive sideways.

## Hardware

- Raspberry Pi Zero 2 W, Raspberry Pi OS Lite 64-bit (needed for prebuilt `pikepdf` arm64 wheels).
- Doxie on the Zero's USB data port via an OTG adapter.
- A 2.5 A supply (or a powered hub). The Doxie charges from the Pi, and a weak supply brings back the brownouts we saw while interrogating it.

## Architecture

One Python package, `piscan`, run as one systemd service under a dedicated `piscan` system user. Four modules, each testable without the others:

| Module | Responsibility | Depends on |
|---|---|---|
| `ingest` | Watch for the Doxie's flash, mount, copy and verify new JPEGs, delete them from the scanner, unmount. Reports scanner status. | `store`, `mount`/`umount` |
| `store` | SQLite database and the page image folder. The only thing that touches either. | nothing |
| `sender` | Build a PDF from a draft, upload to Paperless, follow the task to completion. | `store`, Paperless API |
| `web` | FastAPI, Jinja templates and htmx. Inbox and draft actions. | `store`, `sender`, `ingest` status |

Stack: Python 3 (Pi OS's version), FastAPI + uvicorn, Jinja2, htmx (vendored, no build step), `img2pdf`, `pikepdf`, Pillow, `httpx`. SQLite via the standard library.

### Configuration

`/etc/piscan.toml`:

```toml
[paperless]
url = "https://paperless.example.lan"
token = "..."

[scanner]
device = "/dev/disk/by-id/usb-S2Flash_USB_Mass_Storage_012345115962181711-0:0-part1"
mountpoint = "/mnt/doxie"

[web]
port = 8080
```

## Storage (`store`)

SQLite at `/var/lib/piscan/piscan.db`, WAL mode.

- `drafts`: `id`, `created_at`, `status` (`inbox`, `sending`, `sent`, `failed`), `paperless_task_id`, `paperless_document_id`, `error`, `sent_at`
- `pages`: `id`, `sha256` (unique), `path`, `rotation` (0/90/180/270), `arrived_at` (Pi clock), `draft_id`, `position`

Page images live at `/var/lib/piscan/pages/<sha256>.jpg`, with thumbnails (about 300 px) alongside. Images are never modified - rotation is only a number in the database, applied at send time.

Rules:
- An imported page becomes a new one-page draft.
- Merge moves the pages of the selected drafts into the first one (arrival order) and deletes the now-empty drafts. The drafts are joined end to end in arrival order, each keeping its own page order.
- Split moves one page out into a new draft. Reorder changes `position`.
- Deleting the last page of a draft deletes the draft.
- After Paperless confirms a document, its page files are deleted. The draft row stays as `sent` for 24 h (for the *Recently sent* list), then is purged.

## Ingest (`ingest`)

### Mounting

An `fstab` entry lets the `piscan` user mount the flash, matched on the full `by-id` device path, so a random USB stick never matches:

```
/dev/disk/by-id/usb-S2Flash_USB_Mass_Storage_012345115962181711-0:0-part1  /mnt/doxie  vfat  noauto,user,noexec,nodev,nosuid,uid=piscan,gid=piscan,flush  0  0
```

No desktop automounter is installed on Pi OS Lite, so nothing competes for the device.

### Triggering

Every 2 s, check whether the device exists and its size is non-zero. Start a pass when the device appears, or when its size returns after dropping to 0. Polling covers "already plugged in at boot" without udev rules.

### A pass

Mount, process, unmount. The flash is never left mounted, because the scanner swaps the media while it writes and a FAT filesystem left mounted across that risks corruption.

For each `DOXIE/JPEG/*.JPG` (case-insensitive), in filename order. Everything else on the flash is ignored.

1. Check it's a complete JPEG (starts `FFD8`, ends `FFD9`). If not, skip it and try again next pass.
2. Check free space on the Pi. Below 100 MB, stop the pass and report *Pi storage full*.
3. Copy to a temporary file in the pages folder, `fsync`, hash (SHA-256).
4. Re-hash the source. On mismatch, discard the copy and try again next pass.
5. Rename the copy to `<sha256>.jpg` (atomic, same filesystem) and make the thumbnail, then insert the page and its new draft in one database transaction. If the hash is already in the database (crash between 5 and 6), skip the insert. A crash between the rename and the insert leaves an orphan file with no row - the next pass re-imports the same content over it and inserts the row.
6. Delete the source from the flash.

Then `sync` and unmount.

The source is only deleted once the Pi's copy is on disk, verified and recorded, so a crash at any point leaves the page either safe or due to be re-imported (and deduplicated by hash). Keying on the hash also makes the Doxie's reuse of `IMG_0001.JPG` harmless.

If the media disappears mid-pass (I/O errors), abort, unmount if possible and wait for the next trigger. Nothing unsafe has been deleted.

### Scanner status

`ingest` keeps a status in memory for the web UI:

| Observed | Status |
|---|---|
| No device | *Scanner off / unplugged* |
| Device present, size 0 | *Scanning...* |
| Pass running | *Importing... n of m* |
| Present, idle | *Ready* |
| Last pass failed | *Problem: <reason>* |

"Size 0 means scanning" is inferred from a single scan. The hardware test checklist verifies it. If it proves unreliable, that state is shown as *Busy...* instead.

## Web UI (`web`)

One phone-first page, served on the LAN with no login.

```
+-------------------------------+
| o Ready              3 drafts |   status bar, refreshes every 2 s
+-------------------------------+
| [ ] [img][img]  2 pages 14:03 |   draft card
|                   [Send] [..] |
| [ ] [img]       1 page  14:04 |
|                   [Send] [..] |
+-------------------------------+
| [Merge selected]  [Delete]    |   shown when drafts are ticked
| [Send all]                    |
+-------------------------------+
| > Recently sent (24 h)        |
+-------------------------------+
```

- The status bar and new arrivals update via htmx polling every 2 s. No websockets.
- Tick drafts, then *Merge* or *Delete* (deleting asks for confirmation).
- The draft view (`..`) shows pages larger. Tap a page to rotate it 90 degrees, ↑/↓ to reorder, *Split* to move a page into its own draft, or delete a page.
- Thumbnails are rotated with CSS.
- *Send* sends one draft. *Send all* sends every inbox draft in on-screen order. Merges are kept, so "merge 1+2, merge 3+4, send all" and "merge 1+2, send, merge 3+4, send" both give two documents.
- Each draft shows its state (`sending`, `failed: <reason>` with *Retry*).
- The status bar also shows Paperless reachability, checked every 60 s.

## Sending (`sender`)

A single background worker sends drafts one at a time (the Zero has 512 MB).

1. Mark the draft `sending`.
2. Build a PDF with `img2pdf` from the original JPEGs (no re-encoding). Set each page's `/Rotate` with `pikepdf`.
3. `POST /api/documents/post_document/` with token auth. Store the returned task ID.
4. Poll `GET /api/tasks/?task_id=<id>`:
   - `SUCCESS`: mark `sent`, store the document ID (for a link to Paperless), delete the page files.
   - `FAILURE`: mark `failed` with Paperless's reason (e.g. duplicate). Keep the pages. *Retry* resends.
   - No result after 15 min: mark `failed` with "unknown - check Paperless". If the first upload did succeed, Paperless's duplicate check rejects a retry.

After a restart, drafts still `sending` with a task ID are re-checked, not re-uploaded. Ones without a task ID go back to `inbox`.

## Failures

| Situation | Behaviour |
|---|---|
| Paperless unreachable | Send fails with "can't reach Paperless", *Retry* offered. Health dot in the status bar. |
| Task never reports back | Failed after 15 min, as above. |
| Pi storage below 100 MB | Ingest stops, *Pi storage full* status. Pages stay on the scanner. |
| Flash won't mount repeatedly | *Problem* status with the error. Never `fsck` or reformat automatically. |
| Media removed mid-pass | Abort, retry on next trigger. |
| Power cut | WAL plus the copy/verify/record/delete order means no loss. Worst case a page is re-imported and deduplicated. |

Logs go to journald (`journalctl -u piscan`).

## Install and release

### Release

GitHub repo `piscan` (public). CI (GitHub Actions) runs the tests on every push. Pushing a `vX.Y.Z` tag builds a pure-Python wheel and attaches it to a GitHub release.

### Installer

`install.sh`, kept in the repo as the source of truth and published to a gist (gist URL created during implementation), run with `curl -fsSL <gist-raw-url> | sudo bash`. Idempotent: running it again upgrades.

1. Check this is Raspberry Pi OS (warn if not 64-bit). `apt install python3-venv`.
2. Find the latest release via the GitHub API (or `--version X.Y.Z` to pin or roll back) and download its wheel.
3. Create the `piscan` system user, `/var/lib/piscan`, `/mnt/doxie`, and a venv at `/opt/piscan`. Install the wheel into the venv.
4. Add the `fstab` line if it isn't already there.
5. If `/etc/piscan.toml` doesn't exist, prompt for the Paperless URL and token (reading from `/dev/tty`, since stdin is the pipe) and write it. Never overwrite an existing config.
6. Install `piscan.service`, `daemon-reload`, `enable --now` (or `restart` on upgrade).
7. Check: service active, web port responding, Paperless URL and token valid. Print the URL to open.

## Testing

Unit tests (pytest), hardware-free:
- `ingest` takes mount/unmount and the flash folder as injectable dependencies, so passes run against a temp folder. Covers simulated crashes between each step, truncated JPEGs, hash mismatch, duplicate hashes, reused filenames, low disk space, and the status state machine (including size-0 transitions).
- `store`: merge, split, reorder and delete never lose or duplicate a page.
- `sender`: a fake Paperless (`httpx.MockTransport`) for success, failure, duplicate, timeout and restart recovery. The PDF test checks page count, `/Rotate` values, and that the embedded JPEG streams are byte-identical to the originals.
- `web`: FastAPI `TestClient` against each action.
- Test images are synthetic JPEGs generated with Pillow. No real scans in the repo.

Installer: fresh install, a no-change second run, and an upgrade, in an arm64 container first. systemd and `fstab` behaviour checked on the real Pi.

Hardware checklist (manual, on the real Zero and Doxie):
- [ ] Several scans, checking the *Scanning...* signal each time, including scans in quick succession
- [ ] Start a scan during an import
- [ ] Reboot mid-import
- [ ] End to end to Paperless, including a merged multi-page document and rotated pages
- [ ] Check how A4 portrait pages arrive, to decide on default rotation
- [ ] Scanner stays charged and connected on the chosen power supply
