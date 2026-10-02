#!/usr/bin/env bash
# Checks that a piscan wheel's whole dependency tree resolves to prebuilt wheels on a Pi, so the
# installer never falls back to compiling (slow at best on a Zero, and Pi OS has no Python headers).
#
#   scripts/check-pi-wheels.sh dist/piscan-*.whl
#
# TARGETS and PYTHON override the defaults. Each target is name=tag[,tag...] - every wheel tag that
# Pi can install, since pip doesn't work out the older manylinux tags for itself. The defaults cover
# a Pi Zero W (32-bit, armv6l) and a Zero 2 W on 64-bit Pi OS (aarch64, glibc 2.41 on trixie), both
# on trixie's Python 3.13.
set -euo pipefail

wheel="${1:?usage: check-pi-wheels.sh path/to/piscan.whl}"
aarch64_tags="linux_aarch64,manylinux2014_aarch64"
for minor in 17 28 31 34 35 36 38 39 41; do aarch64_tags+=",manylinux_2_${minor}_aarch64"; done
targets="${TARGETS:-armv6l=linux_armv6l aarch64=$aarch64_tags}"
python="${PYTHON:-3.13}"
pip="${PIP:-python3 -m pip}"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

status=0
for target in $targets; do
    name="${target%%=*}"
    platform_args=()
    IFS=, read -ra tags <<<"${target#*=}"
    for tag in "${tags[@]}"; do platform_args+=(--platform "$tag"); done

    echo "==> $name, Python $python"
    if $pip download --quiet --disable-pip-version-check --only-binary=:all: \
        "${platform_args[@]}" --python-version "$python" --implementation cp \
        --extra-index-url https://www.piwheels.org/simple \
        -d "$work/$name" "$wheel"; then
        # Show what would be installed, so a surprising resolution is easy to spot.
        for f in "$work/$name"/*.whl; do
            case "${f##*/}" in piscan-*) ;; *) echo "    ${f##*/}" ;; esac
        done
    else
        echo "FAILED: no prebuilt wheel set for $name" >&2
        status=1
    fi
done
exit $status
