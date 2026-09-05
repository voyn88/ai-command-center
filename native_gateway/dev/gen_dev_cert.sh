#!/bin/sh
# Generate a self-signed localhost certificate for local gateway development.
# Usage: gen_dev_cert.sh <output-dir>
set -eu
OUT="${1:?usage: gen_dev_cert.sh <output-dir>}"
mkdir -p "$OUT"
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
  -keyout "$OUT/gateway-dev-key.pem" -out "$OUT/gateway-dev-cert.pem" \
  -days 30 -nodes -subj "/CN=localhost" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
chmod 600 "$OUT/gateway-dev-key.pem"
echo "Dev TLS material written to $OUT (valid 30 days, localhost only)."
