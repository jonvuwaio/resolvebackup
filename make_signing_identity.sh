#!/bin/bash
# One-time, on the Mac that builds the app: make a self-signed code-signing identity in the login keychain.
#
#   ./make_signing_identity.sh
#
# build_mac_app.sh signs with it when it is there. Every build then has the same signing identity, so macOS
# remembers what it was allowed (Local Network access in particular) across updates instead of asking again
# for each new build, as it does for ad-hoc signed apps. It is only trusted by this Mac; nothing is notarised.
set -euo pipefail
NAME="${1:-Colour Project Manager Signing}"

if security find-identity -p codesigning | grep -q "\"$NAME\""; then
  echo "Already in the keychain: $NAME"
  exit 0
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
cat > "$tmp/req.cnf" <<EOF
[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
CN = $NAME
[ext]
basicConstraints = critical, CA:false
keyUsage = critical, digitalSignature
extendedKeyUsage = critical, codeSigning
EOF
openssl req -x509 -newkey rsa:2048 -nodes -days 7300 -config "$tmp/req.cnf" \
  -keyout "$tmp/key.pem" -out "$tmp/cert.pem" 2>/dev/null
pass=$(openssl rand -hex 16)
openssl pkcs12 -export -inkey "$tmp/key.pem" -in "$tmp/cert.pem" -name "$NAME" \
  -passout "pass:$pass" -out "$tmp/id.p12"
security import "$tmp/id.p12" -k "$HOME/Library/Keychains/login.keychain-db" -P "$pass" -T /usr/bin/codesign
echo "Made the signing identity: $NAME"
