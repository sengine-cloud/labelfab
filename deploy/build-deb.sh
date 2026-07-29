#!/usr/bin/env bash
# Build one labelfab-agent .deb. Runs as root in a debian container with the repo at
# /src:
#
#   docker run --rm --platform linux/amd64 -v "$PWD:/src" -v "$PWD/dist:/out" \
#     -e VERSION=1.2.3 -e ARCH=amd64 debian:bookworm /src/deploy/build-deb.sh
#
# It ships a fully self-contained CPython built from source (to include AF_BLUETOOTH)
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
PY_VER="${PY_VER:-3.12.4}"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends ca-certificates curl build-essential \
    libssl-dev zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev wget \
    libncursesw5-dev xz-utils tk-dev libxml2-dev libxmlsec1-dev libffi-dev liblzma-dev \
    libbluetooth-dev >/dev/null

# nfpm as a .deb for this arch.
curl -fsSL -o /tmp/nfpm.deb \
    "https://github.com/goreleaser/nfpm/releases/download/v${NFPM_VERSION}/nfpm_${NFPM_VERSION}_${ARCH}.deb"
apt-get install -y /tmp/nfpm.deb >/dev/null

echo "building CPython (${PY_VER}) from source..."
mkdir -p /opt/labelfab
cd /tmp
curl -fsSL -O "https://www.python.org/ftp/python/${PY_VER}/Python-${PY_VER}.tar.xz"
tar -xf "Python-${PY_VER}.tar.xz"
cd "Python-${PY_VER}"
./configure --prefix=/opt/labelfab/python --enable-optimizations >/dev/null
make -j$(nproc) >/dev/null
make install >/dev/null

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

# Sanity: verify that the compiled Python has AF_BLUETOOTH support
"$PY" -c "import socket; print(f'AF_BLUETOOTH is available: {hasattr(socket, \"AF_BLUETOOTH\")}')"
"$PY" -c "import socket; assert hasattr(socket, 'AF_BLUETOOTH'), 'AF_BLUETOOTH is missing!'"

mkdir -p /out
cd /src
VERSION="$VERSION" ARCH="$ARCH" nfpm package -f nfpm.yaml -p deb -t /out
ls -la /out/*.deb
