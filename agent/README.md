# agent/ — installed on the application cluster

Collectors that ship data out to the monitoring server. Nothing here stores data or alerts.

| File | What |
|---|---|
| `install.sh` | Installs everything below; exports the read-only token to `secrets/` |
| `prometheus-agent.values.yaml` | kube-prometheus-stack in **agent mode**: scrapes, adds `cluster=<CLUSTER_NAME>`, remote-writes to `MONITORING_SERVER_IP:30090`. Includes kube-state-metrics and node-exporter |
| `monitors.yaml` | Extra scrape targets: your API's `/metrics`, Traefik, cert-manager, Vector |
| `vector.values.yaml` | Vector DaemonSet: all container logs → Loki at `MONITORING_SERVER_IP:30100`, with a derived `level` label (JSON `level`, Traefik status, or keywords) |
| `event-exporter.yaml` | Kubernetes events as JSON log lines (`source=k8s-event`) |
| `ai-reader-rbac.yaml` | Read-only ServiceAccount for the diagnoser: `get`/`list` on pods, logs, events, workloads, ingresses, certificates. No secrets, no ConfigMaps, no writes |
| `optional/network-policy.yaml` | Allow scraping if your namespace denies ingress by default |
| `optional/postgres-exporter.yaml` | Postgres internals (longest active query, connections) |
| `optional/nginx-exporter-sidecar.yaml` | nginx `stub_status` metrics |

All `.yaml` files are templates; `install.sh` fills `${VARS}` from `config.env`. To apply one by hand:

```bash
source scripts/lib.sh && render agent/optional/network-policy.yaml | kubectl apply -f -
```

Resource footprint on a small node: roughly 1 CPU request and 1.5 GB memory in total.
