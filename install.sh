#!/usr/bin/env bash
# piscan installer for Raspberry Pi OS Lite (bookworm).
#
#   curl -fsSL <raw-url> | sudo bash
#   curl -fsSL <raw-url> | sudo bash -s -- --version 0.1.0
#
# Re-running upgrades. Options:
#   --version X.Y.Z   install that release instead of the latest (also rolls back)
#   --wheel PATH      install a local wheel instead of downloading (testing)
#   --no-systemd      skip systemctl calls and the final check (container tests)
#
# The whole script lives in main() so bash has read all of it before anything
# runs. That matters when it arrives on stdin through a pipe.
set -euo pipefail

REPO="alitheg/piscan"
VENV_DIR="/opt/piscan/venv"
CONFIG="/etc/piscan.toml"
UNIT_DEST="/etc/systemd/system/piscan.service"
DATA_DIR="/var/lib/piscan"
FSTAB="/etc/fstab"
DEFAULT_DEVICE="/dev/disk/by-id/usb-S2Flash_USB_Mass_Storage_012345115962181711-0:0-part1"
DEFAULT_MOUNTPOINT="/mnt/doxie"
DEFAULT_PORT="8080"

WANT_VERSION=""
LOCAL_WHEEL=""
USE_SYSTEMD=1
WORKDIR=""
CHANGES=()
PKG_CHANGED=0
UNIT_CHANGED=0
CONFIG_CREATED=0
# Set from the config (or the defaults) once it is known.
DEVICE="$DEFAULT_DEVICE"
MOUNTPOINT="$DEFAULT_MOUNTPOINT"
PORT="$DEFAULT_PORT"

log() { printf '==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
changed() { CHANGES+=("$*"); }

cleanup() { [[ -n "$WORKDIR" ]] && rm -rf "$WORKDIR"; return 0; }

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --version)
                [[ $# -ge 2 ]] || die "--version needs a value"
                WANT_VERSION="${2#v}"
                shift 2
                ;;
            --wheel)
                [[ $# -ge 2 ]] || die "--wheel needs a path"
                LOCAL_WHEEL="$2"
                shift 2
                ;;
            --no-systemd)
                USE_SYSTEMD=0
                shift
                ;;
            -h | --help)
                sed -n '2,11p' "${BASH_SOURCE[0]:-$0}" 2>/dev/null || true
                exit 0
                ;;
            *) die "unknown option: $1" ;;
        esac
    done
    if [[ -n "$LOCAL_WHEEL" ]]; then
        [[ -f "$LOCAL_WHEEL" ]] || die "wheel not found: $LOCAL_WHEEL"
        [[ -z "$WANT_VERSION" ]] || die "--wheel and --version are mutually exclusive"
    fi
    if [[ -n "$WANT_VERSION" && ! "$WANT_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        die "--version must look like 1.2.3"
    fi
}

# Step 1
check_system() {
    [[ $EUID -eq 0 ]] || die "run as root, e.g. curl -fsSL <url> | sudo bash"
    command -v dpkg >/dev/null || die "this installer needs a Debian-based system (Raspberry Pi OS)"

    if [[ -r /etc/os-release ]] && ! grep -qi 'raspbian\|raspberry' /etc/os-release \
        && [[ ! -e /etc/rpi-issue ]]; then
        warn "this does not look like Raspberry Pi OS - carrying on anyway"
    fi
    if [[ "$(dpkg --print-architecture)" != "arm64" ]]; then
        warn "not a 64-bit OS ($(dpkg --print-architecture)); piscan is only tested on 64-bit Pi OS"
    fi

    # Fail before changing anything if the config can't be created later.
    if [[ ! -f "$CONFIG" ]] && ! have_tty; then
        cat >&2 <<EOF
ERROR: $CONFIG does not exist and there is no terminal to ask for the values.
Create it first, then re-run the installer (it fixes the ownership and mode):

  sudo install -m 0600 /dev/null $CONFIG
  sudoedit $CONFIG

with contents like:

  [paperless]
  url = "https://paperless.example.lan"
  token = "your-api-token"

  [scanner]
  device = "$DEFAULT_DEVICE"
  mountpoint = "$DEFAULT_MOUNTPOINT"

  [web]
  port = $DEFAULT_PORT

Or run the installer from an interactive shell so it can prompt you.
EOF
        exit 1
    fi

    if ! dpkg -s python3-venv >/dev/null 2>&1; then
        log "Installing python3-venv"
        apt-get update -qq </dev/null
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv </dev/null
        changed "installed python3-venv"
    fi
    python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
        || die "python 3.11 or newer is required"
}

# Step 2: sets WHEEL_PATH and TARGET_VERSION. Downloads only if the version
# differs from what is installed, so an up-to-date re-run does no network
# transfer beyond the release lookup.
resolve_wheel() {
    if [[ -n "$LOCAL_WHEEL" ]]; then
        WHEEL_PATH="$LOCAL_WHEEL"
        WHEEL_URL=""
    elif [[ -n "$WANT_VERSION" ]]; then
        WHEEL_URL="https://github.com/${REPO}/releases/download/v${WANT_VERSION}/piscan-${WANT_VERSION}-py3-none-any.whl"
        WHEEL_PATH=""
    else
        log "Looking up the latest release"
        local json="$WORKDIR/release.json"
        curl -fsSL -H 'Accept: application/vnd.github+json' \
            "https://api.github.com/repos/${REPO}/releases/latest" -o "$json" \
            || die "could not query GitHub for the latest release"
        WHEEL_URL="$(python3 - "$json" <<'PY'
import json
import sys

with open(sys.argv[1]) as f:
    release = json.load(f)
for asset in release.get("assets", []):
    if asset.get("name", "").endswith(".whl"):
        print(asset["browser_download_url"])
        break
PY
)"
        [[ -n "$WHEEL_URL" ]] || die "the latest release has no .whl asset"
        WHEEL_PATH=""
    fi

    local name
    name="$(basename "${WHEEL_PATH:-$WHEEL_URL}")"
    # Wheel filenames are name-version-tags.whl.
    TARGET_VERSION="$(printf '%s' "$name" | cut -d- -f2)"
    [[ "$TARGET_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+ ]] \
        || die "could not read a version from wheel name: $name"
    WHEEL_NAME="$name"
}

installed_version() {
    [[ -x "$VENV_DIR/bin/python" ]] || return 0
    "$VENV_DIR/bin/python" -c 'import importlib.metadata as m; print(m.version("piscan"))' \
        2>/dev/null || true
}

# Step 3
install_package() {
    if ! getent passwd piscan >/dev/null; then
        log "Creating the piscan system user"
        useradd --system --user-group --no-create-home --home-dir "$DATA_DIR" \
            --shell /usr/sbin/nologin piscan
        changed "created user piscan"
    fi

    if [[ ! -d "$DATA_DIR" ]]; then
        install -d -o piscan -g piscan -m 0750 "$DATA_DIR"
        changed "created $DATA_DIR"
    fi
    if [[ ! -d "$MOUNTPOINT" ]]; then
        install -d -m 0755 "$MOUNTPOINT"
        changed "created $MOUNTPOINT"
    fi

    if [[ ! -x "$VENV_DIR/bin/python" ]]; then
        log "Creating the virtualenv at $VENV_DIR"
        install -d -m 0755 "$(dirname "$VENV_DIR")"
        python3 -m venv "$VENV_DIR"
        changed "created virtualenv $VENV_DIR"
    fi

    local current
    current="$(installed_version)"
    if [[ "$current" == "$TARGET_VERSION" ]]; then
        log "piscan $current is already installed, skipping pip"
        return 0
    fi

    if [[ -z "$WHEEL_PATH" ]]; then
        log "Downloading $WHEEL_NAME"
        WHEEL_PATH="$WORKDIR/$WHEEL_NAME"
        curl -fsSL "$WHEEL_URL" -o "$WHEEL_PATH" \
            || die "could not download $WHEEL_URL (does that release exist?)"
    fi
    log "Installing piscan $TARGET_VERSION"
    "$VENV_DIR/bin/pip" install --quiet --disable-pip-version-check \
        --upgrade --prefer-binary "$WHEEL_PATH" </dev/null
    PKG_CHANGED=1
    if [[ -z "$current" ]]; then
        changed "installed piscan $TARGET_VERSION"
    else
        changed "changed piscan $current -> $TARGET_VERSION"
    fi
}

# Reads device, mountpoint and port from an existing config so a hand-edited
# file is respected by the fstab step and the final check.
load_config_values() {
    [[ -f "$CONFIG" ]] || return 0
    local out
    out="$(python3 - "$CONFIG" "$DEFAULT_DEVICE" "$DEFAULT_MOUNTPOINT" "$DEFAULT_PORT" <<'PY'
import sys
import tomllib

path, device, mountpoint, port = sys.argv[1:5]
with open(path, "rb") as f:
    raw = tomllib.load(f)
scanner = raw.get("scanner", {})
print(scanner.get("device", device))
print(scanner.get("mountpoint", mountpoint))
print(raw.get("web", {}).get("port", port))
PY
)" || die "$CONFIG is not valid TOML - fix it and re-run"
    {
        IFS= read -r DEVICE
        IFS= read -r MOUNTPOINT
        IFS= read -r PORT
    } <<<"$out"
}

# Step 4
ensure_fstab() {
    [[ -f "$FSTAB" ]] || : >"$FSTAB"
    if grep -qF -- "$DEVICE" "$FSTAB"; then
        return 0
    fi
    log "Adding the scanner to $FSTAB"
    # Make sure the last line ends in a newline before appending.
    if [[ -s "$FSTAB" && -n "$(tail -c1 "$FSTAB")" ]]; then
        echo >>"$FSTAB"
    fi
    printf '%s  %s  vfat  noauto,user,noexec,nodev,nosuid,uid=piscan,gid=piscan,flush  0  0\n' \
        "$DEVICE" "$MOUNTPOINT" >>"$FSTAB"
    changed "added scanner line to $FSTAB"
}

# TOML basic strings need backslash and double quote escaped.
toml_escape() {
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    printf '%s' "$s"
}

have_tty() {
    # Opening /dev/tty fails with ENXIO when there is no controlling terminal.
    (: </dev/tty) 2>/dev/null
}

# Step 5
ensure_config() {
    if [[ -f "$CONFIG" ]]; then
        # Never rewrite the contents, but the service user must be able to read it.
        local mode owner
        mode="$(stat -c '%a' "$CONFIG")"
        owner="$(stat -c '%U:%G' "$CONFIG")"
        if [[ "$mode" != "640" || "$owner" != "root:piscan" ]]; then
            chown root:piscan "$CONFIG"
            chmod 0640 "$CONFIG"
            changed "fixed permissions on $CONFIG (was $owner $mode)"
        fi
        return 0
    fi

    local url="" token=""
    log "Paperless-ngx settings (written to $CONFIG)"
    while [[ -z "$url" ]]; do
        printf 'Paperless URL (e.g. https://paperless.example.lan): ' >/dev/tty
        IFS= read -r url </dev/tty
        url="${url%"${url##*[![:space:]]}"}"
        url="${url#"${url%%[![:space:]]*}"}"
        if [[ -n "$url" && ! "$url" =~ ^https?:// ]]; then
            printf 'That needs to start with http:// or https://\n' >/dev/tty
            url=""
        fi
    done
    while [[ -z "$token" ]]; do
        printf 'Paperless API token (not shown): ' >/dev/tty
        IFS= read -rs token </dev/tty
        printf '\n' >/dev/tty
    done

    # Create with the final mode first so the token is never world-readable.
    install -m 0640 -o root -g piscan /dev/null "$CONFIG"
    cat >"$CONFIG" <<EOF
[paperless]
url = "$(toml_escape "$url")"
token = "$(toml_escape "$token")"

[scanner]
device = "$DEFAULT_DEVICE"
mountpoint = "$DEFAULT_MOUNTPOINT"

[web]
port = $DEFAULT_PORT
EOF
    CONFIG_CREATED=1
    changed "created $CONFIG"
}

# Step 6. The unit comes out of the installed package rather than from the
# repo at the release tag: it is then always the version that matches the
# code in the venv, and the installer needs no second download.
ensure_unit() {
    local src
    src="$("$VENV_DIR/bin/python" - <<'PY'
from importlib.resources import files

print(files("piscan") / "piscan.service")
PY
)"
    [[ -f "$src" ]] || die "the installed package has no piscan.service ($src)"
    if [[ -f "$UNIT_DEST" ]] && cmp -s "$src" "$UNIT_DEST"; then
        return 0
    fi
    install -d -m 0755 "$(dirname "$UNIT_DEST")"
    install -m 0644 "$src" "$UNIT_DEST"
    UNIT_CHANGED=1
    changed "installed $UNIT_DEST"
}

start_service() {
    [[ $USE_SYSTEMD -eq 1 ]] || return 0
    if [[ $UNIT_CHANGED -eq 1 ]]; then
        systemctl daemon-reload
    fi
    if ! systemctl is-enabled --quiet piscan 2>/dev/null; then
        log "Enabling and starting piscan"
        systemctl enable --now piscan
        changed "enabled and started piscan"
    elif [[ $PKG_CHANGED -eq 1 || $UNIT_CHANGED -eq 1 || $CONFIG_CREATED -eq 1 ]] \
        || ! systemctl is-active --quiet piscan; then
        log "Restarting piscan"
        systemctl restart piscan
        changed "restarted piscan"
    fi
}

# Step 7
final_check() {
    [[ $USE_SYSTEMD -eq 1 ]] || return 0
    local ok=1
    log "Checking the service"
    for _ in $(seq 1 20); do
        systemctl is-active --quiet piscan && break
        sleep 1
    done
    if systemctl is-active --quiet piscan; then
        echo "  service: active"
    else
        echo "  service: NOT active (journalctl -u piscan -n 50)"
        ok=0
    fi

    local up=0
    for _ in $(seq 1 20); do
        if python3 - "$PORT" <<'PY'
import sys
import urllib.request

try:
    urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/", timeout=2)
except Exception as e:
    # Any HTTP response at all means the server is up.
    sys.exit(0 if hasattr(e, "code") else 1)
PY
        then
            up=1
            break
        fi
        sleep 1
    done
    if [[ $up -eq 1 ]]; then
        echo "  web: responding on port $PORT"
    else
        echo "  web: no response on port $PORT"
        ok=0
    fi

    if out="$("$VENV_DIR/bin/piscan" check --config "$CONFIG" 2>&1)"; then
        echo "  paperless: $out"
    else
        echo "  paperless: $out"
        ok=0
    fi
    return $((1 - ok))
}

summary() {
    echo
    if [[ ${#CHANGES[@]} -eq 0 ]]; then
        echo "Nothing to do - piscan $TARGET_VERSION is installed and configured."
    else
        echo "Changes:"
        local c
        for c in "${CHANGES[@]}"; do
            echo "  - $c"
        done
    fi
}

main() {
    parse_args "$@"
    WORKDIR="$(mktemp -d)"
    trap cleanup EXIT

    check_system
    resolve_wheel
    install_package
    load_config_values
    ensure_fstab
    ensure_config
    # The config may have just been created, so read it again.
    load_config_values
    ensure_unit
    start_service
    summary

    if [[ $USE_SYSTEMD -eq 1 ]]; then
        if final_check; then
            echo
            echo "Open http://$(hostname).local:${PORT}/ on your phone."
        else
            echo
            echo "Something above failed. Logs: journalctl -u piscan -n 50" >&2
            exit 1
        fi
    fi
}

main "$@"
