# monitoring-server/ — the monitoring server

| Path | What |
|---|---|
| `install-k3s.sh` | Installs single-node k3s + Helm (run once) |
| `install.sh` | Installs everything below and the diagnoser; idempotent |
| `helm/kube-prometheus-stack.values.yaml` | Prometheus (remote-write receiver on NodePort 30090, 15 d retention), Alertmanager (grouping, inhibitions, webhook to the diagnoser, direct email for pipeline alerts), Grafana (HTTPS ingress, Loki datasource) |
| `helm/loki.values.yaml` | Loki single binary, filesystem storage, 7 d retention |
| `helm/blackbox.values.yaml` | Outside-in check modules (status 200 + required page text) |
| `manifests/probes.yaml` | The two URLs checked every 5 s |
| `manifests/loki-push-nodeport.yaml` | Loki push endpoint on NodePort 30100 |
| `manifests/cluster-issuer.yaml` | Let's Encrypt (HTTP-01 via Traefik) |
| `manifests/ollama.yaml` | Ollama with a 15 Gi model volume, one model in RAM, keep-alive 24 h |
| `manifests/diagnoser.yaml` | The diagnoser Deployment (stock `python:3.13-slim`, code from a ConfigMap), Service, ServiceMonitor |
| `rules/alert-rules.yaml` | All alert rules ([docs/alerts.md](../docs/alerts.md)) |
| `dashboards/build.py` | Generates the "service health" Grafana dashboard |

Pinned versions (tested): k3s v1.36.5, kube-prometheus-stack 92.8.0, Loki chart 7.3.0, blackbox-exporter chart 11.20.0, cert-manager v1.21.2, Ollama 0.40.4.
