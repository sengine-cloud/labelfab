#!/bin/sh
# Stop and disable on removal. The user, /etc/labelfab and /var/lib/labelfab are left
# in place so a reinstall keeps the config and spool; purge cleans them if wanted.
set -e

if [ -d /run/systemd/system ]; then
    systemctl stop labelfab-agent.service || true
    systemctl disable labelfab-agent.service || true
fi

exit 0
