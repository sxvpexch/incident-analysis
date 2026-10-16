#!/usr/bin/env bash
# Installs the collectors on the APPLICATION cluster: Prometheus agent, kube-state-metrics, node-exporter,
# metrics-server, Vector, Kubernetes event exporter, and a read-only identity for the AI diagnoser.
# Run with kubectl pointing at the application cluster. Idempotent.
source "$(dirname "$0")/../scripts/lib.sh"
need kubectl helm envsubst
cd "$ROOT/agent"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

log "Helm repositories"
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null
helm repo add vector https://helm.vector.dev >/dev/null
helm repo add metrics-server https://kubernetes-sigs.github.io/metrics-server/ >/dev/null
helm repo update >/dev/null
kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -

log "metrics-server"
helm upgrade --install metrics-server metrics-server/metrics-server -n kube-system --version 3.14.0 \
  --set 'args={--kubelet-insecure-tls}' --wait

log "Prometheus agent + kube-state-metrics + node-exporter"
render prometheus-agent.values.yaml > "$TMP/agent.yaml"
helm upgrade --install agent prometheus-community/kube-prometheus-stack -n monitoring --version 92.8.0 \
  -f "$TMP/agent.yaml" --wait --timeout 10m

log "Vector (logs -> Loki)"
render vector.values.yaml > "$TMP/vector.yaml"
helm upgrade --install vector vector/vector -n monitoring --version 0.59.0 -f "$TMP/vector.yaml" --wait

log "Scrape targets, event exporter, read-only identity for the AI"
apply monitors.yaml
apply event-exporter.yaml
apply ai-reader-rbac.yaml

log "Exporting the read-only token to $SECRETS/"
mkdir -p "$SECRETS" && chmod 700 "$SECRETS"
until kubectl -n monitoring get secret ai-reader-token -o jsonpath='{.data.token}' 2>/dev/null | grep -q .; do sleep 2; done
( umask 077
  kubectl -n monitoring get secret ai-reader-token -o jsonpath='{.data.token}' | base64 -d > "$SECRETS/app-cluster-token"
  kubectl -n monitoring get secret ai-reader-token -o jsonpath='{.data.ca\.crt}' | base64 -d > "$SECRETS/app-cluster-ca.crt" )

log "Done"
echo "Copy $SECRETS/app-cluster-token and $SECRETS/app-cluster-ca.crt to the monitoring server's secrets/ directory."
echo "Optional add-ons (render + apply): agent/optional/network-policy.yaml, postgres-exporter.yaml, nginx-exporter-sidecar.yaml"
echo "  e.g.  source scripts/lib.sh && render agent/optional/network-policy.yaml | kubectl apply -f -"
