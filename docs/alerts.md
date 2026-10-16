# Alert rules

All rules live in [`monitoring-server/rules/alert-rules.yaml`](../monitoring-server/rules/alert-rules.yaml). They are deliberately **specific** — the generic upstream rule set is disabled, because generic alerts are what make on-call vague. Every rule carries `severity` and `component` labels; the diagnoser uses the alert names as hints, not as the diagnosis.

"For" is how long the condition must hold before the alert fires. Most groups are evaluated every 15 s; the `app.synthetic` group (what users feel) every 5 s.

## What users see (fastest signals)

| Alert | For | Severity | Fires when |
|---|---|---|---|
| `SiteCheckFailing` | 10s | critical | The outside-in check of a URL fails (wrong status, wrong content, timeout) |
| `PodProbesFailing` | 0s | critical | A pod failed ≥ 5 liveness/readiness/startup probes in 1 min (kubelet counters; a healthy boot fails at most 1–2) |

## API

| Alert | For | Severity | Fires when |
|---|---|---|---|
| `ApiHighErrorRate` | 30s | critical | 5xx ratio > 5% (with meaningful traffic) |
| `ApiHighLatency` | 1m | warning | p95 > 1 s |
| `ApiNoReadyPods` | 30s | critical | The API Deployment has 0 available replicas |

## Database

| Alert | For | Severity | Fires when |
|---|---|---|---|
| `DbQueriesSlow` | 1m | warning | A query's p95 > 250 ms |
| `DbPoolSaturated` | 1m | warning | Requests are waiting for a pool connection |
| `DbQueryErrors` | 30s | warning | `db_errors_total` increasing |
| `PostgresDown` | 30s | critical | postgres-exporter can't connect (optional exporter) |
| `PostgresLongRunningQueries` | 1m | warning | An active transaction > 500 ms |

## Workloads

| Alert | For | Severity | Fires when |
|---|---|---|---|
| `ContainerOOMKilled` | 0s | critical | Last termination was OOMKilled and it restarted in the last 10 min |
| `ContainerMemoryNearLimit` | 30s | warning | Working set > 80% of the memory limit |
| `ContainerCpuThrottled` | 1m | warning | > 25% of CFS periods throttled **and** using > 50% of the CPU limit (ignores tiny sidecars) |
| `PodCrashLooping` | 30s | critical | Container waiting in CrashLoopBackOff |
| `PodImagePullFailing` | 30s | critical | Container waiting in ErrImagePull / ImagePullBackOff / InvalidImageName |
| `PodRestartingFrequently` | 0s | warning | ≥ 2 restarts in 15 min |
| `PodNotReady` | 2m | warning | Running but not Ready |
| `DeploymentReplicasUnavailable` | 2m | warning | Unavailable replicas > 0 |
| `StatefulSetNotReady` | 1m | critical | Fewer ready replicas than desired |

## Edge

| Alert | For | Severity | Fires when |
|---|---|---|---|
| `TraefikHighErrorRate` | 30s | critical | Traefik returns > 5% 5xx for a service in your namespace |
| `FrontendNginxDown` | 30s | critical | nginx stub_status not answering (optional exporter) |
| `CertificateExpiringSoon` | 10m | warning | Certificate expires in < 14 days |
| `CertificateNotReady` | 10m | warning | cert-manager Certificate not Ready |

## Node

| Alert | For | Severity | Fires when |
|---|---|---|---|
| `NodeMemoryPressure` | 5m | warning | Node memory > 90% |
| `NodeCpuSaturated` | 10m | warning | Node CPU > 90% |
| `NodeDiskFilling` | 10m | warning | Root disk > 85% |

## The monitoring pipeline itself

These bypass the AI and are emailed **directly** by Alertmanager, so a broken diagnosis pipeline can never hide an outage.

| Alert | For | Fires when |
|---|---|---|
| `AppClusterTelemetryMissing` | 1m | No kube-state-metrics data from the app cluster for 3 min (agent, network or firewall broken) |
| `AIDiagnoserDown` | 2m | The diagnoser isn't being scraped as up |
| `OllamaDown` | 2m | The Ollama Deployment has no available replica |

## Grouping and noise control

- Alertmanager groups by `cluster, namespace`: everything wrong in one namespace becomes **one incident** and one email. "Latency + CPU throttling" arrives as one root cause, not two alerts.
- Inhibitions: `PodCrashLooping` suppresses restart/availability/error-rate alerts in the same namespace; `ContainerOOMKilled` suppresses restart/near-limit alerts for the same pod.
- The diagnoser re-diagnoses an open incident only when a new *kind* of alert joins it.

## Tuning

Thresholds are plain PromQL in the rules file. Faster detection means more sensitivity to blips; the shipped values were chosen by breaking things on purpose and measuring (see [testing.md](testing.md)). After editing:

```bash
source scripts/lib.sh && apply monitoring-server/rules/alert-rules.yaml
```
