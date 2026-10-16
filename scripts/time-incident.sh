#!/usr/bin/env bash
# Break something, measure how long each stage takes until the diagnosis email is sent, then restore.
#   scripts/time-incident.sh "<command that breaks it>" "<command that restores it>"
# Example (run where kubectl reaches the app cluster):
#   scripts/time-incident.sh \
#     "kubectl -n shop set image deploy/api api=example/api:does-not-exist" \
#     "kubectl -n shop rollout undo deploy/api"
# Needs: kubectl contexts for both clusters. Set APP_CTX / MON_CTX if they differ from the current context.
source "$(dirname "$0")/lib.sh"
INJECT="${1:?inject command}"; RESTORE="${2:?restore command}"; MAX="${MAX:-420}"
MON=(kubectl ${MON_CTX:+--context "$MON_CTX"})
PROM="${PROM:-http://127.0.0.1:30090}"
q() { curl -s "$PROM/api/v1/query" --data-urlencode "query=$1" | grep -q '"result":\[{'; }
T0=$(date +%s.%N); SINCE=$(date -u +%Y-%m-%dT%H:%M:%SZ)
el() { printf "%6.1fs" "$(echo "$(date +%s.%N) - $T0" | bc)"; }
say() { echo "$(date -u +%T) T+$(el)  $*"; }
bash -c "$INJECT"; say "INJECTED: $INJECT"
f1=; f2=; f3=; f4=
while (( $(echo "$(date +%s.%N) - $T0 < $MAX" | bc) )); do
  [ -z "$f1" ] && q "ALERTS{alertstate=\"pending\",namespace=\"$APP_NAMESPACE\"}" && { f1=1; say "an alert is PENDING"; }
  [ -z "$f2" ] && q "ALERTS{alertstate=\"firing\",namespace=\"$APP_NAMESPACE\"}" && { f2=1; f1=1; say "an alert is FIRING"; }
  LG=$("${MON[@]}" -n ai logs deploy/diagnoser --since-time="$SINCE" 2>/dev/null || true)
  [ -z "$f3" ] && grep -q "evidence collected" <<<"$LG" && { f3=1; say "delivered to the diagnoser, evidence collected"; }
  [ -z "$f4" ] && grep -qE "LLM answered|LLM failed" <<<"$LG" && { f4=1; say "$(grep -E "LLM answered|LLM failed" <<<"$LG" | tail -1 | cut -c25-)"; }
  grep -q "email sent: \[\(CRIT\|WARN\)" <<<"$LG" && { say "$(grep "email sent" <<<"$LG" | tail -1 | cut -c25-)"; break; }
  sleep 0.5
done
bash -c "$RESTORE"; say "RESTORED: $RESTORE"
