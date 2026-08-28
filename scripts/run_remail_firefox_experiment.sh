#!/bin/bash
# Remail + Firefox + protocol registration, then +10min liveness/promotion probe
# with per-account fingerprint + registration proxy affinity.
cd /f/epsoft/GPT-Register-Tool || exit 1

LOG=runtime/remail_firefox_reg_20260827.log
PROBE_LOG=runtime/remail_firefox_probe_20260827.log
PY=D:/software/python/python.exe

echo "=== registration start: $(date -u '+%H:%M:%S') UTC ==="
$PY chatgpt_phone_reg.py --buy-remail-mailbox --registration-driver protocol --count 1 > "$LOG" 2>&1
echo "=== registration done: $(date -u '+%H:%M:%S') UTC ==="
tail -n 4 "$LOG"

if ! grep -q "1/1 registered successfully" "$LOG"; then
  echo "REGISTRATION FAILED - skipping probe"
  exit 2
fi

NEWSESS=$(ls -t sessions/*.json 2>/dev/null | head -1)
EMAIL=$($PY -c "import json,sys; print(json.load(open(sys.argv[1], encoding='utf-8'))['email'])" "$NEWSESS")
FP=$($PY -c "import json,sys; d=json.load(open(sys.argv[1], encoding='utf-8')); print(d.get('auth_fingerprint_profile'), (d.get('identity_context') or {}).get('fingerprint_key'))" "$NEWSESS")
echo "registered: $EMAIL (fingerprint: $FP)"

echo "=== waiting 600s (+10min probe window) ==="
sleep 600
echo "=== probe start: $(date -u '+%H:%M:%S') UTC ==="

echo "--- quota-usage (liveness) ---" >> "$PROBE_LOG"
$PY chatgpt_phone_reg.py --quota-usage --email "$EMAIL" >> "$PROBE_LOG" 2>&1
echo "--- check-promotion ---" >> "$PROBE_LOG"
$PY chatgpt_phone_reg.py --check-promotion --email "$EMAIL" >> "$PROBE_LOG" 2>&1
echo "=== probe done: $(date -u '+%H:%M:%S') UTC ==="
cat "$PROBE_LOG"
