# Architecture

Two machines with clear roles: the **application cluster** only collects and ships data; the **monitoring server** stores it, decides when something is wrong, investigates and notifies.

```
          APPLICATION CLUSTER                                      MONITORING SERVER (k3s)
 ┌─────────────────────────────────────────┐          ┌───────────────────────────────────────────────────┐
 │ your app (frontend → API → database)    │          │                                                   │
 │ ingress controller (Traefik)            │          │  Prometheus ◄── remote-write (:30090)             │
 │                                         │ metrics  │     │  alert rules every 5–15 s                   │
 │ Prometheus AGENT ── scrapes everything ─┼────────► │     ▼                                             │
 │   kube-state-metrics, node-exporter,    │          │  Alertmanager ──► AI diagnoser ──► SMTP ──► you   │
 │   kubelet/cAdvisor, metrics-server      │  logs    │                      │   │    └──► Ollama (LLM)    │
 │ Vector ── container logs ───────────────┼────────► │  Loki ◄── push (:30100)  │                         │
 │ event exporter ── k8s events → logs     │          │                      ▼   │                         │
 │ ai-reader (read-only identity) ◄────────┼──────────┼── pods, events, rollouts, ingresses (:6443)       │
 └─────────────────────────────────────────┘          │  Blackbox exporter ── loads your site every 5 s   │
                    ▲                                  │  Grafana (HTTPS, Let's Encrypt)                    │
                    └──────── public site ◄────────────┤                                                   │
                                                       └───────────────────────────────────────────────────┘
```

## Why this shape

- **Push, don't pull.** Pod IPs are not routable from outside a cluster. Rather than exposing every exporter, the application cluster runs a Prometheus *agent* (scrape + remote-write, no storage, no alerting) and Vector, and pushes to two NodePorts on the monitoring server.
- **Keep the heavy parts away from the app.** Storage, rule evaluation and the LLM run on the monitoring server. If the application cluster falls over, the history and the alerting survive it.
- **Outside-in plus inside-out.** Internal metrics can't see a broken ingress or DNS: the app looks healthy while users get 404s. The blackbox exporter loads real URLs and checks the page content, so those failures are caught too.
- **Read-only access for investigation.** The diagnoser needs things that aren't metrics (rollout diffs, events, previous container logs, ingress backends). It reads them from the application cluster's API with a dedicated read-only ServiceAccount.

## Components

### Application cluster ([`agent/`](../agent/))

| Component | Provides |
|---|---|
| Prometheus agent (kube-prometheus-stack, `agentMode`) | Scrapes and remote-writes all metrics with `cluster=<CLUSTER_NAME>` |
| kube-state-metrics | Object state: pod ready, waiting reasons (CrashLoopBackOff, ImagePullBackOff), restarts, deployment replicas |
| node-exporter | Node CPU, memory, disk, I/O |
| kubelet / cAdvisor (built in) | Container CPU/memory, CFS throttling, **probe success/failure counters** (`prober_probe_total`) |
| metrics-server | `kubectl top` |
| Vector | Every container log to Loki, labelled `cluster, namespace, app, pod, container, level, source` |
| event exporter | Kubernetes events as JSON log lines (`source=k8s-event`), kept beyond the API's 1 h |
| `ai-reader` ServiceAccount | Read-only API access for the diagnoser |

### Monitoring server ([`monitoring-server/`](../monitoring-server/))

| Component | Role |
|---|---|
| Prometheus | Remote-write receiver (`:30090`), 15-day retention, evaluates [alert rules](alerts.md) |
| Alertmanager | Groups by `cluster, namespace` (one incident per namespace), `group_wait` 5 s, `group_interval` 1 m; sends to the diagnoser; pipeline-health alerts go straight to email |
| Loki | Log store (`:30100` push), 7-day retention |
| Blackbox exporter | Outside-in HTTP checks every 5 s with content matching |
| Ollama | Runs the local LLM (default `deepseek-v2:16b`) |
| AI diagnoser | Webhook → evidence → rule engine → LLM → email ([how](how-diagnosis-works.md)) |
| Grafana | Dashboards, Explore, and the history of AI diagnoses |
| cert-manager | TLS for Grafana |

## Data that flows where

| Data | From | To | Retention |
|---|---|---|---|
| Metrics | Prometheus agent | Prometheus `:30090` | 15 days |
| Container logs, Kubernetes events | Vector | Loki `:30100` | 7 days |
| Outside-in checks | Blackbox exporter | Prometheus | 15 days |
| Diagnoses | AI diagnoser | Loki (`source=ai-diagnoser`) + email | 7 days |

## If a piece fails

| Failure | What happens |
|---|---|
| LLM slow or unavailable | The rule-engine diagnosis is emailed instead (marked "AI unavailable") |
| Diagnoser down | `AIDiagnoserDown` fires; Alertmanager emails you directly |
| App cluster stops sending metrics | `AppClusterTelemetryMissing` emails you directly |
| Ollama down | `OllamaDown` emails you directly; diagnoses fall back to rule-based |
