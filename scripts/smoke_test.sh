#!/usr/bin/env bash
# Smoke test for a RUNNING API: health, one simulated recovery, and a repeated request (idempotent replay).
#
#   BASE_URL=http://localhost:8000 PAYMENT_ID=pay_004 scripts/smoke_test.sh
#
# WARNING: this makes the API SAVE a decision (and, if approved, a simulated action) in whatever database the
# API is connected to. Point it only at an API whose database you are happy to write to.
# Everything is simulated: nothing is charged and no email or message is sent.
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
PAYMENT_ID="${PAYMENT_ID:?Set PAYMENT_ID to a FAILED payment id that exists in the database the API uses}"
KEY="smoke-$(date +%s)-$$"

field() { python3 -c "import sys, json; d = json.load(sys.stdin); print($1)"; }

echo "1) health"
curl -fsS "$BASE_URL/health"; echo

echo "2) simulate recovery for $PAYMENT_ID (key $KEY) - this can take several seconds while the LLM thinks"
FIRST=$(curl -fsS -X POST "$BASE_URL/recovery/simulate" -H 'Content-Type: application/json' \
        -H "Idempotency-Key: $KEY" -d "{\"payment_id\": \"$PAYMENT_ID\"}")
echo "$FIRST" | field "'   outcome=%s  action_taken=%s  policy=%s' % (d['run_status'], d['action_taken'], d['decision']['policy_status'])"
[ "$(echo "$FIRST" | field "d['simulated']")" = "True" ] || { echo "FAIL: response is not marked simulated"; exit 1; }
[ "$(echo "$FIRST" | field "d['idempotent_replay']")" = "False" ] || { echo "FAIL: first call should not be a replay"; exit 1; }

echo "3) repeat the same request with the same key (must be a replay, with no second LLM call)"
SECOND=$(curl -fsS -X POST "$BASE_URL/recovery/simulate" -H 'Content-Type: application/json' \
         -H "Idempotency-Key: $KEY" -d "{\"payment_id\": \"$PAYMENT_ID\"}")
[ "$(echo "$SECOND" | field "d['idempotent_replay']")" = "True" ] || { echo "FAIL: second call was not a replay"; exit 1; }
[ "$(echo "$FIRST" | field "d['run_id']")" = "$(echo "$SECOND" | field "d['run_id']")" ] || { echo "FAIL: replay returned a different decision"; exit 1; }

echo "OK: health, simulation and idempotent replay all behaved as expected."
