#!/usr/bin/env bash
#
# Issues the certificates the bot API and the dashboard authenticate each other
# with (issue #65).
#
# WHY A PRIVATE CA AT ALL, GIVEN TAILSCALE
# ----------------------------------------
# Because the tunnel is a segmentation control, not an authentication one. If
# transport authentication came from Tailscale, then anything that ends up on
# the tailnet — a second node, a compromised device, a misapplied ACL — can
# talk to the bot API. With mTLS the API answers exactly one certificate, and
# reaching the port without it gets nothing. Two independent things have to go
# wrong instead of one.
#
# WHAT THIS PRODUCES
#   ca.key / ca.pem          the CA. THE KEY NEVER LEAVES THIS MACHINE.
#   bot-api.key / .pem       server cert, goes on the homelab host
#   dashboard.key / .pem     client cert, goes on the IONOS VPS
#
# USAGE
#   ./scripts/gen_bot_api_certs.sh <bot-api-host> [output-dir]
#
#   <bot-api-host> is the name or tailnet IP the dashboard will connect to, and
#   it must match BOT_API_BIND. Pass it exactly as the dashboard will write it
#   in BOT_API_URL, or the handshake fails on hostname verification.
#
# WHERE EACH FILE GOES
#   homelab:  ca.pem, bot-api.pem, bot-api.key   -> BOT_API_CA / _CERT / _KEY
#   VPS:      ca.pem, dashboard.pem, dashboard.key
#                                    -> BOT_API_CA / _CLIENT_CERT / _CLIENT_KEY
#   nowhere:  ca.key
#
# ROTATION
#   Certificates below are valid for 825 days; the CA for 10 years.
#
#   To rotate a leaf (the normal case — a lost VPS, a suspected key leak, or
#   just the expiry coming up), re-run this script and redeploy the affected
#   side. The CA is unchanged, so the two ends can be updated independently and
#   there is no window where they disagree.
#
#   To rotate the CA itself (only if ca.key is believed compromised): generate
#   the new CA and both leaves, put the new ca.pem on BOTH hosts first, then
#   swap the leaf certs, then restart. Doing it in that order means neither
#   side is ever presented a certificate it cannot verify.
#
#   Revocation is deliberately not modelled. With exactly one client there is
#   nothing a CRL would tell the bot that "reissue the CA and both leaves"
#   doesn't say faster, and BOT_API_CLIENT_CN already pins which certificate is
#   acceptable even if a second one exists.
#
set -euo pipefail

# Under Git Bash / MSYS on Windows, an argument starting with "/" is rewritten
# into a Windows path — so "/CN=VRCVerify Internal CA" reaches openssl as
# "C:/Program Files/Git/CN=..." and it rejects the subject. Harmless anywhere
# else, so it is set unconditionally rather than behind a platform test.
export MSYS_NO_PATHCONV=1

HOST="${1:-}"
OUT="${2:-./certs}"

if [ -z "$HOST" ]; then
    echo "usage: $0 <bot-api-host> [output-dir]" >&2
    echo "  e.g. $0 100.64.0.2" >&2
    echo "       $0 vrcverify-bot.tailnet-name.ts.net" >&2
    exit 64
fi

# An IP needs an IP SAN; a name needs a DNS SAN. Getting this wrong is the
# single most common reason an otherwise correct mTLS setup refuses to connect.
if [[ "$HOST" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    SAN="IP:$HOST"
else
    SAN="DNS:$HOST"
fi

mkdir -p "$OUT"
cd "$OUT"

umask 077  # keys are created unreadable to anyone else, from the start

# A CA with no keyUsage is accepted by openssl, by curl, and by the bot's own
# server context -- and rejected by the dashboard, because Python 3.13 turned on
# ssl.VERIFY_X509_STRICT by default in create_default_context(). Under RFC 5280
# strict checking a CA must say keyCertSign, so an extension-free CA fails with
# "CA cert does not include key usage extension" on the client side only.
CA_EXTS=(
    -addext "basicConstraints=critical,CA:TRUE"
    -addext "keyUsage=critical,keyCertSign,cRLSign"
    -addext "subjectKeyIdentifier=hash"
)

echo "==> Certificate authority"
if [ ! -f ca.key ]; then
    openssl genrsa -out ca.key 4096
    openssl req -x509 -new -nodes -key ca.key -sha256 -days 3650 \
        -subj "/CN=VRCVerify Internal CA" "${CA_EXTS[@]}" -out ca.pem
elif ! openssl x509 -in ca.pem -noout -ext keyUsage 2>/dev/null | grep -q "Certificate Sign"; then
    # Re-signing with the SAME key and the SAME subject: the CA certificate only
    # publishes a public key and a set of extensions, so every certificate the
    # old one signed still verifies against the new one. Nothing needs reissuing
    # and the two hosts can be updated in either order.
    echo "    ca.pem predates the keyUsage fix; re-issuing it from the existing key."
    openssl req -x509 -new -nodes -key ca.key -sha256 -days 3650 \
        -subj "/CN=VRCVerify Internal CA" "${CA_EXTS[@]}" -out ca.pem
else
    echo "    ca.key exists; reusing it (rotating leaves only)."
fi

# Real files rather than <(...) process substitution: under Git Bash the latter
# hands openssl.exe a /dev/fd/63 path a Windows binary cannot open.
trap 'rm -f server.ext client.ext bot-api.csr dashboard.csr' EXIT

printf 'subjectAltName=%s\nextendedKeyUsage=serverAuth\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always\n' "$SAN" > server.ext
printf 'extendedKeyUsage=clientAuth\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always\n' > client.ext

echo "==> Server certificate for the bot API ($SAN)"
openssl genrsa -out bot-api.key 4096
openssl req -new -key bot-api.key -subj "/CN=$HOST" -out bot-api.csr
openssl x509 -req -in bot-api.csr -CA ca.pem -CAkey ca.key -CAcreateserial \
    -out bot-api.pem -days 825 -sha256 -extfile server.ext

echo "==> Client certificate for the dashboard"
# The CN here is what BOT_API_CLIENT_CN pins on the bot side. Keep them equal.
openssl genrsa -out dashboard.key 4096
openssl req -new -key dashboard.key -subj "/CN=vrcverify-dashboard" -out dashboard.csr
openssl x509 -req -in dashboard.csr -CA ca.pem -CAkey ca.key -CAcreateserial \
    -out dashboard.pem -days 825 -sha256 -extfile client.ext
chmod 600 ./*.key

# Run the strict check here rather than suggesting it, because the lenient one
# is worse than no check at all: `openssl verify` passes on a chain the
# dashboard will reject, so it reads as proof the certificates are good. The
# failure it hides surfaces later as a TLS error at request time, on the box
# where the CA key isn't, with no matching log line on the bot.
echo
echo "==> Verifying the chain the way Python will"
openssl verify -x509_strict -CAfile ca.pem bot-api.pem dashboard.pem

echo
echo "Done. Files are in $(pwd)"
echo
echo "Homelab (.env):"
echo "  BOT_API_CA=/certs/ca.pem"
echo "  BOT_API_CERT=/certs/bot-api.pem"
echo "  BOT_API_KEY=/certs/bot-api.key"
echo "  BOT_API_CLIENT_CN=vrcverify-dashboard"
echo
echo "VPS (.env):"
echo "  BOT_API_CA=/certs/ca.pem"
echo "  BOT_API_CLIENT_CERT=/certs/dashboard.pem"
echo "  BOT_API_CLIENT_KEY=/certs/dashboard.key"
echo
echo "Keep ca.key here. It is the only thing that can mint a client the bot"
echo "will trust, and neither host ever needs it."
