#!/usr/bin/env bash
# Ask the diagnoser to analyse the application right now, without sending email (dry run),
# then print the diagnosis. Useful as a smoke test after install.  Usage: scripts/diagnose-now.sh [namespace]
source "$(dirname "$0")/lib.sh"
NS="${1:-$APP_NAMESPACE}"
call() { kubectl -n ai exec deploy/diagnoser -- python -c "import urllib.request,sys;print(urllib.request.urlopen(urllib.request.Request(sys.argv[1],data=$2,method='$3'),timeout=900).read().decode())" "http://localhost:8080$1"; }
call "/diagnose?namespace=$NS&dry_run=1" "b'{}'" POST >/dev/null
echo "Diagnosing $NS (the model may take ~30-60s)..."
SINCE=$(date -u +%Y-%m-%dT%H:%M:%SZ)
until kubectl -n ai logs deploy/diagnoser --since-time="$SINCE" | grep -qE "done in|job failed"; do sleep 2; done
call /last.json None GET
