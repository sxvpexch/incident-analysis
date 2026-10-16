#!/usr/bin/env bash
# Installs the monitoring server: Prometheus, Alertmanager, Grafana, Loki, blackbox checks, Ollama and the
# AI diagnoser. Run on the monitoring server with kubectl pointing at its cluster. Idempotent: re-run after edits.
source "$(dirname "$0")/../scripts/lib.sh"
need kubectl helm envsubst python3
cd "$ROOT/monitoring-server"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

log "Checking secrets in $SECRETS/"
for f in smtp_password app-cluster-token app-cluster-ca.crt; do
  [ -s "$SECRETS/$f" ] || { echo "Missing $SECRETS/$f (see README: Secrets)" >&2; exit 1; }
done
if [ ! -s "$SECRETS/grafana_admin_password" ]; then
  python3 -c "import secrets;print(secrets.token_urlsafe(18),end='')" > "$SECRETS/grafana_admin_password"
  chmod 600 "$SECRETS/grafana_admin_password"; echo "Generated $SECRETS/grafana_admin_password"
fi

log "Helm repositories"
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null
helm repo add grafana https://grafana.github.io/helm-charts >/dev/null
helm repo add jetstack https://charts.jetstack.io >/dev/null
helm repo update >/dev/null

log "Namespaces and secrets"
for ns in monitoring ai; do kubectl create namespace $ns --dry-run=client -o yaml | kubectl apply -f -; done
kubectl -n monitoring create secret generic grafana-admin --from-literal=admin-user=admin \
  --from-file=admin-password="$SECRETS/grafana_admin_password" --dry-run=client -o yaml | kubectl apply -f -
for ns in monitoring ai; do
  kubectl -n $ns create secret generic smtp-credentials --from-file=password="$SECRETS/smtp_password" \
    --dry-run=client -o yaml | kubectl apply -f -
done
kubectl -n ai create secret generic app-cluster-reader --from-file=token="$SECRETS/app-cluster-token" \
  --from-file=ca.crt="$SECRETS/app-cluster-ca.crt" --dry-run=client -o yaml | kubectl apply -f -

log "cert-manager + Let's Encrypt issuer"
helm upgrade --install cert-manager jetstack/cert-manager -n cert-manager --create-namespace \
  --version v1.21.2 --set crds.enabled=true --wait --timeout 5m
apply manifests/cluster-issuer.yaml

log "Prometheus, Alertmanager, Grafana (kube-prometheus-stack)"
render helm/kube-prometheus-stack.values.yaml > "$TMP/kps.yaml"
helm upgrade --install kps prometheus-community/kube-prometheus-stack -n monitoring --version 92.8.0 \
  -f "$TMP/kps.yaml" --wait --timeout 10m

log "Loki"
helm upgrade --install loki grafana/loki -n monitoring --version 7.3.0 -f helm/loki.values.yaml --wait --timeout 6m
apply manifests/loki-push-nodeport.yaml

log "Outside-in checks (blackbox exporter)"
render helm/blackbox.values.yaml > "$TMP/blackbox.yaml"
helm upgrade --install blackbox prometheus-community/prometheus-blackbox-exporter -n monitoring \
  --version 11.20.0 -f "$TMP/blackbox.yaml" --wait
apply manifests/probes.yaml

log "Alert rules"
apply rules/alert-rules.yaml

log "Ollama + model $MODEL (first download takes a few minutes)"
kubectl apply -f manifests/ollama.yaml
kubectl -n ai rollout status deploy/ollama --timeout=300s
kubectl -n ai exec deploy/ollama -- ollama pull "$MODEL"

log "AI diagnoser"
"$ROOT/diagnoser/deploy.sh"

log "Grafana dashboard"
python3 dashboards/build.py | kubectl apply -f -

log "Done"
echo "Grafana:   https://$GRAFANA_HOST  (user admin, password in $SECRETS/grafana_admin_password)"
echo "Remote-write endpoint for the app cluster: http://$MONITORING_SERVER_IP:30090/api/v1/write"
echo "Loki push endpoint for the app cluster:    http://$MONITORING_SERVER_IP:30100"
echo "Smoke test: scripts/diagnose-now.sh   (dry run, prints what the email would say)"
