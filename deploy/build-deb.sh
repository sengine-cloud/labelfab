#!/usr/bin/env bash
# Build one labelfab-agent .deb. Runs as root in a debian container with the repo at
# /src:
#
#   docker run --rm --platform linux/amd64 -v "$PWD:/src" -v "$PWD/dist:/out" \
#     -e VERSION=1.2.3 -e ARCH=amd64 debian:bookworm /src/deploy/build-deb.sh
#
# It ships a fully self-contained, relocatable CPython from python-build-standalone
# and installs the app into it. That is the whole point: a plain `python -m venv`
# only references the build box's system python, so its stdlib path (e.g.
# /usr/lib/python3.11) has to exist on the target too -- which breaks the moment the
# workshop box runs a different Python (Ubuntu 24.04 ships 3.12, not 3.11). The
# standalone build carries its own stdlib and resolves its prefix from the binary
# location, so the package works on any glibc Linux and depends only on libc6.
#
# The build container's own python is irrelevant now, so its version does not matter.
set -euo pipefail

: "${VERSION:?set VERSION}"
: "${ARCH:?set ARCH (amd64|arm64)}"
NFPM_VERSION="${NFPM_VERSION:-2.41.1}"
PBS_PY="${PBS_PY:-3.12}"

case "$ARCH" in
    amd64) PBS_ARCH="x86_64" ;;
    arm64) PBS_ARCH="aarch64" ;;
    *) echo "unsupported ARCH: $ARCH" >&2; exit 2 ;;
esac

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends ca-certificates curl >/dev/null

# nfpm as a .deb for this arch.
curl -fsSL -o /tmp/nfpm.deb \
    "https://github.com/goreleaser/nfpm/releases/download/v${NFPM_VERSION}/nfpm_${NFPM_VERSION}_${ARCH}.deb"
apt-get install -y /tmp/nfpm.deb >/dev/null

# Relocatable, self-contained CPython. Pick the newest install_only build for the
# requested minor and arch from the latest python-build-standalone release.
echo "resolving python-build-standalone (${PBS_PY}, ${PBS_ARCH})..."
# The `install_only_stripped` build is the distribution artifact: same self-contained
# interpreter and full stdlib, minus debug symbols -- roughly a third of the size.
# GitHub URL-encodes the '+' in the version tag as %2B in browser_download_url, so the
# pattern must accept either form.
PBS_URL="$(curl -fsSL https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest \
    | grep -oE "https://[^\"]*cpython-${PBS_PY}\.[0-9]+(\+|%2B)[0-9]+-${PBS_ARCH}-unknown-linux-gnu-install_only_stripped\.tar\.gz" \
    | head -1)"
test -n "$PBS_URL" || { echo "no python-build-standalone asset found for ${PBS_PY}/${PBS_ARCH}" >&2; exit 1; }
echo "  $PBS_URL"

mkdir -p /opt/labelfab
curl -fsSL "$PBS_URL" -o /tmp/python.tar.gz
tar -C /opt/labelfab -xzf /tmp/python.tar.gz   # -> /opt/labelfab/python (relocatable)

PY=/opt/labelfab/python/bin/python3
"$PY" -m pip install --no-cache-dir --upgrade pip >/dev/null
# [agent,ble]: the workshop D30 speaks BLE (no SPP record), so bleak ships too.
"$PY" -m pip install --no-cache-dir "/src[agent,ble]" >/dev/null

# Trim build-only bloat (tests, caches) from the shipped interpreter.
find /opt/labelfab/python -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
find /opt/labelfab/python/lib -type d -name test -prune -exec rm -rf {} + 2>/dev/null || true
find /opt/labelfab/python/lib -type d -name tests -prune -exec rm -rf {} + 2>/dev/null || true

# Sanity: the bundled interpreter must run and import the app before we package it.
"$PY" -c "import labelfab.cli, labelfab.agent; print('bundled interpreter OK')"

# Sanity: RFCOMM addressing must work on the bundled interpreter, which is built
# without the BlueZ headers and so has no socket.AF_BLUETOOTH. The address encoding is
# pure and always checkable; actually opening a socket is not, because build hosts
# (and the QEMU arm64 leg) usually have no bluetooth module loaded -- EAFNOSUPPORT
# there says nothing about the package, so only a *wrong* error is fatal.
"$PY" - <<'PYCHECK'
import errno, socket
from labelfab.device import _rfcomm

assert not _rfcomm.has_native_support(), "expected a build without the BlueZ headers"
assert _rfcomm.sockaddr_rc("AA:FD:FD:6B:9F:5F", 1).hex() == "1f005f9f6bfdfdaa0100"
try:
    _rfcomm.socket_rfcomm().close()
    print("RFCOMM addressing OK (kernel bluetooth present)")
except OSError as exc:
    if exc.errno != errno.EAFNOSUPPORT:
        raise
    print("RFCOMM addressing OK (no kernel bluetooth on the build host; encoding verified)")
PYCHECK

mkdir -p /out
cd /src
VERSION="$VERSION" ARCH="$ARCH" nfpm package -f nfpm.yaml -p deb -t /out
ls -la /out/*.deb
