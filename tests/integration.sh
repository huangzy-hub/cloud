#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
state=$(mktemp -d)
pid=""
cleanup() {
  if [[ -n "$pid" ]]; then kill "$pid" 2>/dev/null || true; fi
  rm -rf "$state"
}
trap cleanup EXIT

python3 gateway/cloud_auth.py --state-dir "$state" init >/dev/null
key=$(python3 gateway/cloud_auth.py --state-dir "$state" add owner | tail -1)
python3 gateway/cloud_auth.py --state-dir "$state" serve --port 18091 >/tmp/cloud-auth-test.log 2>&1 &
pid=$!
for _ in 1 2 3 4 5; do
  curl --noproxy '*' -fsS http://127.0.0.1:18091/healthz >/dev/null 2>&1 && break
  sleep 0.2
done

code=$(curl --noproxy '*' -sS -o /dev/null -w "%{http_code}" http://127.0.0.1:18091/auth)
test "$code" = 401

curl --noproxy '*' -sS -D /tmp/cloud-auth-headers -o /dev/null -X POST \
  -H "Host: cloud.example.com" \
  -H "Origin: https://cloud.example.com" \
  --data-urlencode "key=$key" \
  --data-urlencode "next=/SSD" \
  http://127.0.0.1:18091/login
cookie=$(sed -n 's/^Set-Cookie: \(__Host-cloud_session=[^;]*\).*/\1/p' /tmp/cloud-auth-headers | tr -d '\r')
test -n "$cookie"

code=$(curl --noproxy '*' -sS -o /dev/null -w "%{http_code}" -H "Cookie: $cookie" http://127.0.0.1:18091/auth)
test "$code" = 204

python3 gateway/cloud_auth.py --state-dir "$state" revoke owner >/dev/null
code=$(curl --noproxy '*' -sS -o /dev/null -w "%{http_code}" -H "Cookie: $cookie" http://127.0.0.1:18091/auth)
test "$code" = 401

echo "HTTP integration: PASS"
