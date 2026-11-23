#!/bin/sh
# Install / upgrade the BHM gateway on the STM32MP135 (run as root from the unpacked bundle).
#   tar xzf bhm-gateway-*.tar.gz && cd bhm-gateway && ./install.sh
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)

python3 -c 'import sys, tomllib, sqlite3, ssl; assert sys.version_info >= (3, 11)' \
  || { echo "need python3 >= 3.11 with sqlite3 and ssl" >&2; exit 1; }

echo "-> /opt/bhm-gateway"
rm -rf /opt/bhm-gateway/lib.new
mkdir -p /opt/bhm-gateway
cp -r "$HERE/lib" /opt/bhm-gateway/lib.new
rm -rf /opt/bhm-gateway/lib
mv /opt/bhm-gateway/lib.new /opt/bhm-gateway/lib
python3 -m compileall -q /opt/bhm-gateway/lib >/dev/null || true

echo "-> /etc/bhm"
install -d -m 0755 /etc/bhm /etc/bhm/certs
[ -f /etc/bhm/bhm.toml ] || install -m 0644 "$HERE/bhm.example.toml" /etc/bhm/bhm.toml
[ -f /etc/bhm/mqtt_password ] || install -m 0600 /dev/null /etc/bhm/mqtt_password
chmod 0600 /etc/bhm/mqtt_password
[ -f /etc/bhm/certs/client.key ] && chmod 0600 /etc/bhm/certs/client.key

echo "-> systemd unit"
install -m 0644 "$HERE/bhm-gateway.service" /etc/systemd/system/bhm-gateway.service
install -m 0644 "$HERE/bhm-sim.service" /etc/systemd/system/bhm-sim.service   # not enabled
systemctl daemon-reload
systemctl enable bhm-gateway.service >/dev/null

# TLS certificate validation and reading timestamps need a correct clock, and boards without
# an RTC battery boot with a wrong one. Use systemd-timesyncd. Not `timedatectl set-ntp true`:
# on OpenSTLinux that enables ntpd (which never synced on our board) and DISABLES timesyncd.
if [ -f /usr/lib/systemd/system/systemd-timesyncd.service ]; then
  systemctl disable --now ntpd.service chronyd.service 2>/dev/null || true
  systemctl enable --now systemd-timesyncd.service
else
  timedatectl set-ntp true 2>/dev/null || true
fi

missing=""
grep -q 'example.com' /etc/bhm/bhm.toml && missing="$missing\n  - edit /etc/bhm/bhm.toml (mqtt.host, site_id, battery ports)"
[ -s /etc/bhm/mqtt_password ] || missing="$missing\n  - put the MQTT password in /etc/bhm/mqtt_password"
for f in ca.crt client.crt client.key; do
  [ -f /etc/bhm/certs/$f ] || missing="$missing\n  - copy $f to /etc/bhm/certs/"
done

if [ -n "$missing" ]; then
  printf "installed, NOT started. Still to do:%b\nthen: systemctl restart bhm-gateway\n" "$missing"
else
  systemctl restart bhm-gateway.service
  echo "started. logs: journalctl -u bhm-gateway -f"
fi
echo "no batteries yet? systemctl start bhm-sim and point the ports at /run/bhm-sim/<name>"
