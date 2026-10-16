#!/usr/bin/env bash
# (Re)deploys the AI diagnoser: code and settings as ConfigMaps. Re-run after editing diagnoser.py or config.env.
source "$(dirname "$0")/../scripts/lib.sh"
need kubectl
cd "$ROOT/diagnoser"
kubectl -n ai create configmap diagnoser-code --from-file=diagnoser.py --dry-run=client -o yaml | kubectl apply -f -
kubectl -n ai create configmap diagnoser-config --dry-run=client -o yaml \
  --from-literal=MODEL="$MODEL" \
  --from-literal=LLM_MODE="${LLM_MODE:-lean}" \
  --from-literal=LLM_THINK="${LLM_THINK:-false}" \
  --from-literal=LLM_TIMEOUT="${LLM_TIMEOUT:-420}" \
  --from-literal=LLM_MAX_TOKENS="${LLM_MAX_TOKENS:-400}" \
  --from-literal=QUICK_EMAIL="${QUICK_EMAIL:-false}" \
  --from-literal=K8S_API="https://$APP_NODE_IP:6443" \
  --from-literal=CLUSTER="$CLUSTER_NAME" \
  --from-literal=APP_NAMESPACE="$APP_NAMESPACE" \
  --from-literal=API_APP="$API_APP" \
  --from-literal=FRONTEND_APP="$FRONTEND_APP" \
  --from-literal=APP_DESCRIPTION="$APP_DESCRIPTION" \
  --from-literal=EXCLUDE_ROUTES="$EXCLUDE_ROUTES" \
  --from-literal=APP_INFO_METRIC="$APP_INFO_METRIC" \
  --from-literal=INGRESS_MANIFEST="$INGRESS_MANIFEST" \
  --from-literal=INGRESS_CONTROLLER_NAMESPACE="$INGRESS_CONTROLLER_NAMESPACE" \
  --from-literal=PROJECT_NAME="$PROJECT_NAME" \
  --from-literal=REGION="$REGION" \
  --from-literal=GRAFANA_URL="https://$GRAFANA_HOST" \
  --from-literal=SMTP_HOST="$SMTP_HOST" \
  --from-literal=SMTP_PORT="$SMTP_PORT" \
  --from-literal=SMTP_USER="$SMTP_USER" \
  --from-literal=MAIL_FROM="$PROJECT_NAME <$SMTP_USER>" \
  --from-literal=MAIL_TO="$ALERT_EMAIL_TO" \
  --from-literal=GROUND_TRUTH_METRIC="$GROUND_TRUTH_METRIC" \
  --from-literal=GROUND_TRUTH_LABEL="$GROUND_TRUTH_LABEL" \
  --from-literal=GROUND_TRUTH_MAP="$GROUND_TRUTH_MAP" | kubectl apply -f -
HASH=$( (cat diagnoser.py; kubectl -n ai get configmap diagnoser-config -o yaml) | sha256sum | cut -c1-12)
sed "s/CODE_HASH/$HASH/" "$ROOT/monitoring-server/manifests/diagnoser.yaml" | kubectl apply -f -
kubectl -n ai rollout status deploy/diagnoser --timeout=180s
