#!/bin/sh
# Create the service user, wire up directories, and enable-but-do-not-start: a fresh
# install has no printer MAC yet, so starting would just crash-loop in the journal and
# look broken. The operator starts it after `labelfab probe`.
set -e

mkdir -p /var/lib/labelfab /etc/labelfab

if ! getent passwd labelfab >/dev/null 2>&1; then
    adduser --system --group --no-create-home --home /var/lib/labelfab labelfab
fi
# The RFCOMM socket needs the bluetooth group.
if getent group bluetooth >/dev/null 2>&1; then
    adduser labelfab bluetooth >/dev/null 2>&1 || true
fi

chown labelfab:labelfab /var/lib/labelfab
chmod 750 /etc/labelfab

if [ -d /run/systemd/system ]; then
    systemctl daemon-reload || true
    systemctl enable labelfab-agent.service || true
fi

cat <<'EOF'
labelfab-agent installed (enabled, not started).

Next steps:
  1. bluetoothctl            # scan on, pair and trust the D30, note its MAC
  2. edit /etc/labelfab/agent.toml   # set device.mac and the [mqtt] section
  3. labelfab probe --mac <MAC> --self-test   # confirm alignment
  4. systemctl start labelfab-agent
EOF

exit 0
