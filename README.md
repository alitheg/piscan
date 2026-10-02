# piscan

A small service for a Raspberry Pi Zero 2 W. It pulls scans off a Doxie Go X2, shows each page as a draft in a phone-friendly web inbox, and sends the finished documents to Paperless-ngx.

The flow: feed paper into the Doxie, open the Pi's web page on your phone, merge, split, reorder or rotate pages as needed, then tap *Send all*. Each page is deleted from the scanner once a verified copy is on the Pi, and a reboot part way through loses nothing.

It does no OCR, cropping or deskewing - Paperless does that. There is no login, so keep it on your home LAN.

The design is in [docs/superpowers/specs/2026-10-01-piscan-design.md](docs/superpowers/specs/2026-10-01-piscan-design.md).

## Hardware

- Raspberry Pi Zero 2 W running Raspberry Pi OS Lite, 64-bit (trixie, which ships Python 3.13). The older bookworm base (Python 3.11) should still work.
- An original Pi Zero W also works, on 32-bit Pi OS Lite (it can't run 64-bit). It's noticeably slower - one ARMv6 core - but piscan is mostly idle. Its wheels come from piwheels, which link against Debian's shared libraries instead of bundling them, so the installer apt-installs those on 32-bit systems. The Build workflow checks that every dependency has a prebuilt wheel for both boards, so nothing gets compiled on the Pi.
- The Doxie on the Zero's USB data port, through an OTG adapter.
- A 2.5 A supply, or a powered hub. The Doxie charges from the Pi, and a weak supply brings on brownouts.
- A separate Paperless-ngx server on the same network.

The Doxie shows up as USB `2740:0004` ("Apparent Doxie Go") and mounts as plain USB mass storage. There are two drives: the internal flash (FAT32, label `DOXIE`, scans at `DOXIE/JPEG/IMG_NNNN.JPG`) and the SD slot. piscan only uses the flash.

## Paperless API token

In Paperless-ngx, open your profile ("My Profile"). There is an API auth token section where you can generate and copy one. Alternatively:

```
curl -d 'username=you&password=secret' https://paperless.example.lan/api/token/
```

## Install

<!-- GIST_URL gets filled in with the raw gist URL after the first release. -->

```
curl -fsSL GIST_URL | sudo bash
```

Run it from an interactive shell. On first run it asks for the Paperless URL and token and writes `/etc/piscan.toml`. With no terminal and no existing config it stops before changing anything and tells you what to create.

The installer:

- installs `python3-venv` if needed
- downloads the wheel from the latest GitHub release
- creates the `piscan` system user, `/var/lib/piscan` and `/mnt/doxie`
- creates a virtualenv at `/opt/piscan/venv` and installs the wheel into it
- adds an `fstab` line so the `piscan` user can mount the scanner
- installs `/etc/systemd/system/piscan.service`, then enables and starts it
- checks the service, the web port and the Paperless token, and prints the URL to open

It is idempotent. Running it again upgrades to the latest release, and restarts the service only if something changed.

### Options

Pass options after `bash -s --`:

```
curl -fsSL GIST_URL | sudo bash -s -- --version 0.1.0
```

- `--version X.Y.Z` installs that release instead of the latest. This is also how you roll back.
- `--wheel PATH` installs a local wheel instead of downloading one (for testing).
- `--no-systemd` skips the systemctl calls and the final check (for container tests).

## Where things live

| What | Where |
|---|---|
| Config | `/etc/piscan.toml` (`root:piscan`, mode 0640) |
| Data (SQLite database, page images) | `/var/lib/piscan` |
| Program | `/opt/piscan/venv` |
| Scanner mountpoint | `/mnt/doxie` |
| Service | `piscan.service` |
| Logs | `journalctl -u piscan` |

An existing config is never overwritten. Only the `[paperless]` url and token are required:

```toml
[paperless]
url = "https://paperless.example.lan"
token = "your-api-token"

[scanner]
device = "/dev/disk/by-id/usb-S2Flash_USB_Mass_Storage_012345115962181711-0:0-part1"
mountpoint = "/mnt/doxie"

[web]
port = 8080

[storage]
data_dir = "/var/lib/piscan"
```

The values shown for `[scanner]`, `[web]` and `[storage]` are the defaults. The installer's `fstab` line is matched on the full `device` path, so a random USB stick never matches. If you change `device` or `mountpoint` by hand, re-run the installer so the `fstab` line follows.

The systemd unit and the installer assume `data_dir` is `/var/lib/piscan`. If you point it somewhere else, create that directory and `chown` it to the `piscan` user by hand.

After editing the config, run `sudo systemctl restart piscan`. To test the config and the Paperless token without restarting anything:

```
/opt/piscan/venv/bin/piscan check --config /etc/piscan.toml
```

`piscan serve --config PATH` is what the service runs. `--config` defaults to `/etc/piscan.toml`.

The web UI listens on the configured port (8080 by default) on all interfaces. The installer prints `http://<hostname>.local:8080/` when it finishes.

## Troubleshooting

Start with `journalctl -u piscan -n 50`.

The scanner's status shows in the bar at the top of the web UI. It reads "Scanning..." while the Doxie is writing a page (the flash briefly disappears from USB while it does that), and a "Problem" status with the error if the flash repeatedly won't mount. piscan never runs `fsck` or reformats anything.

Some things worth knowing about the Doxie:

- A flat battery makes it flash green, fall back to orange and drop off USB within seconds. Charge it switched off for 30-60 minutes, then plug it in again.
- Its clock is unset, so scan files are dated 2010. piscan ignores the timestamps.
- Scanning works while it is plugged into the Pi.

If Paperless is unreachable, the send fails with "can't reach Paperless" and offers a retry. If the Pi's storage drops below 100 MB, ingest stops and pages stay on the scanner until you free some space.

## Hardware test checklist

Manual, on a real Zero and Doxie:

- [ ] Several scans, checking the *Scanning...* signal each time, including scans in quick succession
- [ ] Scan several pages in quick succession, then check the scanner's flash is empty after the last one
- [ ] Start a scan during an import
- [ ] Reboot mid-import
- [ ] End to end to Paperless, including a merged multi-page document and rotated pages
- [ ] Check how A4 portrait pages arrive, to decide on default rotation
- [ ] Scanner stays charged and connected on the chosen power supply

Still to verify on a real Pi after the first release:

- [ ] The installer's systemd path end to end (enable, restart on upgrade, final check)
- [ ] GitHub latest-release lookup and `--version` downloads
- [ ] The interactive config prompt when piped through `curl | sudo bash`
- [ ] The `fstab` mount with `uid=piscan` works for the `piscan` user

## Development

```
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/ruff check
```

Python 3.11 or newer. CI runs the tests and `ruff check` on 3.11 and 3.13 for every push. The tests need no hardware and no network, and use synthetic JPEGs.

The installer has its own test, which is not part of the pytest run. `tests/installer/run.sh` builds the current wheel and a higher-version one, then runs `install.sh` in a `debian:trixie` container: fresh install, a no-change re-run, an upgrade, and config and `fstab` preservation. It needs docker. The default platform is `linux/arm64`, which is slow under emulation on an x86_64 host (about five minutes); `PLATFORM=linux/amd64 tests/installer/run.sh` is quicker. `IMAGE=debian:bookworm tests/installer/run.sh` checks the older base.

### Releases

Set `__version__` in `src/piscan/__init__.py`, commit, then tag `vX.Y.Z` with the same version and push the tag. The release workflow refuses to run if the tag and `__version__` disagree. It builds a pure-Python wheel and attaches it to a GitHub release, which is what the installer fetches.

`install.sh` in this repo is the source of truth. The gist copy is what people run, so update it after changing the script.
