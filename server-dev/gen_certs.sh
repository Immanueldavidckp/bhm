#!/usr/bin/env bash
# Create a private CA, a broker certificate and one client certificate per gateway.
#
#   ./gen_certs.sh <broker-hostname-or-ip> [gateway-id ...]
#   e.g. ./gen_certs.sh mqtt.example.com bhm-gw-lab1
#
# Output in ./certs:  ca.crt ca.key  server.crt server.key  <gateway>.crt <gateway>.key
# Keep ca.key offline/private. Copy ca.crt + <gateway>.crt/.key to the gateway only.
set -euo pipefail
cd "$(dirname "$0")"
HOST="${1:?usage: $0 <broker-hostname-or-ip> [gateway-id ...]}"; shift
CLIENTS=("${@:-bhm-gw-lab1}")
DAYS_CA=3650 DAYS=825
mkdir -p certs && cd certs
umask 077

if [[ ! -f ca.key ]]; then
  openssl ecparam -name prime256v1 -genkey -noout -out ca.key
  openssl req -x509 -new -key ca.key -sha256 -days $DAYS_CA -subj "/CN=BHM Root CA" -out ca.crt
fi

if [[ $HOST =~ ^[0-9.]+$ ]]; then SAN="IP:$HOST"; else SAN="DNS:$HOST"; fi
openssl ecparam -name prime256v1 -genkey -noout -out server.key
openssl req -new -key server.key -subj "/CN=$HOST" -out server.csr
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -sha256 -days $DAYS \
  -extfile <(printf "subjectAltName=%s\nextendedKeyUsage=serverAuth\nkeyUsage=digitalSignature" "$SAN") \
  -out server.crt

for c in "${CLIENTS[@]}"; do
  openssl ecparam -name prime256v1 -genkey -noout -out "$c.key"
  openssl req -new -key "$c.key" -subj "/CN=$c" -out "$c.csr"
  openssl x509 -req -in "$c.csr" -CA ca.crt -CAkey ca.key -CAcreateserial -sha256 -days $DAYS \
    -extfile <(printf "extendedKeyUsage=clientAuth\nkeyUsage=digitalSignature") -out "$c.crt"
done
rm -f ./*.csr
chmod 644 ./*.crt
echo "certificates written to $(pwd)"
