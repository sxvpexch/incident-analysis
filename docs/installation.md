# Installation

This walks through a full install: one application cluster (any Kubernetes) and one monitoring server (a Linux VM running k3s). Both should be in the same private network.

## 1. Requirements

| | Application cluster | Monitoring server |
|---|---|---|
| Kubernetes | any (tested: kubeadm 1.37) | k3s, installed by `install-k3s.sh` (tested: v1.36) |
| Size | your app + ~1 CPU / 1.5 GB for the collectors | 8 vCPU, 32 GB RAM, 50 GB disk (no GPU needed) |
| Tools on your workstation | `kubectl`, `helm` 3, `envsubst` (package `gettext-base`), `python3` | same |
| Ingress | Traefik (for routing evidence); any controller works for outside-in checks | k3s' built-in Traefik serves Grafana |

> **Pick a non-burstable VM for the monitoring server** (e.g. AWS m6i/m7i, not t2/t3). The LLM keeps all cores busy for ~30 s per incident; burstable instances run out of CPU credits and slow down by 5–10×.

## 2. Network / firewall

| Port | From | To | Purpose |
|---|---|---|---|
| TCP 30090 | application node(s) | monitoring server | Prometheus remote-write (metrics) |
| TCP 30100 | application node(s) | monitoring server | Loki push (logs) |
| TCP 6443 | monitoring server | application control plane | read-only Kubernetes API for the diagnoser |
| TCP 80, 443 | internet | monitoring server | Grafana + Let's Encrypt HTTP-01 |
| TCP 22 | your IP | both | SSH |

Outbound from the monitoring server: your SMTP server (587), the Ollama model registry, Helm chart repos, and your public site (for outside-in checks).

The Kubernetes API certificate must include the IP or name you use in `APP_NODE_IP` (kubeadm: add it to `apiServer.certSANs`).

## 3. DNS

Create an A record for `GRAFANA_HOST` pointing at the monitoring server's **public** IP (use a static/elastic IP). cert-manager requests a Let's Encrypt certificate for it automatically.

## 4. Configure

```bash
cp config.example.env config.env
$EDITOR config.env
```

The important values:

- `CLUSTER_NAME`, `APP_NAMESPACE`, `API_APP`, `API_DEPLOYMENT`, `API_METRICS_PORT`: what you monitor.
- `MONITORING_SERVER_IP`, `APP_NODE_IP`: private IPs.
- `GRAFANA_HOST`, `ACME_EMAIL`: Grafana's public name and the Let's Encrypt account.
- `SITE_PAGE_URL` / `SITE_PAGE_REGEX`, `SITE_API_URL` / `SITE_API_REGEX`: two URLs users hit, plus text that proves the response is real (a 200 with the wrong page still fails).
- `SMTP_*`, `ALERT_EMAIL_TO`: where alerts go.
- `INGRESS_CONTROLLER_NAMESPACE`: `traefik` for a Helm-installed Traefik, `kube-system` on k3s.

## 5. Install the collectors on the application cluster

With `kubectl` pointing at the **application** cluster:

```bash
agent/install.sh
```

It installs metrics-server, a Prometheus agent with kube-state-metrics and node-exporter, Vector, the Kubernetes event exporter, scrape targets for your API/Traefik/cert-manager, and the `ai-reader` read-only identity. It then writes `secrets/app-cluster-token` and `secrets/app-cluster-ca.crt`.

**If your namespace denies ingress by default** (NetworkPolicy), allow the scraper:

```bash
source scripts/lib.sh && render agent/optional/network-policy.yaml | kubectl apply -f -
```

Optional extras (edit first): [`agent/optional/postgres-exporter.yaml`](../agent/optional/postgres-exporter.yaml), [`agent/optional/nginx-exporter-sidecar.yaml`](../agent/optional/nginx-exporter-sidecar.yaml).

## 6. Install the monitoring server

On the monitoring server, clone the repo and copy in `config.env` plus the two files from step 5.

```bash
monitoring-server/install-k3s.sh <private-ip-of-this-server>
export KUBECONFIG=~/.kube/config

mkdir -p secrets && chmod 700 secrets
echo -n 'your-smtp-app-password' > secrets/smtp_password && chmod 600 secrets/smtp_password
# + secrets/app-cluster-token and secrets/app-cluster-ca.crt from step 5

monitoring-server/install.sh
```

This takes 10–15 minutes, most of it downloading the model (~9 GB). It generates `secrets/grafana_admin_password` if you didn't provide one.

## 7. Verify

```bash
# metrics arriving from the application cluster
scripts/promq.sh 'count by (job) (up{cluster="'"$CLUSTER_NAME"'"} == 1)'

# outside-in checks passing (1 = success)
scripts/promq.sh 'probe_success'

# a dry-run diagnosis of the app as it is right now
scripts/diagnose-now.sh
```

Open `https://GRAFANA_HOST`, log in as `admin`, and find the **service health** dashboard.

To see the whole loop once, break something harmless and time it (this sends a real email):

```bash
scripts/time-incident.sh \
  "kubectl -n $APP_NAMESPACE set image deploy/$API_DEPLOYMENT $API_APP=example/does-not-exist:1" \
  "kubectl -n $APP_NAMESPACE rollout undo deploy/$API_DEPLOYMENT"
```

## Updating

Every install script is idempotent. After editing `config.env`, alert rules or the diagnoser:

| Changed | Re-run |
|---|---|
| `diagnoser/diagnoser.py` or AI settings | `diagnoser/deploy.sh` |
| `monitoring-server/rules/alert-rules.yaml` | `source scripts/lib.sh && apply monitoring-server/rules/alert-rules.yaml` |
| anything else on the monitoring server | `monitoring-server/install.sh` |
| anything on the application cluster | `agent/install.sh` |

## Uninstall

```bash
# application cluster
helm -n monitoring uninstall agent vector; helm -n kube-system uninstall metrics-server
kubectl delete namespace monitoring; kubectl delete clusterrole,clusterrolebinding ai-reader event-exporter

# monitoring server
/usr/local/bin/k3s-uninstall.sh
```
