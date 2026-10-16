# Lessons learned

Things that surprised us while building and testing this. Most of them changed the design.

## The AI

**A reasoning model was 10× too slow on CPU.** `deepseek-r1:8b` "thinks" before answering, even with thinking disabled, and ran at ~4 tokens/s on 8 older CPU cores. One diagnosis took 10–12 minutes and once timed out. `deepseek-v2:16b` is a mixture-of-experts model (16B parameters, ~2.4B active per token) that answers directly: 66–85 s at first, then ~25 s after the prompt and output changes below. Same family, very different fit for CPU.

**Most of the LLM time was reading, not writing.** One incident spent 42 s processing a 2,500-token evidence prompt and 43 s writing 210 tokens. Fixes: send only the decisive facts when the rule engine is already confident (~300–400 tokens), and cache the fixed system prompt in Ollama's KV cache at startup so only the evidence is processed. Prompt processing dropped to 5–7 s.

**Make the model do less.** Asking for only the root cause and actions (title and user impact come from measurements) shortened answers. Hard limits in the JSON schema (`maxItems`, `maxLength` — Ollama enforces them) plus `repeat_penalty` stopped a failure where the model repeated the same action until it hit the token limit and produced truncated JSON.

**The model will blame whatever is in front of it.** An API error-rate incident was blamed on an unrelated exporter rollout from an hour earlier, simply because "recent changes" was in the prompt. Now changes are shown to the model only when the rule engine links them to the symptoms, and an answer that blames a deploy without that link is sent back once.

**Never let the model write commands that change things.** It once suggested `kubectl scale --all=false deploy/api`, which is not a valid command. Model output is now limited to read-only commands; the only "fix" command comes from the rule engine.

**Evidence first works.** The deterministic rule engine got the category right in every test, including when the LLM failed. The model's value is a readable explanation, not the detection. The evidence block is always machine-generated.

## Detection

**Internal metrics can't see a broken ingress.** With a typo in the ingress host, every pod was healthy and the API error rate was zero while every user got a 404. Only the outside-in check (blackbox exporter loading real URLs and matching page content) caught it.

**Kubernetes events are a poor alert source.** We tried alerting on probe-failure events through the Loki ruler. Kubernetes merges repeated events into one object and bumps a counter, so the event exporter emitted one line for dozens of failures and a "3 in a minute" rule never fired. The kubelet's `prober_probe_total{result="failed"}` counter catches the same failure in ~25 s.

**Labels that flip reset `for:` timers.** An image-pull alert grouped by `reason` never stayed pending long enough: the pod alternates between `ErrImagePull` and `ImagePullBackOff`, so each flip was a new series and restarted the 30 s timer. Dropping `reason` from the grouping fixed it.

**Alertmanager's `group_interval` can dominate.** A new alert joining an incident group that was still open waited for the next 5-minute update cycle: 3.5 minutes of the 6-minute total. Use a short `group_interval` (1 m) and dedupe in the receiver instead.

**Old pods hide bad rollouts.** With `maxUnavailable: 0`, a broken new revision never takes traffic, so error-rate and availability alerts stay quiet. The useful signal is "newest revision's pods failing while the previous revision is healthy", plus a diff of exactly what changed between revisions. Diff the ports too, not just images.

**Tiny sidecars get CPU-throttled.** A 100m-limit exporter tripped the throttling alert all day. Throttling only matters when the container is also using a large share of its limit.

## Kubernetes gotchas

**`kubectl rollout undo` leaves `last-applied-configuration` stale.** After a rollback, re-applying the same broken manifest with `kubectl apply` was a silent no-op: the 3-way merge saw no change between the last-applied file and the new file, so the live (rolled-back) value stayed. Re-apply a corrected manifest once after any rollback.

**A rollback reuses the old ReplicaSet.** Its creation time is old, so "rolled out 25 minutes ago" was wrong for a rollout that was seconds old. Use the Deployment's last spec change time (`managedFields`) instead.

## Infrastructure

**Burstable VMs are a trap for local LLMs.** A t2.2xlarge has full speed only while CPU credits last; sustained inference drains them and can cut throughput by 5–10×. Use a fixed-performance instance.

**Strings with trailing newlines.** A generated Grafana password written with `print()` had a newline the user could never type. Write secrets with `printf '%s'` / `echo -n`.
