#!/usr/bin/env bash
# Runs inside the container, started by run.sh. There is no tty here.
# shellcheck disable=SC2016  # single quotes are deliberate: the inner bash expands
set -uo pipefail

INSTALL=/test/install.sh
CONFIG=/etc/piscan.toml
DEVICE="/dev/disk/by-id/usb-S2Flash_USB_Mass_Storage_012345115962181711-0:0-part1"
FAILS=0

pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; FAILS=$((FAILS + 1)); }
check() { # check "description" command...
    local desc="$1"
    shift
    if "$@" >/dev/null 2>&1; then pass "$desc"; else fail "$desc"; fi
}

run_installer() { # writes output to /tmp/out, returns installer exit status
    bash "$INSTALL" --no-systemd "$@" </dev/null >/tmp/out 2>&1
}

wheel() { ls "$1"/*.whl; }

# 1. No config and no tty: must fail early and explain, changing nothing.
if run_installer --wheel "$(wheel /wheels)"; then
    fail "no config, no tty should fail"
else
    pass "no config, no tty fails"
fi
check "failure message names $CONFIG" grep -q "$CONFIG does not exist" /tmp/out
check "failure message shows how to create it" grep -q 'sudoedit' /tmp/out
check "nothing installed by the failed run" test ! -e /opt/piscan

# 2. Fresh install with a pre-made config containing an awkward token.
cat >"$CONFIG" <<'EOF'
[paperless]
url = "https://paperless.example.lan"
token = "sekrit-token"
EOF
chmod 0644 "$CONFIG"
cp "$CONFIG" /tmp/config.orig

if run_installer --wheel "$(wheel /wheels)"; then
    pass "fresh install exits 0"
else
    fail "fresh install exits 0"
    cat /tmp/out
fi
cat /tmp/out
check "piscan user exists" getent passwd piscan
check "user has no login shell" bash -c 'getent passwd piscan | grep -q nologin'
check "data dir owned by piscan" bash -c '[ "$(stat -c %U /var/lib/piscan)" = piscan ]'
check "mountpoint exists" test -d /mnt/doxie
check "venv has piscan" test -x /opt/piscan/venv/bin/piscan
check "piscan imports" /opt/piscan/venv/bin/python -c 'import piscan'
check "unit installed" test -f /etc/systemd/system/piscan.service
check "unit ExecStart matches venv" grep -q '^ExecStart=/opt/piscan/venv/bin/piscan serve' /etc/systemd/system/piscan.service
check "fstab line present once" bash -c "[ \"\$(grep -cF '$DEVICE' /etc/fstab)\" = 1 ]"
check "fstab line has the mount options" grep -q "$DEVICE  /mnt/doxie  vfat  noauto,user,noexec,nodev,nosuid,uid=piscan,gid=piscan,flush  0  0" /etc/fstab
check "config contents untouched" cmp -s "$CONFIG" /tmp/config.orig
check "config fixed to root:piscan 640" bash -c '[ "$(stat -c %U:%G:%a /etc/piscan.toml)" = root:piscan:640 ]'

# 3. Same version again: no changes.
sha_before="$(sha256sum /etc/fstab /etc/systemd/system/piscan.service "$CONFIG" | sha256sum)"
if run_installer --wheel "$(wheel /wheels)"; then pass "second run exits 0"; else fail "second run exits 0"; fi
cat /tmp/out
check "second run reports nothing to do" grep -q '^Nothing to do' /tmp/out
check "second run skipped pip" grep -q 'already installed, skipping pip' /tmp/out
check "second run left files byte-identical" bash -c "[ \"\$(sha256sum /etc/fstab /etc/systemd/system/piscan.service $CONFIG | sha256sum)\" = '$sha_before' ]"
check "fstab line still present once" bash -c "[ \"\$(grep -cF '$DEVICE' /etc/fstab)\" = 1 ]"

# 4. Upgrade to a higher version: package changes, config and fstab survive.
echo 'extra = "kept"' >>"$CONFIG"
cp "$CONFIG" /tmp/config.edited
if run_installer --wheel "$(wheel /wheels-next)"; then pass "upgrade exits 0"; else fail "upgrade exits 0"; fi
cat /tmp/out
check "upgrade reports the version change" grep -q "changed piscan $CURRENT -> $NEXT" /tmp/out
check "venv now at $NEXT" bash -c "[ \"\$(/opt/piscan/venv/bin/python -c 'import importlib.metadata as m; print(m.version(\"piscan\"))')\" = '$NEXT' ]"
check "config preserved across upgrade" cmp -s "$CONFIG" /tmp/config.edited
check "fstab line still present once after upgrade" bash -c "[ \"\$(grep -cF '$DEVICE' /etc/fstab)\" = 1 ]"

# 5. A changed unit file is put back.
echo '# local edit' >>/etc/systemd/system/piscan.service
run_installer --wheel "$(wheel /wheels-next)" || fail "unit repair run exits 0"
check "unit restored from the package" bash -c '! grep -q "local edit" /etc/systemd/system/piscan.service'
check "unit repair is reported" grep -q 'installed /etc/systemd/system/piscan.service' /tmp/out

# 6. A rebuilt wheel with the same version is installed, then a re-run is a no-op.
run_installer --wheel "$(wheel /wheels-rebuild)" || fail "rebuild run exits 0"
cat /tmp/out
check "rebuild is reinstalled" grep -q "reinstalled piscan $NEXT from a different wheel" /tmp/out
check "venv has the rebuilt code" grep -q '# rebuilt' /opt/piscan/venv/lib/python3*/site-packages/piscan/__init__.py
check "venv still at $NEXT" bash -c "[ \"\$(/opt/piscan/venv/bin/python -c 'import importlib.metadata as m; print(m.version(\"piscan\"))')\" = '$NEXT' ]"
run_installer --wheel "$(wheel /wheels-rebuild)" || fail "rebuild re-run exits 0"
check "rebuild re-run skipped pip" grep -q 'already installed, skipping pip' /tmp/out

# 7. Bad options.
check "unknown option rejected" bash -c "! bash $INSTALL --bogus </dev/null"
check "--wheel with a missing file rejected" bash -c "! bash $INSTALL --wheel /nope.whl </dev/null"

echo
if [ "$FAILS" -eq 0 ]; then
    echo "ALL PASSED"
else
    echo "$FAILS FAILED"
    exit 1
fi
