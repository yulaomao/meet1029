#!/usr/bin/env bash
# Generate a private LAN CA and a Meeting AI HTTPS certificate.
#
# The browser microphone API needs HTTPS away from localhost.  This script is
# intentionally self-contained: it does not contact a CA, upload a key, or
# depend on a particular LAN router/DNS provider.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
OUTPUT_DIR="$PROJECT_DIR/.local/certs"
ENV_FILE="$PROJECT_DIR/.env"
PORT="5001"
PRIMARY_IP=""
HOSTNAMES=("localhost")
EXTRA_IPS=()
WRITE_ENV=0
FORCE=0
REGENERATE_CA=0

usage() {
  cat <<'EOF'
Usage: scripts/generate_lan_tls.sh [options]

Generate a private LAN root CA and an HTTPS server certificate for Meeting AI.
The generated public *.cer file is the one to install and trust on each phone.

Options:
  --ip ADDRESS        LAN IP to include in the certificate (repeatable).
                      If omitted, the primary active LAN IP is detected.
  --hostname NAME     DNS name to include in the certificate (repeatable).
  --port PORT         Web port to show in the final LAN URL (default: 5001).
  --output-dir PATH   Certificate output directory (default: .local/certs).
  --write-env         Update this project's .env for HTTPS and LAN listening.
  --force             Replace the server certificate/key, preserving the CA.
  --regenerate-ca     Replace the CA as well (requires --force; phones must
                      install the newly generated root certificate again).
  -h, --help          Show this help.

Examples:
  ./scripts/generate_lan_tls.sh --write-env
  ./scripts/generate_lan_tls.sh --ip 192.168.1.20 --write-env
  ./scripts/generate_lan_tls.sh --ip 10.0.0.8 --hostname meeting.office.local --force --write-env
EOF
}

die() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

detect_lan_ip() {
  python3 - <<'PY'
import socket

try:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.connect(("10.255.255.255", 1))
    print(probe.getsockname()[0])
except OSError:
    pass
finally:
    try:
        probe.close()
    except NameError:
        pass
PY
}

validate_ip() {
  python3 - "$1" <<'PY'
import ipaddress
import sys

try:
    candidate = ipaddress.ip_address(sys.argv[1])
except ValueError:
    raise SystemExit(1)
if candidate.version != 4 or candidate.is_loopback or candidate.is_unspecified:
    raise SystemExit(1)
PY
}

validate_hostname() {
  [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]$ || "$1" =~ ^[A-Za-z0-9]$ ]]
}

append_unique() {
  local candidate="$1"
  shift
  local existing
  for existing in "$@"; do
    [[ "$existing" == "$candidate" ]] && return 0
  done
  return 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ip)
      [[ $# -ge 2 ]] || die "--ip requires an address"
      validate_ip "$2" || die "invalid LAN IPv4 address: $2"
      if ! append_unique "$2" "${EXTRA_IPS[@]-}"; then EXTRA_IPS+=("$2"); fi
      shift 2
      ;;
    --hostname)
      [[ $# -ge 2 ]] || die "--hostname requires a name"
      validate_hostname "$2" || die "invalid hostname: $2"
      if ! append_unique "$2" "${HOSTNAMES[@]}"; then HOSTNAMES+=("$2"); fi
      shift 2
      ;;
    --port)
      [[ $# -ge 2 && "$2" =~ ^[1-9][0-9]{0,4}$ && "$2" -le 65535 ]] || die "invalid port"
      PORT="$2"
      shift 2
      ;;
    --output-dir)
      [[ $# -ge 2 ]] || die "--output-dir requires a path"
      OUTPUT_DIR="$2"
      shift 2
      ;;
    --write-env) WRITE_ENV=1; shift ;;
    --force) FORCE=1; shift ;;
    --regenerate-ca) REGENERATE_CA=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

command -v openssl >/dev/null 2>&1 || die "openssl is required"
command -v python3 >/dev/null 2>&1 || die "python3 is required"
(( REGENERATE_CA == 0 || FORCE == 1 )) || die "--regenerate-ca requires --force"

if [[ ${#EXTRA_IPS[@]} -eq 0 ]]; then
  PRIMARY_IP="$(detect_lan_ip)"
  [[ -n "$PRIMARY_IP" ]] || die "could not detect a LAN IP; pass --ip explicitly"
  validate_ip "$PRIMARY_IP" || die "detected IP is not a usable LAN IPv4 address: $PRIMARY_IP"
  EXTRA_IPS=("$PRIMARY_IP")
else
  PRIMARY_IP="${EXTRA_IPS[0]}"
fi

mkdir -p "$(dirname -- "$OUTPUT_DIR")"
OUTPUT_DIR="$(cd -- "$(dirname -- "$OUTPUT_DIR")" && pwd)/$(basename -- "$OUTPUT_DIR")"
mkdir -p "$OUTPUT_DIR"
chmod 700 "$OUTPUT_DIR"

CA_KEY="$OUTPUT_DIR/meeting-ai-lan-root-ca.key"
CA_CERT="$OUTPUT_DIR/meeting-ai-lan-root-ca.crt"
CA_CER="$OUTPUT_DIR/meeting-ai-lan-root-ca.cer"
SERVER_KEY="$OUTPUT_DIR/meeting-ai-lan-server.key"
SERVER_CSR="$OUTPUT_DIR/meeting-ai-lan-server.csr"
SERVER_CERT="$OUTPUT_DIR/meeting-ai-lan-server.crt"
SERVER_EXT="$OUTPUT_DIR/meeting-ai-lan-server.ext"
CA_CONFIG="$OUTPUT_DIR/meeting-ai-lan-ca.conf"

if (( REGENERATE_CA )); then
  rm -f -- "$CA_KEY" "$CA_CERT" "$CA_CER" "$SERVER_KEY" "$SERVER_CSR" "$SERVER_CERT" "$SERVER_EXT" "$CA_CONFIG" "$OUTPUT_DIR/meeting-ai-lan-root-ca.srl"
fi

if [[ -e "$SERVER_KEY" || -e "$SERVER_CERT" ]]; then
  (( FORCE )) || die "server certificate already exists; use --force to replace it"
  rm -f -- "$SERVER_KEY" "$SERVER_CSR" "$SERVER_CERT" "$SERVER_EXT"
fi

if [[ -e "$CA_KEY" || -e "$CA_CERT" || -e "$CA_CER" ]]; then
  [[ -f "$CA_KEY" && -f "$CA_CERT" && -f "$CA_CER" ]] || die "incomplete CA files; use --force --regenerate-ca to replace them"
  printf 'Reusing existing local root CA: %s\n' "$CA_CERT"
else
  printf 'Generating private local root CA…\n'
  openssl genrsa -out "$CA_KEY" 4096
  printf '%s\n' \
    '[req]' \
    'distinguished_name = distinguished_name' \
    'prompt = no' \
    'x509_extensions = v3_ca' \
    '' \
    '[distinguished_name]' \
    'C = CN' \
    'O = Meeting AI Local' \
    'OU = LAN' \
    'CN = Meeting AI Local LAN Root CA' \
    '' \
    '[v3_ca]' \
    'basicConstraints = critical, CA:true, pathlen:0' \
    'keyUsage = critical, keyCertSign, cRLSign' \
    'subjectKeyIdentifier = hash' > "$CA_CONFIG"
  openssl req -x509 -new -sha256 -key "$CA_KEY" -out "$CA_CERT" -days 3650 -config "$CA_CONFIG"
  openssl x509 -in "$CA_CERT" -outform der -out "$CA_CER"
fi

printf '%s\n' \
  'authorityKeyIdentifier = keyid,issuer' \
  'basicConstraints = critical, CA:false' \
  'keyUsage = critical, digitalSignature, keyEncipherment' \
  'extendedKeyUsage = serverAuth' \
  'subjectAltName = @alternative_names' \
  '' \
  '[alternative_names]' > "$SERVER_EXT"

index=1
for hostname in "${HOSTNAMES[@]}"; do
  printf 'DNS.%d = %s\n' "$index" "$hostname" >> "$SERVER_EXT"
  ((index += 1))
done
printf 'IP.%d = 127.0.0.1\n' "$index" >> "$SERVER_EXT"
((index += 1))
for ip in "${EXTRA_IPS[@]}"; do
  printf 'IP.%d = %s\n' "$index" "$ip" >> "$SERVER_EXT"
  ((index += 1))
done

printf 'Generating HTTPS server certificate for %s…\n' "$PRIMARY_IP"
openssl genrsa -out "$SERVER_KEY" 2048
openssl req -new -sha256 -key "$SERVER_KEY" -out "$SERVER_CSR" \
  -subj "/C=CN/O=Meeting AI Local/OU=LAN/CN=$PRIMARY_IP"
openssl x509 -req -sha256 -in "$SERVER_CSR" -CA "$CA_CERT" -CAkey "$CA_KEY" -CAcreateserial \
  -out "$SERVER_CERT" -days 825 -extfile "$SERVER_EXT"
chmod 600 "$CA_KEY" "$SERVER_KEY"
chmod 644 "$CA_CERT" "$CA_CER" "$SERVER_CERT" "$SERVER_EXT" "$CA_CONFIG"
openssl verify -CAfile "$CA_CERT" "$SERVER_CERT" >/dev/null

set_env_value() {
  local key="$1"
  local value="$2"
  local temporary
  temporary="$(mktemp "${ENV_FILE}.tmp.XXXXXX")"
  if [[ -f "$ENV_FILE" ]]; then
    awk -v key="$key" -v value="$value" '
      index($0, key "=") == 1 {
        if (!replaced) print key "=" value
        replaced = 1
        next
      }
      { print }
      END { if (!replaced) print key "=" value }
    ' "$ENV_FILE" > "$temporary"
  else
    printf '%s=%s\n' "$key" "$value" > "$temporary"
  fi
  mv "$temporary" "$ENV_FILE"
}

if (( WRITE_ENV )); then
  [[ -f "$ENV_FILE" ]] || die "--write-env requires an existing .env; copy .env.example and set its database/secret values first"
  set_env_value "HOST" "0.0.0.0"
  set_env_value "TLS_CERT_FILE" "$SERVER_CERT"
  set_env_value "TLS_KEY_FILE" "$SERVER_KEY"
  set_env_value "TLS_CA_CERT_FILE" "$CA_CERT"
  set_env_value "SESSION_COOKIE_SECURE" "1"
  set_env_value "INTERNAL_LIVE_HOST" "127.0.0.1"
  set_env_value "INTERNAL_LIVE_SCHEME" "https"
  set_env_value "INTERNAL_LIVE_CA_FILE" "$CA_CERT"
  printf 'Updated HTTPS settings in %s\n' "$ENV_FILE"
fi

printf '\nReady.\n'
printf 'Phone URL: https://%s:%s\n' "$PRIMARY_IP" "$PORT"
printf 'Install and trust on each phone: %s\n' "$CA_CER"
printf 'Server certificate: %s\n' "$SERVER_CERT"
printf 'Private keys stay only on this computer: %s, %s\n' "$CA_KEY" "$SERVER_KEY"
if (( ! WRITE_ENV )); then
  printf '\nTo configure this project automatically, rerun with --write-env.\n'
fi
printf 'Restart the Web and speech Worker after changing .env.\n'
