# Contributing

Thanks for taking a look. Issues and pull requests are welcome.

## Reporting a bad diagnosis

The most useful issue is a failure the system got wrong. Please include:

- what actually broke (and how you caused it, if on purpose),
- the email you received (remove anything private),
- the output of `GET /last.json` and `GET /last-llm.json` from the diagnoser if you have it:
  ```bash
  kubectl -n ai exec deploy/diagnoser -- python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8080/last-llm.json').read().decode())"
  ```

## Changing the diagnoser

- `diagnoser/diagnoser.py` is intentionally a single file with **no third-party dependencies**, so it runs on a stock `python` image with the code mounted from a ConfigMap. Please keep it that way.
- New evidence goes in an `ev_*` function and the `SECTIONS` map; new root causes go in `analyze()` as a hypothesis with a score and **facts that quote real numbers**.
- Anything the model writes must stay bounded (schema limits) and must never become an executed or cluster-changing command.
- Deploy your change with `diagnoser/deploy.sh` and check it with `scripts/diagnose-now.sh` and at least one scenario from [docs/testing.md](docs/testing.md). Include the timing output in the PR.

## Changing alerts

Edit `monitoring-server/rules/alert-rules.yaml` (it is a template: `${VARS}` come from `config.env`). Explain in the PR what failure the change catches and what noise it might add.

## Good first contributions

- Re-run the scenarios marked `*` in [docs/testing.md](docs/testing.md) with the current settings and update the table.
- Support another ingress controller (NGINX Ingress, Envoy Gateway) in the routing evidence.
- Notification channels besides email (Slack, Teams, webhooks).
- Persist "already notified" state so a diagnoser restart never re-sends an email.
