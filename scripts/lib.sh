# Shared helpers: load config.env and render templates with only our variables
# (so Prometheus/Alertmanager templates like {{ $labels.pod }} are left untouched).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${CONFIG:-$ROOT/config.env}"
SECRETS="${SECRETS:-$ROOT/secrets}"
[ -f "$CONFIG" ] || { echo "Missing $CONFIG. Run: cp config.example.env config.env, then edit it." >&2; exit 1; }
set -a; . "$CONFIG"; set +a

VARS='${CLUSTER_NAME} ${APP_NAMESPACE} ${API_APP} ${API_DEPLOYMENT} ${API_METRICS_PORT} ${FRONTEND_APP}
${INGRESS_CONTROLLER_NAMESPACE} ${MONITORING_SERVER_IP} ${APP_NODE_IP} ${GRAFANA_HOST} ${ACME_EMAIL}
${SITE_PAGE_URL} ${SITE_PAGE_REGEX} ${SITE_API_URL} ${SITE_API_REGEX} ${SMTP_HOST} ${SMTP_PORT} ${SMTP_USER}
${ALERT_EMAIL_TO} ${MODEL} ${EXCLUDE_ROUTES}'

render() { envsubst "$VARS" < "$1"; }
apply() { render "$1" | kubectl apply -f -; }
need() { for c in "$@"; do command -v "$c" >/dev/null || { echo "Missing required command: $c" >&2; exit 1; }; done; }
log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
