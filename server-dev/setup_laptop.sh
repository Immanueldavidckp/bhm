#!/usr/bin/env bash
# One-time setup of the BHM server stack on this laptop (safe to re-run).
#   ./setup_laptop.sh <laptop-ip>       e.g. ./setup_laptop.sh 10.5.52.104
# Re-run with the new IP if the laptop's address changes (only the broker cert is renewed).
set -euo pipefail
cd "$(dirname "$0")"
IP="${1:?usage: $0 <laptop-ip the board connects to>}"
umask 077
mkdir -p secrets

# broker certificate valid for the laptop IP, localhost and the compose service name
./gen_certs.sh "$IP,localhost,mosquitto" bhm-gw-lab1 bhm-ingest >/dev/null
echo "certs: server cert for $IP,localhost,mosquitto; client certs bhm-gw-lab1, bhm-ingest"

# MQTT users (password kept in secrets/<user>.password, created once)
for u in bhm-gw-lab1 bhm-ingest; do
  if [[ ! -s secrets/$u.password ]]; then
    openssl rand -base64 24 | tr -d '\n' > "secrets/$u.password"
    ./add_user.sh "$u" "$(cat "secrets/$u.password")" >/dev/null
    echo "mqtt user $u: password in secrets/$u.password"
  fi
done

# database passwords (created once: MySQL keeps them after its first start)
if [[ ! -s .env ]]; then
  cat > .env <<ENV
UID=$(id -u)
GID=$(id -g)
MYSQL_ROOT_PASSWORD=$(openssl rand -hex 16)
MYSQL_PASSWORD=$(openssl rand -hex 16)
MQTT_INGEST_PASSWORD=$(cat secrets/bhm-ingest.password)
ENV
  echo ".env written (database passwords)"
fi
echo "next: docker compose up -d --build"
