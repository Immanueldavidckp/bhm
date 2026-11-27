#!/usr/bin/env bash
# One-time setup of auto-deploy on the server. Run ON the server:
#   git clone https://github.com/Immanueldavidckp/bhm.git ~/bhm/repo
#   ~/bhm/repo/server-dev/deploy/install.sh
set -euo pipefail
D=$(cd "$(dirname "$0")" && pwd)
# the timer runs an installed copy, so a push cannot change the script that runs sudo;
# re-run install.sh after reviewing a change to deploy/
mkdir -p ~/bhm/bin ~/.config/systemd/user
install -m 755 "$D/bhm-deploy.sh" ~/bhm/bin/bhm-deploy.sh
cp "$D/bhm-deploy.service" "$D/bhm-deploy.timer" ~/.config/systemd/user/
systemctl --user daemon-reload
~/bhm/bin/bhm-deploy.sh      # first run: sets the baseline, deploys nothing
systemctl --user enable --now bhm-deploy.timer
systemctl --user list-timers bhm-deploy.timer --no-pager
