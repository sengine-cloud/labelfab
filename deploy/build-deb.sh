#!/usr/bin/env bash
# Build one labelfab-agent .deb. Meant to run as root inside a debian container whose
# python matches the target box, with the repo at /src:
#
#   docker run --rm --platform linux/amd64 -v "$PWD:/src" -v "$PWD/dist:/out" \
#     -e VERSION=1.2.3 -e ARCH=amd64 debian:bookworm /src/deploy/build-deb.sh
#
# It builds a --copies venv at the final install path so shebangs resolve on the
# target, then packages it with nfpm.
set -euo pipefail

: "${VERSION:?set VERSION}"
: "${ARCH:?set ARCH (amd64|arm64)}"
NFPM_VERSION="${NFPM_VERSION:-2.41.1}"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip ca-certificates curl >/dev/null

# nfpm as a .deb for this arch (its own release assets are named by Go arch names,
# which happen to match Debian's amd64/arm64).
curl -fsSL -o /tmp/nfpm.deb \
    "https://github.com/goreleaser/nfpm/releases/download/v${NFPM_VERSION}/nfpm_${NFPM_VERSION}_${ARCH}.deb"
apt-get install -y /tmp/nfpm.deb >/dev/null

# The venv is built at its install path so the copied interpreter's shebangs are
# correct on the target. --copies avoids symlinks into the build container's python.
python3 -m venv --copies /opt/labelfab/venv
/opt/labelfab/venv/bin/pip install --no-cache-dir --upgrade pip >/dev/null
/opt/labelfab/venv/bin/pip install --no-cache-dir "/src[agent]" >/dev/null
# Trim build-only bloat from the shipped venv.
find /opt/labelfab/venv -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
rm -rf /opt/labelfab/venv/share /opt/labelfab/venv/lib/python*/site-packages/pip/_vendor/*/tests 2>/dev/null || true

mkdir -p /out
cd /src
VERSION="$VERSION" ARCH="$ARCH" nfpm package -f nfpm.yaml -p deb -t /out
ls -la /out/*.deb
