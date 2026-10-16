#!/usr/bin/env bash
# Run a PromQL query against the monitoring server's Prometheus.  Usage: scripts/promq.sh '<query>'
PROM="${PROM:-http://127.0.0.1:30090}"
curl -s "$PROM/api/v1/query" --data-urlencode "query=$1" |
  python3 -c "import sys,json;[print(' ',r['metric'],r['value'][1]) for r in json.load(sys.stdin)['data']['result']]"
