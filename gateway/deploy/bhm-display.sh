#!/bin/sh
# BHM status screen on the LCD. Started by Weston's start-up hook as user weston
# (install.sh sets DEFAULT_DEMO_APPLICATION in /etc/default/demo-launcher to this file).
# Remove /etc/default/demo-launcher to get ST's demo launcher back.
. /etc/profile.d/weston_profile.sh
export PYTHONPATH=/opt/bhm-gateway/lib PYTHONUNBUFFERED=1
while true; do
    python3 -m bhm_gateway display
    sleep 3       # crashed or closed: come back
done
