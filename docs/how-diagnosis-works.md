# How a diagnosis is made

The diagnoser ([`diagnoser/diagnoser.py`](../diagnoser/diagnoser.py)) is a single Python file using only the standard library. Alertmanager posts an incident (everything firing in one namespace) to `/alertmanager`; a worker processes it in four steps.

## 1. Collect evidence (~0.1 s, in parallel)

| Source | Collected |
|---|---|
| **Prometheus** | API request rate, 5xx ratio and p95/p99 overall, per route and per pod, compared with 1 h earlier · DB query p95 per query (and 1 h earlier), DB errors, connection pool, Postgres longest active transaction · per container: memory vs limit and growth over 10 min, CPU vs limit, CFS throttling, restarts, last termination reason · node CPU, memory, disk, I/O · outside-in check status, HTTP code and content match · certificate expiry |
| **Loki** | error lines in the last 10 min grouped by message · HTTP 5xx by route · median duration of failed vs successful requests · Traefik responses ≥ 400 for your hosts (last 3 min) and Traefik error lines about your namespace |
| **Kubernetes API** (read-only) | pods and container states, previous logs of crashed containers · Warning events of the last 30 min · rollout history: newest vs previous revision of each Deployment (image, ports, resources, args, command, env, probes), which pods belong to which revision, last spec change time · ingresses: whether each backend service exists, exposes the port and has ready endpoints; whether the requested host and TLS hosts match the ingress rules |

## 2. Rule engine (deterministic, instant)

Each candidate root cause gets a score (0–1) and the facts that support it. A hypothesis without facts is dropped. Examples:

| Category | Scores high when |
|---|---|
| Bad deploy | newest revision's pods are not Ready **and** previous revision's pods are healthy, change < 1 h old. Facts quote the exact `field old -> new`. Carries the fix command `kubectl rollout undo deployment/<name>`. |
| Image pull failure | containers waiting in `ErrImagePull`/`ImagePullBackOff`, with the image change that caused it |
| Ingress / routing | outside-in check failing while app pods are Ready; higher with a missing backend or host mismatch (`difflib` flags near-typos) |
| Memory leak / OOM | `OOMKilled` (0.9), or near limit **and** growing in containers that are old enough and not restarting |
| Crashloop / probe failure | CrashLoopBackOff with liveness failures and no OOM; capped below a bad rollout |
| Database slowness | query p95 > 250 ms and > 4× its baseline; boosted by pool waits and long Postgres transactions; penalised if the API is CPU-bound |
| CPU saturation | CFS throttling > 25% or CPU > 85% of limit, boosted by latency without slow DB |
| Application errors | 5xx > 5%; facts include failed-vs-ok request duration (fail-fast in code) and what was ruled out (DB errors, restarts, resource pressure, recent changes) |
| Application latency | p95 > 1 s; boosted when > 1 s of it is not explained by DB time |

Confidence: `HIGH` ≥ 0.8, `MEDIUM` ≥ 0.55, else `LOW`. If no hypothesis survives, the email says **No clear root cause found (LOW)** and the LLM is not asked to invent one.

## 3. The LLM writes the explanation

The model (default `deepseek-v2:16b` on Ollama) receives:

- the firing alerts,
- the top 3 hypotheses with their facts,
- a one-line context (API rate/errors/latency, node usage) when the top hypothesis is already ≥ 0.8; the full evidence otherwise.

It answers in a JSON schema enforced by Ollama:

| Field | Limit |
|---|---|
| `category` | one of the categories above |
| `confidence` | HIGH / MEDIUM / LOW |
| `root_cause` | ≤ 280 characters |
| `recommended_actions` | 1–2 items, ≤ 140 characters each |

Guardrails applied to the answer:

- **No invented deploy blame.** If the root cause mentions a deploy/rollout but no change-related hypothesis exists, the model is asked once more with that stated explicitly.
- **No cluster-changing commands from the model.** Model-written commands are kept only if they are read-only (`get`, `describe`, `logs`, `top`). The fix command comes from the rule engine.
- **No loops or truncation.** Schema length limits plus `repeat_penalty` keep the output short and well-formed.
- **Title and user impact are measured, not generated** (lean mode): the title is the strongest fact; the impact comes from outside-in checks and API error/latency numbers.

Speed tricks: the fixed system prompt is cached in the model's KV cache at startup (only the evidence is processed per incident, ~5–7 s), and the answer is ~90–120 tokens (~20 s on 8 CPU cores).

If the model fails or times out (`LLM_TIMEOUT`), the rule-engine result is emailed instead.

## 4. Notify

One HTML email per incident (plus a plain-text part):

```
● [CRITICAL] <title>                                   ← severity colour bar
ROOT CAUSE          model's explanation
CATEGORY · confidence
EVIDENCE            machine-generated facts (never written by the model)
RECOMMENDED ACTION  rule-engine fix command + model's read-only checks
LIVE METRICS        API rate/5xx/p95/p99 · DB pool and slowest query · node CPU/mem/disk/IO · outside-in checks
RECENT CHANGES      rollouts and ingress edits in the last 2 h
USER IMPACT         measured
GROUND TRUTH        (only if GROUND_TRUTH_METRIC is configured)
links: dashboard · error logs · alerts
```

When an incident's alerts resolve, a short `[RESOLVED]` email follows. If a *new kind* of alert joins an open incident, it is re-diagnosed and sent as `UPDATE`; more pods hitting the same alert do not trigger new emails.

## Diagnoser HTTP API

| Endpoint | Purpose |
|---|---|
| `POST /alertmanager` | Alertmanager webhook |
| `POST /diagnose?namespace=<ns>&dry_run=1` | Diagnose now from whatever is firing (or a health check if nothing is); `dry_run=1` sends no email |
| `GET /last` | HTML of the last email |
| `GET /last.json` | Last diagnosis, hypotheses and collection errors |
| `GET /last-llm.json` | Exact evidence text sent to the model and its raw answer |
| `GET /metrics` | Counters: webhooks, diagnoses, fallbacks, emails, LLM seconds |
| `GET /healthz` | Liveness |

## Fault-injection ground truth (optional)

If you run chaos experiments, expose a metric that names the injected fault (e.g. `chaos_mode_active{mode="db_slow_ms"} 800`) and set `GROUND_TRUTH_METRIC` / `GROUND_TRUTH_MAP`. The metric is **withheld** from the rule engine and the model; the email then shows whether the diagnosis matched what you injected — an honest way to measure accuracy.
