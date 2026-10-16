# Testing: break things on purpose

The only honest way to know whether the diagnoses are right is to cause failures you understand and read what the system says. This page lists scenarios that work on most clusters, what a correct diagnosis looks like, and how to measure every stage.

## Measure an incident end to end

[`scripts/time-incident.sh`](../scripts/time-incident.sh) runs a "break" command, timestamps each stage, and runs a "restore" command once the email is sent:

```bash
scripts/time-incident.sh "<break command>" "<restore command>"
```

```
05:58:33 T+   0.3s  INJECTED: kubectl -n shop patch deploy api ... containerPort 800
05:59:00 T+  27.6s  an alert is FIRING
05:59:06 T+  33.3s  delivered to the diagnoser, evidence collected
05:59:34 T+  61.7s  LLM answered in 29.2s (102 tokens)
05:59:36 T+  63.0s  email sent: [CRITICAL] Bad Deploy: deployment/api revision 14 ... containerPort 'http' 8000 -> 800 (HIGH)
05:59:37 T+  64.0s  RESTORED: kubectl -n shop rollout undo deployment/api
```

It needs the monitoring server's Prometheus on `127.0.0.1:30090` and `kubectl` access to both clusters (`MON_CTX` / `APP_CTX` if your contexts differ). Each run sends a real email, then a `RESOLVED` one.

> Run scenarios one at a time and wait for alerts to clear in between (`scripts/promq.sh 'ALERTS{alertstate="firing"}'`). Alerts still firing from the previous test merge into the next incident.

## Scenarios

Replace `shop`, `api` and `frontend` with your names.

| # | Break | Restore | Expected diagnosis |
|---|---|---|---|
| 1 | `kubectl -n shop set image deploy/api api=example/api:does-not-exist` | `kubectl -n shop rollout undo deploy/api` | **Image pull failure**: names the bad and the previous image; old pods still serving |
| 2 | `kubectl -n shop patch deploy api --type=json -p='[{"op":"replace","path":"/spec/template/spec/containers/0/ports/0/containerPort","value":800}]'` | `kubectl -n shop rollout undo deploy/api` | **Bad deploy**: `containerPort 'http' 8000 -> 800`, probes refused, `rollout undo` |
| 3 | `kubectl -n shop patch ingress <name> --type=json -p='[{"op":"replace","path":"/spec/rules/0/http/paths/0/backend/service/name","value":"frontend-v2"}]'` | patch it back | **Ingress / routing**: service `frontend-v2` does not exist; app pods healthy |
| 4 | `kubectl -n shop patch ingress <name> --type=json -p='[{"op":"replace","path":"/spec/rules/0/host","value":"typo.example.com"}]'` | patch it back | **Ingress / routing**: users request host X, no rule serves it, typo flagged |
| 5 | `kubectl -n shop set resources deploy/api --limits=memory=40Mi` | `rollout undo` | **Memory / OOM**: OOMKilled, working set vs limit |
| 6 | `kubectl -n shop set resources deploy/api --limits=cpu=20m` (under load) | `rollout undo` | **CPU saturation**: throttling, latency |
| 7 | `kubectl -n shop scale statefulset postgres --replicas=0` | scale back to 1 | **Database errors**: DB errors, pg down (if postgres-exporter installed) |

If your app has fault-injection switches (error rate, added latency, slow queries, memory leak, CPU burn, failing liveness), expose the active fault as a metric and set `GROUND_TRUTH_METRIC` — each email then says whether the diagnosis **matched** what you injected.

## Results from the reference setup

Monitoring server: 8 vCPU (Xeon E5-2686 v4, no GPU), 32 GB. App: nginx → FastAPI → Postgres on single-node kubeadm.

| Scenario | Diagnosis | Fault → email | Alert fired | LLM |
|---|---|---|---|---|
| Ingress backend missing | Ingress / routing (HIGH) | **48.9 s** | 17.1 s | 26.0 s |
| Ingress host typo | Ingress / routing (HIGH), typo + TLS mismatch | **46.7 s** | 15.0 s | 24.7 s |
| containerPort 8000 → 800 | Bad deploy (HIGH), exact field | **63.0 s** | 27.6 s | 29.2 s |
| Image tag missing | Image pull failure (HIGH) | 3 m 51 s* | 2 m 18 s* | 85 s* |
| 30% HTTP 500 | Application errors (HIGH) | 3 m 29 s* | 1 m 37 s* | 74 s* |
| DB queries +800 ms | Database slowness (HIGH) | —* | 2 m 38 s* | (old model) |

\* measured before the detection and LLM speed-ups (`PodProbesFailing`, 5 s outside-in checks, prompt caching, lean answers). Re-running them is a good first contribution.

### Where the time goes

| Stage | Typical |
|---|---|
| Failure visible in metrics / outside-in check | 3–23 s |
| Alert fires | 15–28 s |
| Delivered to the diagnoser | +5 s |
| Evidence collection | 0.1 s |
| LLM (CPU) | 25–30 s |
| Email sent | ~1 s |
| **Total** | **~45–65 s** |
