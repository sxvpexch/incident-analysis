# diagnoser/ — the AI diagnoser

A single Python file (standard library only) that turns an Alertmanager incident into an evidence-backed email. How it decides: [docs/how-diagnosis-works.md](../docs/how-diagnosis-works.md).

```bash
diagnoser/deploy.sh          # (re)deploy after changing diagnoser.py or config.env
scripts/diagnose-now.sh      # dry-run diagnosis, prints the result
```

## Settings

Set in `config.env` (rendered into the `diagnoser-config` ConfigMap by `deploy.sh`):

| Variable | Default | Meaning |
|---|---|---|
| `MODEL` | `deepseek-v2:16b` | Any Ollama model |
| `LLM_MODE` | `lean` | `lean`: model writes cause + actions, title/impact from measurements. `full`: model writes everything |
| `LLM_MAX_TOKENS` | `400` | Output cap |
| `LLM_TIMEOUT` | `420` | Seconds before falling back to the rule-engine diagnosis |
| `LLM_THINK` | `false` | Only for reasoning models (keeps their reasoning out of the answer) |
| `QUICK_EMAIL` | `false` | `true`: send the rule-engine email immediately and the AI analysis as a reply in the same thread (for slow models) |
| `APP_NAMESPACE`, `API_APP`, `FRONTEND_APP`, `EXCLUDE_ROUTES`, `APP_INFO_METRIC` | | What to look at |
| `APP_DESCRIPTION` | | One line of context for the model |
| `INGRESS_MANIFEST`, `INGRESS_CONTROLLER_NAMESPACE` | | Used in fix commands and routing evidence |
| `GROUND_TRUTH_METRIC`, `GROUND_TRUTH_LABEL`, `GROUND_TRUTH_MAP` | empty | Optional fault-injection accuracy check |

## Endpoints

`POST /alertmanager` · `POST /diagnose?namespace=&dry_run=1` · `GET /last` (email HTML) · `GET /last.json` · `GET /last-llm.json` (exact prompt evidence + raw model answer) · `GET /metrics` · `GET /healthz`

## Running locally

```bash
export PROM_URL=http://<prometheus>:9090 LOKI_URL=http://<loki>:3100 OLLAMA_URL=http://<ollama>:11434 \
       K8S_API=https://<api-server>:6443 K8S_TOKEN_FILE=./token K8S_CA_FILE=./ca.crt APP_NAMESPACE=shop
python3 diagnoser.py      # listens on :8080; without SMTP settings it logs instead of emailing
```
