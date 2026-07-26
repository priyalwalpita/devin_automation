#!/usr/bin/env bash
# Replay a sample GitHub `issues.labeled` webhook against a locally running service.
set -euo pipefail

HOST="${HOST:-http://localhost:8080}"
PAYLOAD="${PAYLOAD:-$(dirname "$0")/sample_payloads/issue_labeled.json}"

if [ -f .env ] && [ -z "${GITHUB_WEBHOOK_SECRET:-}" ]; then
  # shellcheck disable=SC2046
  export $(grep -E '^GITHUB_WEBHOOK_SECRET=' .env | xargs -r) || true
fi

BODY="$(cat "$PAYLOAD")"
ARGS=(-sS -X POST "$HOST/webhook"
  -H "Content-Type: application/json"
  -H "X-GitHub-Event: issues"
  -H "X-GitHub-Delivery: simulate-$(date +%s)")

if [ -n "${GITHUB_WEBHOOK_SECRET:-}" ]; then
  SIG="$(printf '%s' "$BODY" | GITHUB_WEBHOOK_SECRET="$GITHUB_WEBHOOK_SECRET" python3 -c '
import hashlib, hmac, os, sys
body = sys.stdin.buffer.read()
digest = hmac.new(os.environ["GITHUB_WEBHOOK_SECRET"].encode(), body, hashlib.sha256).hexdigest()
print("sha256=" + digest)
')"
  ARGS+=(-H "X-Hub-Signature-256: $SIG")
  echo "-> POST $HOST/webhook (signed)"
else
  echo "-> POST $HOST/webhook (unsigned; GITHUB_WEBHOOK_SECRET not set)"
fi

curl "${ARGS[@]}" --data "$BODY" | python3 -m json.tool
echo
echo "Open $HOST/dashboard to watch the remediation."
