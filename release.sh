#!/usr/bin/env bash
# release.sh (C15): build the migratetool wheel from this checkout and print its sha256.
#
#   ./release.sh [OUTDIR]      default OUTDIR: dist/ of the checkout
#
# Needs a python3 with pip (and network or a local index for 'setuptools' when it is not installed). The wheel holds
# the package only; install it on the convert host with the pinned dependencies:
#   /opt/mig-venv/bin/pip install -r requirements-host.txt dist/migratetool-<version>-py3-none-any.whl
# Refuses a dirty tree: a release is a commit.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=${1:-$HERE/dist}
PY=${PYTHON:-python3}
cd "$HERE"
if git -C "$HERE" rev-parse --git-dir >/dev/null 2>&1 && [ -n "$(git -C "$HERE" status --porcelain -- migratetool pyproject.toml)" ]; then
  echo "release.sh: uncommitted changes under migratetool/ or pyproject.toml: commit first" >&2
  exit 1
fi
version=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' migratetool/__init__.py)
want=$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml)
if [ "$version" != "$want" ]; then
  echo "release.sh: migratetool/__init__.py says $version, pyproject.toml $want" >&2
  exit 1
fi
mkdir -p "$OUT"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
"$PY" -m pip wheel --no-deps --no-build-isolation -w "$tmp" "$HERE" >/dev/null 2>"$tmp/err" || {
  "$PY" -m pip wheel --no-deps -w "$tmp" "$HERE" >/dev/null || { cat "$tmp/err" >&2; exit 1; }
}
whl=$(ls "$tmp"/migratetool-"$version"-*.whl)
cp "$whl" "$OUT/"
cd "$OUT"
sha256sum "$(basename "$whl")" | tee "$(basename "$whl").sha256"
echo "commit $(git -C "$HERE" rev-parse --short HEAD 2>/dev/null || echo '?')"
