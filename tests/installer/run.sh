#!/usr/bin/env bash
# Runs install.sh in a debian:bookworm container. Not part of the pytest run.
#
#   tests/installer/run.sh
#
# Needs docker and the build module (pip install -e '.[dev]'). The default
# platform is linux/arm64, which is what the Pi runs; on an x86_64 host that
# needs qemu binfmt and is slow. Use PLATFORM=linux/amd64 for a quick run.
# Network access is needed inside the container (apt and PyPI).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
platform="${PLATFORM:-linux/arm64}"
python="${PYTHON:-$repo/.venv/bin/python}"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/wheels"

echo "==> Building the current wheel"
"$python" -m build --wheel --outdir "$work/wheels" "$repo" >/dev/null

# A second wheel with a higher version, to exercise the upgrade path.
echo "==> Building a higher-version wheel for the upgrade test"
bump="$work/bump"
mkdir "$bump"
cp -r "$repo/src" "$repo/pyproject.toml" "$repo/README.md" "$bump/"
current="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$bump/src/piscan/__init__.py")"
IFS=. read -r major minor patch <<<"$current"
next="$major.$minor.$((patch + 100))"
sed -i "s/^__version__ = .*/__version__ = \"$next\"/" "$bump/src/piscan/__init__.py"
mkdir "$work/wheels-next"
"$python" -m build --wheel --outdir "$work/wheels-next" "$bump" >/dev/null
echo "    current=$current upgrade=$next"

echo "==> Running in $platform"
docker run --rm --platform "$platform" \
    -e CURRENT="$current" -e NEXT="$next" \
    -v "$repo/install.sh:/test/install.sh:ro" \
    -v "$here/inside.sh:/test/inside.sh:ro" \
    -v "$work/wheels:/wheels:ro" \
    -v "$work/wheels-next:/wheels-next:ro" \
    debian:bookworm bash /test/inside.sh
