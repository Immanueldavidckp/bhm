#!/usr/bin/env bash
# Create a private CA, a broker certificate and one client certificate per gateway.
#
#   ./gen_certs.sh <broker-host>[,<more names/IPs>...] [client-id ...]
#   e.g. ./gen_certs.sh mqtt.example.com bhm-gw-lab1
#        ./gen_certs.sh 10.5.52.104,localhost,mosquitto bhm-gw-lab1 bhm-ingest
# The first name is the certificate CN; every name/IP goes into subjectAltName.
#
# Output in ./certs:  ca.crt ca.key  server.crt server.key  <gateway>.crt <gateway>.key
# Keep ca.key offline/private. Copy ca.crt + <gateway>.crt/.key to the gateway only.
set -euo pipefail
cd "$(dirname "$0")"
HOSTS="${1:?usage: $0 <broker-host>[,<more names/IPs>] [client-id ...]}"; shift
CLIENTS=("${@:-bhm-gw-lab1}")
DAYS_CA=3650 DAYS=825
mkdir -p certs && cd certs
umask 077

if [[ ! -f ca.key ]]; then
  openssl ecparam -name prime256v1 -genkey -noout -out ca.key
  openssl req -x509 -new -key ca.key -sha256 -days $DAYS_CA -subj "/CN=BHM Root CA" -out ca.crt
fi

SAN=""
IFS=, read -ra NAMES <<< "$HOSTS"
for h in "${NAMES[@]}"; do
  if [[ $h =~ ^[0-9.]+$ ]]; then SAN+="${SAN:+,}IP:$h"; else SAN+="${SAN:+,}DNS:$h"; fi
done
openssl ecparam -name prime256v1 -genkey -noout -out server.key
openssl req -new -key server.key -subj "/CN=${NAMES[0]}" -out server.csr
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
