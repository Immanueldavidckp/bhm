#!/usr/bin/env bash
# Add/replace an MQTT user in mosquitto/passwd (hashed). Prints the generated password once.
#   ./add_user.sh <username> [password]
set -euo pipefail
cd "$(dirname "$0")"
USER="${1:?usage: $0 <username> [password]}"
PASS="${2:-$(openssl rand -base64 24)}"
touch mosquitto/passwd
docker pull -q eclipse-mosquitto:2.0.20 >/dev/null
docker run --rm -v "$PWD/mosquitto:/m" eclipse-mosquitto:2.0.20 \
  sh -c "mosquitto_passwd -b /m/passwd '$USER' '$PASS' && chmod 0644 /m/passwd" \
  && chown "$(id -u):$(id -g)" mosquitto/passwd 2>/dev/null || true
echo "$USER: $PASS"
