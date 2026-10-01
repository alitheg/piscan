# piscan

A small service for a Raspberry Pi that pulls scans off a Doxie Go X2, lets you tidy them up in a web UI, and sends them to Paperless-ngx.

Design: [docs/superpowers/specs/2026-10-01-piscan-design.md](docs/superpowers/specs/2026-10-01-piscan-design.md)

## Install

On Raspberry Pi OS Lite (64-bit, bookworm):

```
curl -fsSL <installer-url> | sudo bash
```

Running it again upgrades. `--version X.Y.Z` pins or rolls back (pass it with `bash -s -- --version X.Y.Z`). The installer asks for the Paperless URL and token on first run and writes `/etc/piscan.toml`; an existing config is never overwritten.

## Testing the installer

`tests/installer/run.sh` runs `install.sh` in a `debian:bookworm` container (fresh install, no-change re-run, upgrade, config and fstab preserved). It needs docker and takes about five minutes under arm64 emulation; `PLATFORM=linux/amd64 tests/installer/run.sh` is faster. It is not part of the pytest run.
