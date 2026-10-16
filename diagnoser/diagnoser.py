#!/usr/bin/env python3
"""incident-analysis: evidence-first AI alert diagnoser.

Alertmanager webhook -> collect evidence (Prometheus, Loki, Kubernetes API of the app cluster)
-> rule-based signal analysis -> a local LLM (Ollama) writes the diagnosis -> HTML email.

The evidence block in the email is generated from real data, never by the model. The model only
reasons over that evidence. If you inject faults on purpose (chaos testing), an optional
ground-truth metric is withheld from the analysis and shown afterwards as a check.

Standard library only, so it runs on a stock python image with this file mounted from a ConfigMap.
"""
import html
import difflib
import json
import logging
import os
import queue
import re
import smtplib
import ssl
import threading
import time
import traceback
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ENV = os.environ.get
PROM = ENV("PROM_URL", "http://kps-prometheus.monitoring.svc:9090")
LOKI = ENV("LOKI_URL", "http://loki.monitoring.svc:3100")
OLLAMA = ENV("OLLAMA_URL", "http://ollama.ai.svc:11434")
MODEL = ENV("MODEL", "deepseek-v2:16b")
LLM_THINK = ENV("LLM_THINK", "false") == "true"  # only for reasoning models (e.g. deepseek-r1); keeps reasoning out of the answer
LLM_TIMEOUT = int(ENV("LLM_TIMEOUT", "420"))
# Send the evidence + rule-based diagnosis right away, then the AI analysis as a reply in the same thread.
QUICK_EMAIL = ENV("QUICK_EMAIL", "false") == "true"
K8S_API = ENV("K8S_API", "https://10.0.0.20:6443")
K8S_TOKEN_FILE = ENV("K8S_TOKEN_FILE", "/var/run/app-cluster/token")
K8S_CA_FILE = ENV("K8S_CA_FILE", "/var/run/app-cluster/ca.crt")
CLUSTER = ENV("CLUSTER", "my-cluster")                 # value of the `cluster` label added by the agent
APP_NAMESPACE = ENV("APP_NAMESPACE", "default")        # namespace of the monitored application
API_APP = ENV("API_APP", "api")                        # app.kubernetes.io/name of the HTTP API workload
FRONTEND_APP = ENV("FRONTEND_APP", "frontend")         # app.kubernetes.io/name of the web frontend (optional)
APP_DESCRIPTION = ENV("APP_DESCRIPTION", "a web application")
EXCLUDE_ROUTES = ENV("EXCLUDE_ROUTES", "unmatched")    # regex of `route` label values ignored for latency/errors
APP_INFO_METRIC = ENV("APP_INFO_METRIC", "app_info")   # metric with a `version` label (optional)
INGRESS_MANIFEST = ENV("INGRESS_MANIFEST", "<your ingress manifest>")
PROJECT_NAME = ENV("PROJECT_NAME", "incident-analysis")
REGION = ENV("REGION", "")
DASHBOARD_UID = ENV("DASHBOARD_UID", "service-health")
INGRESS_CONTROLLER_NAMESPACE = ENV("INGRESS_CONTROLLER_NAMESPACE", "traefik")
GRAFANA = ENV("GRAFANA_URL", "https://grafana.example.com")
# Optional fault-injection ground truth: a metric whose `GROUND_TRUTH_LABEL` names the injected fault,
# and a JSON map from that label value to the category a correct diagnosis should produce.
GROUND_TRUTH_METRIC = ENV("GROUND_TRUTH_METRIC", "")
GROUND_TRUTH_LABEL = ENV("GROUND_TRUTH_LABEL", "mode")
SMTP_HOST = ENV("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(ENV("SMTP_PORT", "587"))
SMTP_USER = ENV("SMTP_USER", "")
SMTP_PASSWORD_FILE = ENV("SMTP_PASSWORD_FILE", "/var/run/smtp/password")
MAIL_FROM = ENV("MAIL_FROM", SMTP_USER)
MAIL_TO = [a.strip() for a in ENV("MAIL_TO", "").split(",") if a.strip()]
WINDOW = "5m"            # rate window for "now"
BASELINE_OFFSET = "1h"   # compare against the same window this long ago

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("diagnoser")

CATEGORIES = [
    "MEMORY LEAK / OOM", "CPU SATURATION", "DATABASE SLOWNESS", "DATABASE ERRORS",
    "APPLICATION ERRORS", "APPLICATION LATENCY", "CRASHLOOP / PROBE FAILURE", "BAD DEPLOY",
    "IMAGE PULL FAILURE", "INGRESS / ROUTING", "CERTIFICATE", "NODE PRESSURE", "TELEMETRY GAP", "UNKNOWN",
]
# Example map for a fault-injection metric; override with GROUND_TRUTH_MAP (JSON).
_EXAMPLE_GROUND_TRUTH_MAP = {
    "error_rate": "APPLICATION ERRORS", "latency_ms": "APPLICATION LATENCY",
    "db_slow_ms": "DATABASE SLOWNESS", "leak_kb_per_req": "MEMORY LEAK / OOM",
    "cpu_burn_ms": "CPU SATURATION", "fail_liveness": "CRASHLOOP / PROBE FAILURE",
}
GROUND_TRUTH_MAP = json.loads(ENV("GROUND_TRUTH_MAP", "") or "null") or _EXAMPLE_GROUND_TRUTH_MAP

STATS = {"webhooks": 0, "diagnoses_ok": 0, "diagnoses_fallback": 0, "emails_sent": 0,
         "emails_failed": 0, "llm_seconds_sum": 0.0, "llm_calls": 0, "skipped_duplicates": 0}
LAST = {"html": "<p>No diagnosis yet.</p>", "result": None}
SEEN = {}            # groupKey -> (frozenset(fingerprints), time, title)
JOBS = queue.Queue()


# ---------------------------------------------------------------- data sources

def _http(url, data=None, headers=None, timeout=20, ctx=None, method=None):
    body = None
    if data is not None:
        body = data if isinstance(data, bytes) else json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read()


def prom(q):
    """Instant query -> [(labels, value)]; NaN/Inf dropped."""
    raw = _http(f"{PROM}/api/v1/query?" + urllib.parse.urlencode({"query": q}))
    out = []
    for r in json.loads(raw)["data"]["result"]:
        v = r["value"][1]
        if v in ("NaN", "+Inf", "-Inf"):
            continue
        out.append((r["metric"], float(v)))
    return out


def prom1(q):
    r = prom(q)
    return r[0][1] if r else None


def loki_query(q, minutes=10, limit=300):
    end = time.time_ns()
    params = {"query": q, "start": end - minutes * 60 * 10**9, "end": end,
              "limit": limit, "direction": "backward"}
    raw = _http(f"{LOKI}/loki/api/v1/query_range?" + urllib.parse.urlencode(params), timeout=30)
    return json.loads(raw)["data"]


def loki_instant(q):
    raw = _http(f"{LOKI}/loki/api/v1/query?" + urllib.parse.urlencode({"query": q}), timeout=30)
    return [(r["metric"], float(r["value"][1])) for r in json.loads(raw)["data"]["result"]]


_K8S_CTX = None


def k8s(path, text=False):
    global _K8S_CTX
    if _K8S_CTX is None:
        _K8S_CTX = ssl.create_default_context(cafile=K8S_CA_FILE)
    token = open(K8S_TOKEN_FILE).read().strip()
    raw = _http(K8S_API + path, headers={"Authorization": f"Bearer {token}"}, ctx=_K8S_CTX)
    return raw.decode(errors="replace") if text else json.loads(raw)


def parse_ts(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def ago(dt):
    if not dt:
        return "?"
    s = int((datetime.now(timezone.utc) - dt).total_seconds())
    if s < 90:
        return f"{s}s ago"
    if s < 5400:
        return f"{s // 60}m ago"
    if s < 172800:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m ago"
    return f"{s // 86400}d ago"


def fmt_s(v):
    if v is None:
        return "n/a"
    return f"{v * 1000:.0f}ms" if v < 1 else f"{v:.2f}s"


def fmt_pct(v):
    return "n/a" if v is None else f"{v * 100:.1f}%"


# ---------------------------------------------------------------- evidence collection

def ev_pods(ns):
    pods = []
    for p in k8s(f"/api/v1/namespaces/{ns}/pods")["items"]:
        st = p.get("status", {})
        cs_list = st.get("containerStatuses", []) or []
        containers = []
        for cs in cs_list:
            state = cs.get("state", {})
            last = (cs.get("lastState") or {}).get("terminated")
            c = {"name": cs["name"], "image": cs.get("image"), "ready": cs.get("ready"),
                 "restarts": cs.get("restartCount", 0)}
            if "waiting" in state:
                c["state"] = "waiting:" + state["waiting"].get("reason", "")
            elif "terminated" in state:
                c["state"] = "terminated:" + state["terminated"].get("reason", "")
            else:
                c["state"] = "running"
            if last:
                c["last_terminated"] = {"reason": last.get("reason"), "exit_code": last.get("exitCode"),
                                        "finished": last.get("finishedAt")}
            containers.append(c)
        pods.append({
            "name": p["metadata"]["name"],
            "app": p["metadata"].get("labels", {}).get("app.kubernetes.io/name", ""),
            "phase": st.get("phase"),
            "ready": all(c["ready"] for c in containers) if containers else False,
            "created": p["metadata"].get("creationTimestamp"),
            "containers": containers,
        })
    # Logs from the previous (crashed) container instance are often the single best clue.
    prev = []
    for p in pods:
        for c in p["containers"]:
            if c["restarts"] > 0 and c.get("last_terminated") and len(prev) < 2:
                try:
                    txt = k8s(f"/api/v1/namespaces/{ns}/pods/{p['name']}/log?container={c['name']}"
                              f"&previous=true&tailLines=12", text=True)
                    prev.append({"pod": p["name"], "container": c["name"],
                                 "lines": [ln[:240] for ln in txt.strip().splitlines()[-8:]]})
                except Exception as e:  # previous logs are gone once the container is replaced
                    log.debug("previous logs %s: %s", p["name"], e)
    return {"pods": pods, "previous_logs": prev}


def _container_specs(tmpl):
    out = {}
    for c in tmpl.get("spec", {}).get("containers", []):
        out[c["name"]] = {"image": c.get("image"), "resources": c.get("resources", {}),
                          "ports": {p.get("name") or str(p.get("containerPort")): p.get("containerPort") for p in c.get("ports", []) or []},
                          "args": c.get("args"), "command": c.get("command"),
                          "env": sorted(e["name"] for e in c.get("env", []) or []),
                          "probes": {k: c.get(k) for k in ("livenessProbe", "readinessProbe", "startupProbe")}}
    return out


def ev_changes(ns):
    """Rollout history: what changed in the most recent revision of each workload, and when."""
    deps = k8s(f"/apis/apps/v1/namespaces/{ns}/deployments")["items"]
    rss = k8s(f"/apis/apps/v1/namespaces/{ns}/replicasets")["items"]
    out = []
    for d in deps:
        name = d["metadata"]["name"]
        owned = [r for r in rss if any(o.get("name") == name for o in r["metadata"].get("ownerReferences", []))]
        owned.sort(key=lambda r: int(r["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision", 0)))
        if not owned:
            continue
        cur, prev = owned[-1], (owned[-2] if len(owned) > 1 else None)
        cur_rs = cur["metadata"]["name"]
        st = d.get("status", {})
        spec_changed = max((f["time"] for f in d["metadata"].get("managedFields", [])
                            if f.get("time") and f.get("subresource") != "status" and f.get("manager") != "kube-controller-manager"),
                           default=None)
        item = {"workload": f"deployment/{name}",
                "revision": cur["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision"),
                "rolled_out": max(filter(None, [cur["metadata"]["creationTimestamp"], spec_changed])),
                "replicas": f"{st.get('availableReplicas', 0)}/{d['spec'].get('replicas')} available",
                "current_rs": cur_rs, "all_rs": [r["metadata"]["name"] for r in owned], "changes": []}
        if prev:
            a, b = _container_specs(prev["spec"]["template"]), _container_specs(cur["spec"]["template"])
            for cname in sorted(set(a) | set(b)):
                if cname not in a:
                    item["changes"].append(f"container {cname} added ({b[cname]['image']})")
                    continue
                if cname not in b:
                    item["changes"].append(f"container {cname} removed")
                    continue
                for field in ("image", "ports", "resources", "args", "command", "env", "probes"):
                    if a[cname][field] != b[cname][field]:
                        if field == "image":
                            item["changes"].append(f"{cname} image {a[cname]['image']} -> {b[cname]['image']}")
                        elif field == "ports":
                            pa, pb = a[cname]["ports"], b[cname]["ports"]
                            for pn in sorted(set(pa) | set(pb)):
                                if pa.get(pn) != pb.get(pn):
                                    item["changes"].append(f"{cname} containerPort '{pn}' {pa.get(pn)} -> {pb.get(pn)}")
                        else:
                            item["changes"].append(f"{cname} {field} changed")
            ra = prev["spec"]["template"]["metadata"].get("annotations", {}).get("kubectl.kubernetes.io/restartedAt")
            rb = cur["spec"]["template"]["metadata"].get("annotations", {}).get("kubectl.kubernetes.io/restartedAt")
            if rb and ra != rb and not item["changes"]:
                item["changes"].append("rollout restart (no spec change)")
        else:
            item["changes"].append("initial revision")
        out.append(item)
    for s in k8s(f"/apis/apps/v1/namespaces/{ns}/statefulsets")["items"]:
        st = s.get("status", {})
        out.append({"workload": f"statefulset/{s['metadata']['name']}",
                    "revision": st.get("updateRevision"),
                    "replicas": f"{st.get('readyReplicas', 0)}/{s['spec'].get('replicas')} ready",
                    "rolled_out": None,
                    "changes": (["rolling update in progress"] if st.get("currentRevision") != st.get("updateRevision") else []),
                    "images": [c["image"] for c in s["spec"]["template"]["spec"]["containers"]]})
    return out


def ev_events(ns, minutes=30):
    items = k8s(f"/api/v1/namespaces/{ns}/events")["items"]
    cutoff = time.time() - minutes * 60
    groups = {}
    for e in items:
        ts = parse_ts(e.get("lastTimestamp") or e.get("eventTime") or e["metadata"].get("creationTimestamp"))
        if not ts or ts.timestamp() < cutoff:
            continue
        obj = e.get("involvedObject", {})
        key = (e.get("type"), e.get("reason"), f"{obj.get('kind', '')}/{obj.get('name', '')}")
        g = groups.setdefault(key, {"type": key[0], "reason": key[1], "object": key[2], "count": 0,
                                    "last": ts, "message": ""})
        g["count"] += e.get("count") or 1
        if ts >= g["last"]:
            g["last"], g["message"] = ts, (e.get("message") or "")[:220]
    out = sorted(groups.values(), key=lambda g: (g["type"] != "Warning", -g["count"]))
    for g in out:
        g["last"] = ago(g["last"])
    return out[:14]


def ev_api(ns):
    S = f'cluster="{CLUSTER}",namespace="{ns}"'
    R = f'route!~"{EXCLUDE_ROUTES}"'
    p95 = (f'histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{{{S},{R}}}[{WINDOW}]{{off}})))')
    out = {
        "rps": prom1(f'sum(rate(http_requests_total{{{S}}}[{WINDOW}]))'),
        "error_ratio": prom1(f'sum(rate(http_requests_total{{{S},status=~"5.."}}[{WINDOW}])) / sum(rate(http_requests_total{{{S}}}[{WINDOW}]))'),
        "error_ratio_baseline": prom1(f'sum(rate(http_requests_total{{{S},status=~"5.."}}[{WINDOW}] offset {BASELINE_OFFSET})) / sum(rate(http_requests_total{{{S}}}[{WINDOW}] offset {BASELINE_OFFSET}))'),
        "p95": prom1(p95.replace("{off}", "")),
        "p95_baseline": prom1(p95.replace("{off}", f" offset {BASELINE_OFFSET}")),
        "p99": prom1(f'histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket{{{S},{R}}}[{WINDOW}])))'),
        "status_rps": {m.get("status"): v for m, v in prom(f'sum by (status) (rate(http_requests_total{{{S}}}[{WINDOW}])) > 0')},
        "routes_5xx": [(m.get("route"), v) for m, v in prom(f'topk(4, sum by (route) (rate(http_requests_total{{{S},status=~"5.."}}[{WINDOW}])) > 0)')],
        "routes_p95": [(m.get("route"), v) for m, v in prom(f'topk(5, histogram_quantile(0.95, sum by (route, le) (rate(http_request_duration_seconds_bucket{{{S},{R}}}[{WINDOW}]))))')],
        "pods_5xx": {m.get("pod"): v for m, v in prom(f'sum by (pod) (rate(http_requests_total{{{S},status=~"5.."}}[{WINDOW}])) / sum by (pod) (rate(http_requests_total{{{S}}}[{WINDOW}]))')},
        "version": sorted({m.get("version") for m, _ in prom(f'{APP_INFO_METRIC}{{{S}}}')} - {None}),
    }
    return out


def ev_db(ns):
    S = f'cluster="{CLUSTER}",namespace="{ns}"'
    q = f'histogram_quantile(0.95, sum by (query, le) (rate(db_query_duration_seconds_bucket{{{S}}}[{WINDOW}]{{off}})))'
    now = {m.get("query"): v for m, v in prom(q.replace("{off}", ""))}
    base = {m.get("query"): v for m, v in prom(q.replace("{off}", f" offset {BASELINE_OFFSET}"))}
    return {
        "query_p95": {k: {"now": v, "baseline": base.get(k)} for k, v in sorted(now.items(), key=lambda x: -x[1])},
        "errors_per_s": {m.get("query"): v for m, v in prom(f'sum by (query) (rate(db_errors_total{{{S}}}[{WINDOW}])) > 0')},
        "pool": {m.get("state"): v for m, v in prom(f'sum by (state) (db_pool_connections{{{S}}})')},
        "pool_waiting_max_5m": prom1(f'max(max_over_time(db_pool_connections{{{S},state="waiting"}}[{WINDOW}]))'),
        "pg_up": prom1(f'max(pg_up{{{S}}})'),
        "pg_max_active_tx_s": prom1(f'max(max_over_time(pg_stat_activity_max_tx_duration{{{S},state="active"}}[{WINDOW}]))'),
        "pg_connections": {m.get("state"): v for m, v in prom(f'sum by (state) (pg_stat_activity_count{{{S},datname!=""}}) > 0')},
    }


def ev_resources(ns):
    S = f'cluster="{CLUSTER}",namespace="{ns}",container!="",container!="POD"'
    rows = {}

    def put(metric_q, field):
        for m, v in prom(metric_q):
            rows.setdefault((m.get("pod"), m.get("container")), {})[field] = v
    put(f'max by (pod, container) (container_memory_working_set_bytes{{{S}}})', "mem")
    put(f'max by (pod, container) (kube_pod_container_resource_limits{{{S},resource="memory"}})', "mem_limit")
    put(f'max by (pod, container) (delta(container_memory_working_set_bytes{{{S}}}[10m]))', "mem_delta_10m")
    put(f'sum by (pod, container) (rate(container_cpu_usage_seconds_total{{{S}}}[{WINDOW}]))', "cpu")
    put(f'max by (pod, container) (kube_pod_container_resource_limits{{{S},resource="cpu"}})', "cpu_limit")
    put(f'sum by (pod, container) (rate(container_cpu_cfs_throttled_periods_total{{{S}}}[{WINDOW}])) / sum by (pod, container) (rate(container_cpu_cfs_periods_total{{{S}}}[{WINDOW}]))', "throttled")
    put(f'max by (pod, container) (increase(kube_pod_container_status_restarts_total{{{S}}}[30m]))', "restarts_30m")
    for m, v in prom(f'max by (pod, container, reason) (kube_pod_container_status_last_terminated_reason{{{S}}}) == 1'):
        rows.setdefault((m.get("pod"), m.get("container")), {})["last_reason"] = m.get("reason")
    out = []
    for (pod, cont), r in rows.items():
        if not pod or not cont or "mem" not in r and "cpu" not in r:
            continue
        r.update(pod=pod, container=cont)
        if r.get("mem") and r.get("mem_limit"):
            r["mem_pct"] = r["mem"] / r["mem_limit"]
        if r.get("cpu") is not None and r.get("cpu_limit"):
            r["cpu_pct"] = r["cpu"] / r["cpu_limit"]
        out.append(r)
    out.sort(key=lambda r: r["pod"])
    return out


def ev_edge(ns):
    S = f'cluster="{CLUSTER}",service=~"{ns}-.*"'
    return {
        "traefik_5xx_ratio": {m.get("service"): v for m, v in prom(
            f'sum by (service) (rate(traefik_service_requests_total{{{S},code=~"5.."}}[{WINDOW}])) / sum by (service) (rate(traefik_service_requests_total{{{S}}}[{WINDOW}]))')},
        "traefik_p95": {m.get("service"): v for m, v in prom(
            f'histogram_quantile(0.95, sum by (service, le) (rate(traefik_service_request_duration_seconds_bucket{{{S}}}[{WINDOW}])))')},
        "nginx_up": prom1(f'min(nginx_up{{cluster="{CLUSTER}",namespace="{ns}"}})'),
    }


def ev_logs(ns):
    sel = f'cluster="{CLUSTER}",namespace="{ns}",source="container"'
    out = {"by_level": {}, "top_errors": [], "http_5xx_by_route": {}, "slow_routes_p95_ms": {}}
    for m, v in loki_instant(f'sum by (app, level) (count_over_time({{{sel}}}[10m]))'):
        out["by_level"][f"{m.get('app')}/{m.get('level')}"] = int(v)
    groups = {}
    data = loki_query(f'{{{sel},level=~"error|critical|fatal"}}', minutes=10, limit=300)
    for stream in data.get("result", []):
        app = stream["stream"].get("app", "?")
        for _, line in stream["values"]:
            try:
                j = json.loads(line)
                if j.get("msg") == "request" and j.get("status"):
                    j["msg"] = f"HTTP {j.get('status')} {j.get('method', '')}"
                key = (app, j.get("msg") or j.get("message") or "?", j.get("query") or j.get("route") or "",
                       str(j.get("error") or j.get("exc") or "")[:120])
            except ValueError:
                key = (app, re.sub(r"\d+", "N", line)[:120], "", "")
            g = groups.setdefault(key, {"app": key[0], "msg": key[1], "where": key[2], "error": key[3],
                                        "count": 0, "sample": line[:300]})
            g["count"] += 1
    out["top_errors"] = sorted(groups.values(), key=lambda g: -g["count"])[:6]
    for m, v in loki_instant(f'sum by (route, status) (count_over_time({{{sel},app="{API_APP}"}} | json | status >= 500 [10m]))'):
        out["http_5xx_by_route"][f"{m.get('route')} {m.get('status')}"] = int(v)
    for name, cond in (("failed_ms_p50", "status >= 500"), ("ok_ms_p50", "status < 500")):
        r = loki_instant(f'quantile_over_time(0.5, {{{sel},app="{API_APP}"}} | json | {cond} | route!~"{EXCLUDE_ROUTES}" | unwrap duration_ms [10m]) by (app)')
        out[name] = round(max(v for _, v in r), 1) if r else None
    for m, v in loki_instant(f'quantile_over_time(0.95, {{{sel},app="{API_APP}"}} | json | route!~"{EXCLUDE_ROUTES}" | unwrap duration_ms [10m]) by (route)'):
        out["slow_routes_p95_ms"][m.get("route")] = round(v, 1)
    return out


def ev_node(_ns):
    S = f'cluster="{CLUSTER}"'
    return {
        "cpu": prom1(f'1 - avg(rate(node_cpu_seconds_total{{{S},mode="idle"}}[{WINDOW}]))'),
        "mem": prom1(f'1 - sum(node_memory_MemAvailable_bytes{{{S}}}) / sum(node_memory_MemTotal_bytes{{{S}}})'),
        "disk": prom1(f'1 - min(node_filesystem_avail_bytes{{{S},mountpoint="/"}} / node_filesystem_size_bytes{{{S},mountpoint="/"}})'),
        "iowait": prom1(f'avg(rate(node_cpu_seconds_total{{{S},mode="iowait"}}[{WINDOW}]))'),
        "load1": prom1(f'max(node_load1{{{S}}})'),
        "disk_read_iops": prom1(f'sum(rate(node_disk_reads_completed_total{{{S},device!~"loop.*"}}[{WINDOW}]))'),
        "disk_write_iops": prom1(f'sum(rate(node_disk_writes_completed_total{{{S},device!~"loop.*"}}[{WINDOW}]))'),
    }


def ev_certs(_ns):
    return {m.get("name"): round(v / 86400, 1) for m, v in
            prom(f'certmanager_certificate_expiration_timestamp_seconds{{cluster="{CLUSTER}"}} - time()')}


def ev_probes(ns):
    """Outside-in checks from the blackbox exporter (what a user sees)."""
    S = f'cluster="{CLUSTER}",namespace="{ns}"'
    out = {}
    for metric, field in (("probe_success", "ok"), ("probe_http_status_code", "status"),
                          ("probe_failed_due_to_regex", "wrong_content"), ("probe_duration_seconds", "seconds")):
        for m, v in prom(f'{metric}{{{S}}}'):
            out.setdefault(m.get("instance"), {})[field] = v
    for m, v in prom(f'min_over_time(probe_success{{{S}}}[30m])'):
        out.setdefault(m.get("instance"), {})["failed_in_30m"] = v == 0
    return out


def ev_ingress(ns):
    """Ingress rules -> do their backend services exist, expose the port, and have ready endpoints?"""
    ings = k8s(f"/apis/networking.k8s.io/v1/namespaces/{ns}/ingresses")["items"]
    svcs = {x["metadata"]["name"]: x for x in k8s(f"/api/v1/namespaces/{ns}/services")["items"]}
    eps = {x["metadata"]["name"]: x for x in k8s(f"/api/v1/namespaces/{ns}/endpoints")["items"]}
    out = {"ingresses": [], "traefik_status": {}, "traefik_errors": []}
    for ing in ings:
        md = ing["metadata"]
        changed = max((f["time"] for f in md.get("managedFields", []) if f.get("time")), default=None)
        last_mgr = max((f for f in md.get("managedFields", []) if f.get("time")), key=lambda f: f["time"], default={})
        item = {"name": md["name"], "changed": changed, "changed_by": last_mgr.get("manager"), "paths": []}
        for r in ing["spec"].get("rules", []):
            for p in r.get("http", {}).get("paths", []):
                b = p.get("backend", {}).get("service", {})
                name, want = b.get("name"), b.get("port", {}).get("name") or b.get("port", {}).get("number")
                probs, svc = [], svcs.get(name)
                if not svc:
                    probs.append(f"service '{name}' does not exist (existing: {', '.join(sorted(svcs))})")
                else:
                    if not any(pp.get("name") == want or pp.get("port") == want for pp in svc["spec"].get("ports", [])):
                        probs.append(f"service '{name}' has no port '{want}'")
                    ready = sum(len(ss.get("addresses", []) or []) for ss in (eps.get(name) or {}).get("subsets", []) or [])
                    if not ready:
                        probs.append(f"service '{name}' has no ready endpoints")
                item["paths"].append({"host": r.get("host"), "path": p.get("path"), "service": name,
                                      "port": want, "problems": probs})
        out["ingresses"].append(item)
    ing_hosts = {pth["host"] for i in out["ingresses"] for pth in i["paths"] if pth["host"]}
    tls_hosts = {h for i in ings for t in i["spec"].get("tls", []) for h in t.get("hosts", [])}
    # the hosts users actually request = the external check targets
    user_hosts = {urllib.parse.urlparse(m.get("instance", "")).hostname for m, _ in
                  prom(f'probe_success{{cluster="{CLUSTER}",namespace="{ns}"}}')} - {None}
    out["host_mismatch"] = []
    for h in sorted(user_hosts - ing_hosts):
        near = difflib.get_close_matches(h, list(ing_hosts), n=1, cutoff=0.6)
        out["host_mismatch"].append(
            f"users request host '{h}' but no ingress rule serves it; ingress hosts are {sorted(ing_hosts) or 'none'}"
            + (f" ('{near[0]}' looks like a typo of '{h}')" if near else ""))
    for h in sorted(ing_hosts - tls_hosts):
        out["host_mismatch"].append(f"ingress host '{h}' is not covered by the TLS section (tls hosts: {sorted(tls_hosts)})")
    for h in sorted(user_hosts | ing_hosts):
        for m, v in loki_instant(f'sum by (DownstreamStatus, RouterName) (count_over_time({{cluster="{CLUSTER}",namespace="{INGRESS_CONTROLLER_NAMESPACE}"}} '
                                 f'| json | RequestHost="{h}" | DownstreamStatus >= 400 [3m]))'):
            out["traefik_status"][f"{h} HTTP {m.get('DownstreamStatus')} router={m.get('RouterName') or 'NONE'}"] = int(v)
    groups = {}
    # only the last 3 minutes and only lines about this namespace: older errors belong to earlier incidents
    data = loki_query(f'{{cluster="{CLUSTER}",namespace="{INGRESS_CONTROLLER_NAMESPACE}",level=~"error|warning"}} != "DownstreamStatus" |= "{ns}"', minutes=3, limit=200)
    for st in data.get("result", []):
        for _, line in st["values"]:
            try:
                j = json.loads(line)
                msg = f"{j.get('error') or ''} {j.get('message') or j.get('msg') or ''}".strip()
            except ValueError:
                msg = line
            msg = re.sub(r"\d+", "N", msg)[:200]
            groups[msg] = groups.get(msg, 0) + 1
    out["traefik_errors"] = sorted(groups.items(), key=lambda x: -x[1])[:3]
    return out


def ev_chaos(ns):
    """Optional ground truth for fault-injection tests (never shown to the analysis or the model)."""
    if not GROUND_TRUTH_METRIC:
        return {}
    S = f'cluster="{CLUSTER}",namespace="{ns}"'
    return {m.get(GROUND_TRUTH_LABEL): v for m, v in
            prom(f'max by ({GROUND_TRUTH_LABEL}) ({GROUND_TRUTH_METRIC}{{{S}}}) > 0')}


def ev_telemetry(_ns):
    return {"app_cluster_series_up": prom1(f'count(up{{cluster="{CLUSTER}"}} == 1)'),
            "app_cluster_targets_down": [m.get("job") for m, _ in prom(f'up{{cluster="{CLUSTER}"}} == 0')]}


SECTIONS = {"pods": ev_pods, "changes": ev_changes, "events": ev_events, "api": ev_api, "db": ev_db,
            "resources": ev_resources, "edge": ev_edge, "logs": ev_logs, "node": ev_node,
            "certs": ev_certs, "chaos": ev_chaos, "telemetry": ev_telemetry,
            "probes": ev_probes, "ingress": ev_ingress}


def collect(ns):
    ev = {"namespace": ns, "collection_errors": []}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = {name: pool.submit(fn, ns) for name, fn in SECTIONS.items()}
        for name, f in futs.items():
            try:
                ev[name] = f.result(timeout=60)
            except Exception as e:
                ev[name] = None
                ev["collection_errors"].append(f"{name}: {e}")
                log.warning("evidence %s failed: %s", name, e)
    return ev


# ---------------------------------------------------------------- rule-based signal analysis

def analyze(ev, alerts):
    """Score candidate root causes from the evidence. Each hypothesis carries the facts that support it.
    The chaos ground truth is deliberately NOT used here."""
    api, db, res = ev.get("api") or {}, ev.get("db") or {}, ev.get("resources") or []
    logs, events, changes = ev.get("logs") or {}, ev.get("events") or [], ev.get("changes") or []
    node, pods = ev.get("node") or {}, (ev.get("pods") or {}).get("pods", [])
    H = []

    def hyp(cat, score, facts):
        H.append({"category": cat, "score": score, "facts": [f for f in facts if f]})

    api_rows = [r for r in res if r["pod"].startswith(f"{API_APP}-")]
    api_cpu_max = max((r.get("cpu_pct") or 0 for r in api_rows), default=0)
    api_thr_max = max((r.get("throttled") or 0 for r in api_rows), default=0)
    p95, p95b = api.get("p95"), api.get("p95_baseline")
    err, errb = api.get("error_ratio"), api.get("error_ratio_baseline")
    latency_bad = p95 is not None and p95 > 1.0
    errors_bad = err is not None and err > 0.05
    warn_events = [e for e in events if e["type"] == "Warning"]
    alert_names = {a["labels"].get("alertname") for a in alerts}

    # Bad rollout: pods of the newest revision fail while pods of the previous revision are healthy
    bad_rollout = set()
    for c in changes:
        if not c.get("current_rs"):
            continue
        ts = parse_ts(c.get("rolled_out"))
        real = [x for x in c["changes"] if x not in ("initial revision", "rollout restart (no spec change)")]
        new = [p for p in pods if p["name"].startswith(c["current_rs"] + "-")]
        old = [p for p in pods if any(p["name"].startswith(r + "-") for r in c["all_rs"] if r != c["current_rs"])]
        new_bad = [p for p in new if not p["ready"]]
        if not (ts and real and new_bad and (datetime.now(timezone.utc) - ts).total_seconds() < 3600):
            continue
        wl = c["workload"].split("/")[-1]
        facts = [f"{c['workload']} revision {c['revision']} rolled out {ago(ts)} changed: " + "; ".join(real[:3])]
        for p in new_bad[:2]:
            cs = p["containers"][0] if p["containers"] else {}
            lt = cs.get("last_terminated") or {}
            facts.append(f"new pod {p['name']}: not Ready, {cs.get('state')}, restarts={cs.get('restarts')}"
                         + (f", last exit {lt.get('reason')}({lt.get('exit_code')})" if lt else ""))
        if old:
            facts.append(f"previous revision pods {', '.join(p['name'] for p in old)}: "
                         + ("all Ready and serving" if all(p["ready"] for p in old) else "also unhealthy"))
        facts += [f"event {e['reason']} x{e['count']}: {e['message'][:170]}" for e in warn_events
                  if any(e["object"].lower().endswith("/" + p["name"]) for p in new_bad)][:2]
        score = 0.93 if old and all(p["ready"] for p in old) else 0.75
        H.append({"category": "BAD DEPLOY", "score": score, "facts": facts,
                  "action": f"kubectl -n {ev['namespace']} rollout undo deployment/{wl}"})
        bad_rollout.add(c["workload"])

    # Ingress / routing: users get errors from outside while the app itself is healthy
    probes = ev.get("probes") or {}
    failing = {u: p for u, p in probes.items() if p.get("ok") == 0}
    if failing:
        ing = ev.get("ingress") or {}
        facts = [f"external check {u}: HTTP {p.get('status', 0):.0f}" + (" with wrong page content" if p.get("wrong_content") else "")
                 for u, p in failing.items()]
        okp = [u for u, p in probes.items() if p.get("ok") == 1]
        if okp:
            facts.append("still working from outside: " + ", ".join(okp))
        broken = [(i, pth) for i in ing.get("ingresses", []) for pth in i["paths"] if pth["problems"]]
        facts += (ing.get("host_mismatch") or [])[:2]
        for i, pth in broken[:2]:
            facts.append(f"ingress {i['name']} path {pth['path']} -> service {pth['service']}:{pth['port']}: " + "; ".join(pth["problems"]))
        for i in ing.get("ingresses", []):
            ct = parse_ts(i.get("changed"))
            if ct and (datetime.now(timezone.utc) - ct).total_seconds() < 3600:
                facts.append(f"ingress {i['name']} was modified {ago(ct)} (by {i.get('changed_by')})")
        for k, n in list((ing.get("traefik_status") or {}).items())[:2]:
            facts.append(f"Traefik answered {k} x{n} in the last 3m")
        facts += [f"Traefik log x{n}: {m[:150]}" for m, n in (ing.get("traefik_errors") or [])[:1]]
        app_pods = [p for p in pods if p["app"] in (API_APP, FRONTEND_APP)]
        if app_pods and all(p["ready"] for p in app_pods) and not errors_bad:
            facts.append(f"all {len(app_pods)} {API_APP}/{FRONTEND_APP} pods Ready, API 5xx {fmt_pct(err)} -> the app is healthy; requests are not reaching it")
        hyp("INGRESS / ROUTING", 0.95 if (broken or ing.get("host_mismatch")) else 0.7, facts)

    # Memory leak / OOM
    oom = [r for r in res if r.get("last_reason") == "OOMKilled"]
    oom_pods = [(p["name"], c) for p in pods for c in p["containers"]
                if (c.get("last_terminated") or {}).get("reason") == "OOMKilled"]
    young = {p["name"] for p in pods if parse_ts(p.get("created")) and
             (datetime.now(timezone.utc) - parse_ts(p["created"])).total_seconds() < 900}
    growing = [r for r in res if (r.get("mem_delta_10m") or 0) > 20 * 2**20 and r["pod"] not in young
               and not (r.get("restarts_30m") or 0) and r.get("mem_limit")]
    near = [r for r in res if (r.get("mem_pct") or 0) > 0.8]
    if oom or oom_pods or near or growing:
        facts = []
        for pname, c in oom_pods[:3]:
            lt = c["last_terminated"]
            facts.append(f"{pname}/{c['name']}: last terminated OOMKilled (exit {lt.get('exit_code')}) "
                         f"{ago(parse_ts(lt.get('finished')))}, restarts={c['restarts']}")
        for r in sorted({id(x): x for x in near + growing + oom}.values(), key=lambda r: -(r.get("mem_pct") or 0))[:3]:
            if r.get("mem_pct"):
                facts.append(f"{r['pod']}/{r['container']}: memory {r['mem'] / 2**20:.0f}Mi / {r['mem_limit'] / 2**20:.0f}Mi "
                             f"({r['mem_pct'] * 100:.0f}% of limit), +{(r.get('mem_delta_10m') or 0) / 2**20:.0f}Mi in 10m")
        score = 0.9 if (oom or oom_pods) else (0.7 if growing and near else 0.45)
        if (oom or oom_pods) and growing:
            score = 0.95
        hyp("MEMORY LEAK / OOM", score, facts)

    # Crashloop / probe failures (not caused by OOM)
    crash = [(p["name"], c) for p in pods for c in p["containers"] if "CrashLoopBackOff" in c.get("state", "")]
    probe_ev = [e for e in warn_events if e["reason"] in ("Unhealthy", "BackOff", "Killing")
                or "probe failed" in e["message"].lower()]
    if crash or (probe_ev and any(c["restarts"] for p in pods for c in p["containers"])):
        facts = [f"{n}/{c['name']}: {c['state']}, restarts={c['restarts']}, last exit "
                 f"{(c.get('last_terminated') or {}).get('reason')}({(c.get('last_terminated') or {}).get('exit_code')})"
                 for n, c in crash[:3]]
        facts += [f"event {e['reason']} x{e['count']} on {e['object']}: {e['message'][:140]}" for e in probe_ev[:3]]
        liveness = any("liveness" in e["message"].lower() for e in probe_ev)
        score = 0.9 if (crash and liveness and not oom_pods) else (0.6 if not oom_pods else 0.3)
        if bad_rollout:
            score = min(score, 0.6)
        hyp("CRASHLOOP / PROBE FAILURE", score, facts)

    # Image pull failures: a new pod cannot start because its image cannot be pulled
    pulls = [(p["name"], c) for p in pods for c in p["containers"]
             if any(r in c.get("state", "") for r in ("ImagePullBackOff", "ErrImagePull", "InvalidImageName"))]
    if pulls:
        facts = [f"{n}/{c['name']}: {c['state'].split(':')[-1]} for image {c['image']}" for n, c in pulls[:3]]
        facts += [f"event {e['reason']} x{e['count']} on {e['object']}: {e['message'][:160]}" for e in warn_events
                  if e["reason"] in ("Failed", "BackOff", "ErrImagePull") and "pod/" in e["object"].lower()][:2]
        for c in changes:
            img = [x for x in c["changes"] if "image" in x]
            if img and any(n.startswith(c["workload"].split("/")[-1] + "-") for n, _ in pulls):
                facts.append(f"{c['workload']} revision {c['revision']} rolled out {ago(parse_ts(c.get('rolled_out')))}: {img[0]}")
                facts.append(f"{c['workload']}: {c['replicas']} (old pods still serving; rollout is stuck)")
        hyp("IMAGE PULL FAILURE", 0.95, facts)

    # Database slowness: DB-side time up while app CPU is normal
    qp = db.get("query_p95") or {}
    slow_q = {k: v for k, v in qp.items() if v["now"] > 0.25 and (v["baseline"] is None or v["now"] > 4 * max(v["baseline"], 0.005))}
    if slow_q:
        facts = [f"db query {k}: p95 {fmt_s(v['now'])} (1h ago {fmt_s(v['baseline'])})" for k, v in list(slow_q.items())[:4]]
        if db.get("pool_waiting_max_5m"):
            facts.append(f"DB pool: up to {db['pool_waiting_max_5m']:.0f} requests waiting for a connection "
                         f"(size={db.get('pool', {}).get('size', 0):.0f}, available={db.get('pool', {}).get('available', 0):.0f})")
        if db.get("pg_max_active_tx_s"):
            facts.append(f"Postgres: longest active transaction {fmt_s(db['pg_max_active_tx_s'])} (pg_stat_activity)")
        facts.append(f"api CPU max {api_cpu_max * 100:.0f}% of limit, throttled {api_thr_max * 100:.0f}% -> app is not CPU-bound")
        score = 0.6 + (0.15 if db.get("pool_waiting_max_5m") else 0) + (0.1 if (db.get("pg_max_active_tx_s") or 0) > 0.5 else 0) \
            + (0.1 if api_cpu_max < 0.6 else -0.2)
        hyp("DATABASE SLOWNESS", score, facts)

    # Database errors
    if db.get("errors_per_s") or db.get("pg_up") == 0:
        facts = [f"db_errors_total {k}: {v:.2f}/s" for k, v in db["errors_per_s"].items()]
        facts += [f"log '{g['msg']}' x{g['count']} ({g['where']}): {g['error']}" for g in logs.get("top_errors", []) if "db" in g["msg"].lower()][:2]
        if db.get("pg_up") == 0:
            facts.append("postgres-exporter: pg_up=0 (cannot connect)")
        hyp("DATABASE ERRORS", 0.85 if db.get("pg_up") == 0 else 0.7, facts)

    # CPU saturation
    if api_thr_max > 0.25 or api_cpu_max > 0.85:
        top = sorted(api_rows, key=lambda r: -(r.get("throttled") or 0))[:2]
        facts = [f"{r['pod']}: CPU {r.get('cpu', 0) * 1000:.0f}m of {r.get('cpu_limit', 0) * 1000:.0f}m limit "
                 f"({(r.get('cpu_pct') or 0) * 100:.0f}%), CFS-throttled {(r.get('throttled') or 0) * 100:.0f}% of periods" for r in top]
        if latency_bad:
            facts.append(f"API p95 {fmt_s(p95)} (1h ago {fmt_s(p95b)}) while DB queries stay fast")
        score = 0.55 + (0.2 if api_thr_max > 0.4 else 0) + (0.15 if latency_bad and not slow_q else 0)
        hyp("CPU SATURATION", score, facts)

    # Application errors (5xx not explained by DB or crashes)
    if errors_bad:
        facts = [f"API 5xx ratio {fmt_pct(err)} (1h ago {fmt_pct(errb)}), {api.get('rps') or 0:.1f} req/s"]
        facts += [f"route {r}: {v:.2f} 5xx/s" for r, v in (api.get("routes_5xx") or [])[:3]]
        pods5 = api.get("pods_5xx") or {}
        if len(pods5) > 1:
            facts.append("5xx spread across pods: " + ", ".join(f"{p} {v * 100:.0f}%" for p, v in pods5.items()))
        fms, oms = logs.get("failed_ms_p50"), logs.get("ok_ms_p50")
        if fms is not None and oms is not None:
            facts.append(f"failed requests take {fms}ms (p50) vs {oms}ms for successful ones"
                         + (" -> they fail immediately in app code, before any DB work" if fms < max(oms, 1) * 0.5 else ""))
        ruled_out = []
        if not db.get("errors_per_s"):
            ruled_out.append("no DB errors")
        if not any((r.get("restarts_30m") or 0) > 0 for r in api_rows):
            ruled_out.append("no api restarts")
        if not any((r.get("mem_pct") or 0) > 0.8 or (r.get("throttled") or 0) > 0.25 for r in api_rows):
            ruled_out.append("no memory/CPU pressure")
        if not any(c["workload"] == "deployment/api" and parse_ts(c.get("rolled_out")) and
                   (datetime.now(timezone.utc) - parse_ts(c["rolled_out"])).total_seconds() < 3600 and
                   any(x not in ("initial revision", "rollout restart (no spec change)") for x in c["changes"]) for c in changes):
            ruled_out.append("no api spec/image change in the last hour")
        if ruled_out:
            facts.append("ruled out: " + ", ".join(ruled_out))
        facts += [f"log [{g['app']}] '{g['msg']}' x{g['count']} e.g. {g['where']}" for g in logs.get("top_errors", [])[:1]]
        tr = (ev.get("edge") or {}).get("traefik_5xx_ratio") or {}
        facts += [f"Traefik {s}: {v * 100:.0f}% 5xx (users see these errors)" for s, v in tr.items() if v > 0.01][:1]
        score = 0.75 - (0.3 if db.get("errors_per_s") else 0) - (0.3 if crash else 0) - (0.2 if oom_pods else 0)
        hyp("APPLICATION ERRORS", score, facts)

    # Application latency (not CPU, not DB)
    if latency_bad:
        facts = [f"API p95 {fmt_s(p95)}, p99 {fmt_s(api.get('p99'))} (p95 1h ago {fmt_s(p95b)})"]
        facts += [f"route {r}: p95 {fmt_s(v)}" for r, v in (api.get("routes_p95") or [])[:3]]
        fast_db = max((v["now"] for v in qp.values()), default=0)
        facts.append(f"slowest DB query p95 {fmt_s(fast_db)}; api CPU max {api_cpu_max * 100:.0f}% of limit, throttled {api_thr_max * 100:.0f}%")
        slowr = logs.get("slow_routes_p95_ms") or {}
        if slowr:
            facts.append("access logs p95 duration_ms: " + ", ".join(f"{k}={v:.0f}" for k, v in list(slowr.items())[:3]))
        uniform = len([1 for _, v in (api.get("routes_p95") or []) if v > 1]) >= 2
        # latency the DB cannot explain: API p95 well above the slowest query p95
        unexplained = p95 - fast_db
        if slow_q and unexplained > 1.0:
            facts.append(f"{unexplained:.2f}s of API p95 is NOT explained by DB time -> extra delay inside the app")
        score = 0.5 + (0.25 if not slow_q and api_thr_max < 0.2 else 0.15 if unexplained > 1.0 and api_thr_max < 0.2 else -0.25) + (0.1 if uniform else 0)
        hyp("APPLICATION LATENCY", score, facts)

    # Recent change (bad deploy): only a suspect when there is also a symptom
    symptoms = [h for h in H if h["score"] >= 0.45]
    alert_pods = {a["labels"].get("pod", "") for a in alerts}
    for c in (changes if symptoms else []):
        if c["workload"] in bad_rollout:
            continue
        ts = parse_ts(c.get("rolled_out"))
        real = [x for x in c["changes"] if x not in ("initial revision", "rollout restart (no spec change)")]
        wl = c["workload"].split("/")[-1]
        related = any("image" in x for x in real) or any(p.startswith(wl + "-") for p in alert_pods)
        if ts and real and related and (datetime.now(timezone.utc) - ts).total_seconds() < 3600:
            hyp("BAD DEPLOY", 0.5 + (0.2 if any("image" in x for x in real) else 0),
                [f"{c['workload']} revision {c['revision']} rolled out {ago(ts)}: " + "; ".join(real[:3]),
                 f"{c['workload']}: {c['replicas']}"])

    if node.get("mem", 0) and node["mem"] > 0.9 or (node.get("disk") or 0) > 0.85 or (node.get("cpu") or 0) > 0.9:
        hyp("NODE PRESSURE", 0.6, [f"app node CPU {fmt_pct(node.get('cpu'))}, memory {fmt_pct(node.get('mem'))}, disk {fmt_pct(node.get('disk'))}"])
    if "CertificateExpiringSoon" in alert_names or "CertificateNotReady" in alert_names:
        hyp("CERTIFICATE", 0.8, [f"certificate {k}: expires in {v} days" for k, v in (ev.get("certs") or {}).items()])
    if "AppClusterTelemetryMissing" in alert_names:
        hyp("TELEMETRY GAP", 0.9, [f"series up from app cluster: {(ev.get('telemetry') or {}).get('app_cluster_series_up')}"])

    H = [h for h in H if h["facts"]]          # a hypothesis without supporting facts is noise
    H.sort(key=lambda h: -h["score"])
    for h in H:
        h["score"] = round(min(max(h["score"], 0.05), 0.99), 2)
        h["confidence"] = "HIGH" if h["score"] >= 0.8 else "MEDIUM" if h["score"] >= 0.55 else "LOW"
    return H


# ---------------------------------------------------------------- LLM

def evidence_text(ev, alerts, hyps):
    """Compact, human-readable evidence for the model (no chaos ground truth)."""
    L = []
    L.append("FIRING ALERTS:")
    for a in alerts:
        lb = a["labels"]
        L.append(f"- {lb.get('alertname')} [{lb.get('severity')}] {a['annotations'].get('summary', '')} (since {ago(parse_ts(a.get('startsAt')))})")
    L.append("\nAUTOMATED SIGNAL ANALYSIS (candidate root causes, score 0-1, with supporting facts):")
    for h in hyps[:3]:
        L.append(f"* {h['category']} score={h['score']}")
        L += [f"    - {f}" for f in h["facts"][:7]]
    if hyps and hyps[0]["score"] >= 0.8:
        # Focused mode: the decisive facts are above. Keep the prompt small so the model answers fast.
        api, node = ev.get("api") or {}, ev.get("node") or {}
        L.append(f"\nCONTEXT: API {api.get('rps') or 0:.1f} req/s, 5xx {fmt_pct(api.get('error_ratio'))}, p95 {fmt_s(api.get('p95'))}; "
                 f"node CPU {fmt_pct(node.get('cpu'))}, mem {fmt_pct(node.get('mem'))}")
        return "\n".join(L)
    api = ev.get("api") or {}
    if api:
        L.append(f"\nAPI: {api.get('rps') or 0:.2f} req/s, 5xx {fmt_pct(api.get('error_ratio'))} (1h ago {fmt_pct(api.get('error_ratio_baseline'))}), "
                 f"p95 {fmt_s(api.get('p95'))} (1h ago {fmt_s(api.get('p95_baseline'))}), p99 {fmt_s(api.get('p99'))}, versions {api.get('version')}")
        L.append("  status req/s: " + ", ".join(f"{k}={v:.2f}" for k, v in (api.get("status_rps") or {}).items()))
    db = ev.get("db") or {}
    if db:
        L.append("DB p95 per query: " + ", ".join(f"{k}={fmt_s(v['now'])} (was {fmt_s(v['baseline'])})" for k, v in (db.get("query_p95") or {}).items()))
        L.append(f"  pool {db.get('pool')}, max waiting 5m={db.get('pool_waiting_max_5m')}, errors/s {db.get('errors_per_s')}, "
                 f"pg_up={db.get('pg_up')}, longest active tx {fmt_s(db.get('pg_max_active_tx_s'))}")
    L.append("CONTAINERS (mem/limit, cpu/limit, throttled, restarts 30m, last termination):")
    for r in ev.get("resources") or []:
        L.append(f"  {r['pod']}/{r['container']}: mem {r.get('mem', 0) / 2**20:.0f}Mi/{(r.get('mem_limit') or 0) / 2**20:.0f}Mi "
                 f"(+{(r.get('mem_delta_10m') or 0) / 2**20:.0f}Mi/10m), cpu {(r.get('cpu') or 0) * 1000:.0f}m/{(r.get('cpu_limit') or 0) * 1000:.0f}m, "
                 f"throttled {(r.get('throttled') or 0) * 100:.0f}%, restarts {r.get('restarts_30m', 0):.0f}, last {r.get('last_reason', '-')}")
    for p in (ev.get("pods") or {}).get("pods", []):
        bad = [c for c in p["containers"] if c["state"] != "running" or not c["ready"]]
        if bad or p["phase"] != "Running":
            L.append(f"  POD {p['name']} phase={p['phase']} " + "; ".join(f"{c['name']} {c['state']} ready={c['ready']} restarts={c['restarts']}" for c in bad))
    for pl in (ev.get("pods") or {}).get("previous_logs", []):
        L.append(f"PREVIOUS CONTAINER LOGS {pl['pod']}/{pl['container']}:")
        L += [f"  {x}" for x in pl["lines"][-5:]]
    if any(h["category"] in ("BAD DEPLOY", "IMAGE PULL FAILURE") for h in hyps):
        L.append("RECENT CHANGES (correlated with the symptoms):")
        for c in ev.get("changes") or []:
            L.append(f"  {c['workload']} rev {c['revision']} ({c['replicas']}) rolled out {ago(parse_ts(c.get('rolled_out')))}: {', '.join(c['changes']) or 'no change'}")
    else:
        L.append("RECENT CHANGES: none related to these symptoms (do not blame a deploy)")
    L.append("KUBERNETES EVENTS (30m):")
    for e in (ev.get("events") or [])[:8]:
        L.append(f"  {e['type']} {e['reason']} x{e['count']} {e['object']} ({e['last']}): {e['message'][:160]}")
    lg = ev.get("logs") or {}
    if lg:
        L.append("LOGS (10m) count by app/level: " + json.dumps(lg.get("by_level")))
        L.append(f"  api request duration p50: failed={lg.get('failed_ms_p50')}ms, successful={lg.get('ok_ms_p50')}ms")
        for g in lg.get("top_errors", [])[:4]:
            L.append(f"  error x{g['count']} [{g['app']}] {g['msg']} {g['where']} {g['error']}")
    node = ev.get("node") or {}
    L.append(f"NODE: cpu {fmt_pct(node.get('cpu'))}, mem {fmt_pct(node.get('mem'))}, disk {fmt_pct(node.get('disk'))}, iowait {fmt_pct(node.get('iowait'))}")
    return "\n".join(L)


SYSTEM_PROMPT = """You are a senior Kubernetes SRE on call for __APP__ in namespace __NS__.
You receive firing alerts plus evidence that was already collected from Prometheus, Loki and the Kubernetes API, and an automated
signal analysis that ranks candidate root causes. Decide the single most likely ROOT CAUSE (not the symptom).
Rules:
- Use only facts present in the evidence; quote real numbers, pod names, routes and queries. Never invent values.
- Distinguish cause from symptom: e.g. slow DB queries with normal app CPU means the database is the cause, not the API.
  OOMKilled with memory climbing per request means a memory leak; CFS throttling with normal DB means CPU saturation;
  high latency with fast DB and no throttling means added latency inside the application; liveness probe failures with
  restarts and no OOM means a probe/crash problem; 5xx with no DB errors and healthy pods means application errors;
  only the NEWEST revision's pods failing while the previous revision's pods are Ready means that revision's change is the
  cause: quote the exact changed field with old -> new value (e.g. containerPort 'http' 8000 -> 800) and explain how it
  breaks the pod (e.g. probes target the named port, so they now hit a port the app does not listen on);
  ErrImagePull/ImagePullBackOff right after an image change means the new image reference is wrong or unavailable:
  name the bad image and the previous good one, and recommend kubectl rollout undo;
  an external check failing while app pods are Ready and the API is healthy means routing: name the ingress path and
  its broken backend (e.g. a service that does not exist) or the host mismatch (quote both hostnames), and recommend
  restoring the ingress from git: kubectl -n __NS__ apply -f __INGRESS__
- If a recent rollout changed an image right before the symptoms, say so; otherwise do not blame a deploy.
- Be brief: root_cause is 1-2 sentences citing the decisive facts. 2-3 actions, one line each. user_impact one sentence.
- Keep your private reasoning short (under 200 words): pick the cause, check it against the evidence, answer.
- recommended_actions: 2-4 concrete steps, prefer exact kubectl commands with -n __NS__.
- user_impact: 1-2 sentences on what users of the site experience.
- title: "<component>: <what is wrong with the key number>", e.g. "api: 29% of requests fail with HTTP 500" or "postgres: queries 100x slower (p95 975ms)".
Answer ONLY with JSON matching the schema.""".replace("__APP__", APP_DESCRIPTION).replace("__NS__", APP_NAMESPACE).replace("__INGRESS__", INGRESS_MANIFEST)

# lean: the model writes only what needs reasoning (cause + actions); title and user impact come from
# measured data. full: the model writes everything (previous behaviour). Switch with LLM_MODE in config.env.
LLM_MODE = ENV("LLM_MODE", "lean")
LLM_SCHEMA_FULL = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "max 90 chars: '<component>: <what is wrong, with the key number>'"},
        "category": {"type": "string", "enum": CATEGORIES},
        "confidence": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
        "root_cause": {"type": "string"},
        "recommended_actions": {"type": "array", "items": {"type": "string"}},
        "user_impact": {"type": "string"},
    },
    "required": ["title", "category", "confidence", "root_cause", "recommended_actions", "user_impact"],
}
LLM_SCHEMA_LEAN = {
    "type": "object",
    "properties": {
        "category": LLM_SCHEMA_FULL["properties"]["category"],
        "confidence": LLM_SCHEMA_FULL["properties"]["confidence"],
        "root_cause": {"type": "string", "maxLength": 280},
        "recommended_actions": {"type": "array", "minItems": 1, "maxItems": 2,
                                "items": {"type": "string", "maxLength": 140}},
    },
    "required": ["category", "confidence", "root_cause", "recommended_actions"],
}
LLM_SCHEMA = LLM_SCHEMA_LEAN if LLM_MODE == "lean" else LLM_SCHEMA_FULL


def measured_impact(ev):
    """User impact from measurements (external checks + API), used in lean mode."""
    probes, api = ev.get("probes") or {}, ev.get("api") or {}
    path = lambda u: urllib.parse.urlparse(u).path or "/"
    bad = [f"{path(u)} (HTTP {p.get('status', 0):.0f})" for u, p in probes.items() if p.get("ok") == 0]
    good = [path(u) for u, p in probes.items() if p.get("ok") == 1]
    if bad:
        return f"Users cannot load {', '.join(bad)}" + (f"; {', '.join(good)} still works." if good else ".")
    parts = []
    if (api.get("error_ratio") or 0) > 0.01:
        parts.append(f"{api['error_ratio'] * 100:.0f}% of API requests fail")
    if (api.get("p95") or 0) > 1:
        parts.append(f"API responses are slow (p95 {fmt_s(api['p95'])})")
    if parts:
        return "Users are affected: " + " and ".join(parts) + "."
    return "No user-facing errors measured right now (external checks pass, API error rate normal)."


def ask_llm(evtext, hyps=None):
    out = _ask_llm_once(evtext)
    blames_deploy = re.search(r"deploy|rollout|rolled out|exporter|new version|image", out["root_cause"], re.I)
    if blames_deploy and not any(h["category"] in ("BAD DEPLOY", "IMAGE PULL FAILURE", "INGRESS / ROUTING") for h in (hyps or [])):
        log.info("LLM blamed a deploy without evidence; asking again")
        out = _ask_llm_once(evtext + "\n\nNOTE: no deployment or config change is related to these symptoms. "
                                     "Explain the root cause from the metrics and logs only.")
    acts = list(dict.fromkeys(re.sub(r"\s+", " ", a).strip(" .") for a in out.get("recommended_actions", [])
                              if isinstance(a, str) and len(a.strip()) > 12))
    # Never pass on a model-written command that changes the cluster: only read-only kubectl verbs survive.
    # Changes (rollback, restart, apply) come only from the rule engine's known-good command.
    acts = [a for a in acts if "kubectl" not in a or re.search(r"kubectl\s+(-n\s+\S+\s+)?(get|describe|logs|top|explain)\b", a)
            and not re.search(r"\b(scale|delete|patch|edit|apply|rollout|set|annotate|label|drain|cordon)\b", a)]
    # always lead with one exact, known-good command for the category if the model gave none
    match = next((h for h in (hyps or []) if h["category"] == out["category"]), {})
    known = [match["action"]] if match.get("action") else DEFAULT_ACTIONS.get(out["category"], [f"kubectl -n {APP_NAMESPACE} get pods,events"])[-1:]
    if match.get("action") or not any("kubectl" in a for a in acts):
        acts = known + [a for a in acts if a not in known]
    out["recommended_actions"] = acts[:3]
    if len(out.get("title", "")) < 12 or out["title"].strip().upper() == out["category"] or LLM_MODE == "lean":
        top = next((h for h in (hyps or []) if h["category"] == out["category"]), (hyps or [{}])[0])
        fact = (top.get("facts") or ["see evidence"])[0]
        out["title"] = f"{out['category'].title()}: " + (fact if len(fact) <= 100 else fact[:100].rsplit(" ", 1)[0] + "...")
    return out


def _ask_llm_once(evtext):
    body = {
        "model": MODEL, "stream": False, "think": LLM_THINK, "format": LLM_SCHEMA, "keep_alive": "24h",
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": evtext + "\n\nReturn the diagnosis JSON."}],
        "options": {"temperature": 0.1, "repeat_penalty": 1.2, "repeat_last_n": 128, "num_ctx": int(ENV("LLM_NUM_CTX", "8192")), "num_predict": int(ENV("LLM_MAX_TOKENS", "400"))},
    }
    t0 = time.time()
    raw = _http(f"{OLLAMA}/api/chat", data=body, timeout=LLM_TIMEOUT)
    dt = time.time() - t0
    STATS["llm_calls"] += 1
    STATS["llm_seconds_sum"] += dt
    resp = json.loads(raw)
    content = resp["message"].get("content") or ""
    LAST["llm_raw"] = {"evidence": evtext, "content": content, "tokens": resp.get("eval_count")}
    if not content.strip():
        raise ValueError(f"empty answer after {resp.get('eval_count')} tokens (token budget spent on reasoning)")
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
    m = re.search(r"\{.*\}", content, re.S)
    out = json.loads(m.group(0) if m else content)
    for k in LLM_SCHEMA["required"]:
        if k not in out:
            raise ValueError(f"LLM output missing {k}")
    if out["category"] not in CATEGORIES:
        out["category"] = "UNKNOWN"
    out["_llm_seconds"] = round(dt, 1)
    out["_tokens"] = resp.get("eval_count")
    log.info("LLM answered in %.1fs (%s tokens)", dt, resp.get("eval_count"))
    return out


DEFAULT_ACTIONS = {
    "MEMORY LEAK / OOM": ["kubectl -n __NS__ top pods -l app.kubernetes.io/name=__API__",
                          "kubectl -n __NS__ describe pod <pod> | grep -A5 'Last State'",
                          "Roll back or fix the leaking code path; meanwhile: kubectl -n __NS__ rollout restart deploy/__API__"],
    "CPU SATURATION": ["kubectl -n __NS__ top pods -l app.kubernetes.io/name=__API__",
                       "Find the hot code path; temporarily raise the CPU limit or scale: kubectl -n __NS__ scale deploy/__API__ --replicas=4"],
    "DATABASE SLOWNESS": ["kubectl -n __NS__ exec <postgres-pod> -- psql -U <user> -d <database> -c \"select pid, now()-query_start as age, query from pg_stat_activity where state='active' order by age desc\"",
                          "Check locks / slow statements before touching the API; restarting API pods will not help"],
    "DATABASE ERRORS": ["kubectl -n __NS__ logs <postgres-pod> --tail=50", "kubectl -n __NS__ logs -l app.kubernetes.io/name=__API__ --tail=50 | grep 'db query failed'"],
    "APPLICATION ERRORS": ["kubectl -n __NS__ logs -l app.kubernetes.io/name=__API__ --tail=100 | grep '\"status\": 5'",
                           "If a rollout preceded it: kubectl -n __NS__ rollout undo deploy/__API__"],
    "APPLICATION LATENCY": ["kubectl -n __NS__ logs -l app.kubernetes.io/name=__API__ --tail=100 | grep duration_ms",
                            "Compare handler time vs DB time in Grafana; check recent config/feature-flag changes"],
    "CRASHLOOP / PROBE FAILURE": ["kubectl -n __NS__ describe pod -l app.kubernetes.io/name=__API__ | grep -A3 -E 'Liveness|Last State'",
                                  "kubectl -n __NS__ logs deploy/__API__ --previous --tail=50"],
    "INGRESS / ROUTING": ["kubectl -n __NS__ get ingress -o yaml",
                          "Restore from git: kubectl -n __NS__ apply -f __INGRESS__"],
    "IMAGE PULL FAILURE": ["kubectl -n __NS__ rollout undo deploy/<deployment>",
                           "kubectl -n __NS__ describe pod <pod> | grep -A3 Failed"],
    "BAD DEPLOY": ["kubectl -n __NS__ rollout history deploy/__API__", "kubectl -n __NS__ rollout undo deploy/__API__"],
}
DEFAULT_ACTIONS = {k: [a.replace("__NS__", APP_NAMESPACE).replace("__API__", API_APP).replace("__INGRESS__", INGRESS_MANIFEST)
                       for a in v] for k, v in DEFAULT_ACTIONS.items()}


def fallback_diagnosis(hyps, reason=None):
    top = hyps[0] if hyps else {"category": "UNKNOWN", "confidence": "LOW", "facts": []}
    if top.get("action"):
        DEFAULT_ACTIONS[top["category"]] = [top["action"]] + DEFAULT_ACTIONS.get(top["category"], [])[:1]
    lead = (f"Rule-based analysis (AI unavailable: {reason})." if reason else
            "Automated evidence analysis; the AI review follows in this thread.")
    return {
        "title": f"{top['category'].title()} in {APP_NAMESPACE}",
        "category": top["category"], "confidence": top.get("confidence", "LOW"),
        "root_cause": f"{lead} Strongest signal: {top['category']}: "
                      + ("; ".join(top["facts"][:2]) if top["facts"] else "no strong signal found, see evidence."),
        "key_evidence": top["facts"][:5],
        "recommended_actions": DEFAULT_ACTIONS.get(top["category"], [f"kubectl -n {APP_NAMESPACE} get pods,events"]),
        "user_impact": "See API error rate and latency in Live metrics.",
        "_fallback": True,
    }


# ---------------------------------------------------------------- email

SEV_COLOR = {"critical": "#d93025", "warning": "#f29900", "info": "#1a73e8", "resolved": "#188038"}
CAT_ICON = {"MEMORY LEAK / OOM": "&#128165;", "CPU SATURATION": "&#128293;", "DATABASE SLOWNESS": "&#128034;",
            "DATABASE ERRORS": "&#128452;", "APPLICATION ERRORS": "&#10060;", "APPLICATION LATENCY": "&#9203;",
            "CRASHLOOP / PROBE FAILURE": "&#128260;", "IMAGE PULL FAILURE": "&#128683;", "INGRESS / ROUTING": "&#128678;", "BAD DEPLOY": "&#128230;", "CERTIFICATE": "&#128274;",
            "NODE PRESSURE": "&#128421;", "TELEMETRY GAP": "&#128225;", "UNKNOWN": "&#10067;"}


def evidence_block(ev, diag, hyps):
    """Machine-generated evidence lines (never from the model) for the chosen category."""
    lines = []
    match = next((h for h in hyps if h["category"] == diag["category"]), None)
    if match:
        lines += match["facts"][:6]
    for h in hyps:
        if h is not match and h["score"] >= 0.55 and len(lines) < 8:
            lines += h["facts"][:1]
    pl = (ev.get("pods") or {}).get("previous_logs") or []
    if pl and diag["category"] in ("CRASHLOOP / PROBE FAILURE", "MEMORY LEAK / OOM", "APPLICATION ERRORS"):
        lines.append(f"previous logs {pl[0]['pod']}: " + (pl[0]["lines"][-1][:160] if pl[0]["lines"] else "(empty)"))
    if not lines:
        lines = [f"{a['labels'].get('alertname')}: {a['annotations'].get('summary', '')}" for a in diag.get("_alerts", [])]
    return lines[:9]


def render_email(diag, ev, hyps, alerts, severity, update=False):
    color = SEV_COLOR.get(severity, "#f29900")
    e = html.escape
    api, node, db = ev.get("api") or {}, ev.get("node") or {}, ev.get("db") or {}
    chaos = ev.get("chaos") or {}
    expected = [GROUND_TRUTH_MAP.get(m) for m in chaos]
    if not GROUND_TRUTH_METRIC:
        truth = ""
    elif chaos:
        ok = diag["category"] in expected
        truth = (f"{GROUND_TRUTH_METRIC}: {', '.join(f'{k}={v:g}' for k, v in chaos.items())} &rarr; expected "
                 f"<b>{e(', '.join(x for x in expected if x))}</b> &mdash; diagnosis "
                 + ("<b style='color:#188038'>MATCHES &#10004;</b>" if ok else "<b style='color:#d93025'>DIFFERS &#10008;</b>"))
    else:
        truth = f"{GROUND_TRUTH_METRIC}: none (no fault injected) &mdash; this is a real issue"
    ev_lines = evidence_block(ev, diag, hyps)
    changes = [c for c in (ev.get("changes") or []) if c.get("rolled_out")
               and (datetime.now(timezone.utc) - parse_ts(c["rolled_out"])).total_seconds() < 7200]
    for i in (ev.get("ingress") or {}).get("ingresses", []):
        ct = parse_ts(i.get("changed"))
        if ct and (datetime.now(timezone.utc) - ct).total_seconds() < 7200:
            changes.append({"workload": f"ingress/{i['name']}", "revision": "-", "rolled_out": i["changed"],
                            "changes": [f"modified by {i.get('changed_by')}"]})
    changes.sort(key=lambda c: c["rolled_out"], reverse=True)
    change_txt = "; ".join(f"{c['workload']} rev {c['revision']} {ago(parse_ts(c['rolled_out']))}"
                           f" ({', '.join(c['changes'][:2]) or 'no change'})" for c in changes[:2])
    alert_names = sorted({a["labels"].get("alertname") for a in alerts})
    first = min((parse_ts(a.get("startsAt")) for a in alerts if a.get("startsAt")), default=None)
    title = diag["title"]
    sev_label = severity.upper()
    ai_line = ("rule engine (AI review follows)" if diag.get("_quick") else "rule-based fallback" if diag.get("_fallback") else
               f"{MODEL} &middot; {diag.get('_llm_seconds', '?')}s")
    other = [h for h in hyps if h["category"] != diag["category"]][:2]
    pill = "display:inline-block;background:#f1f3f4;border:1px solid #dadce0;border-radius:4px;padding:1px 6px;font-family:monospace;font-size:12px"
    h2 = "font-size:13px;font-weight:700;letter-spacing:.3px;margin:16px 0 6px;color:#202124"
    grafana_logs = GRAFANA + "/explore?schemaVersion=1&panes=" + urllib.parse.quote(json.dumps(
        {"a": {"datasource": "loki", "queries": [{"refId": "A", "expr": f'{{cluster="{CLUSTER}",namespace="{ev["namespace"]}",level=~"error|warning"}}'}],
               "range": {"from": "now-30m", "to": "now"}}}))
    body = f"""<!doctype html><html><body style="margin:0;padding:16px;background:#f6f8fa;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#202124">
<div style="max-width:680px;margin:auto;background:#fff;border:1px solid #e0e0e0;border-left:6px solid {color};border-radius:8px;padding:18px 22px">
<div style="font-size:12px;color:#5f6368">{'UPDATE &middot; ' if update else ''}{e(PROJECT_NAME)} &middot; {e(CLUSTER)} / {e(ev['namespace'])}</div>
<div style="font-size:18px;font-weight:700;margin:4px 0 10px"><span style="color:{color}">&#9679;</span> [{sev_label}] {e(title)}</div>
<div style="{h2}">ROOT CAUSE</div>
<div style="font-size:14px;line-height:1.5">{e(diag['root_cause'])}</div>
<div style="margin-top:10px"><span style="{pill}">{CAT_ICON.get(diag['category'], '')} {e(diag['category'])}</span>
<span style="{pill}">confidence: {e(diag['confidence'])}</span></div>
<div style="{h2}">EVIDENCE</div>
<pre style="background:#f8f9fa;border:1px solid #e8eaed;border-radius:6px;padding:10px 12px;font-size:12px;line-height:1.45;white-space:pre-wrap;margin:0">{e(chr(10).join(ev_lines))}</pre>
{"<ul style='font-size:13px;margin:8px 0 0;padding-left:20px'>" + "".join(f"<li>{e(x)}</li>" for x in diag.get('key_evidence', [])[:5]) + "</ul>" if diag.get('key_evidence') and not diag.get('_fallback') else ""}
<div style="{h2}">RECOMMENDED ACTION</div>
<ol style="font-size:13px;margin:0;padding-left:20px;line-height:1.55">{"".join(f"<li><code style='font-size:12px'>{e(x)}</code></li>" if x.strip().startswith('kubectl') else f"<li>{e(x)}</li>" for x in diag['recommended_actions'][:4])}</ol>
<div style="{h2}">&#128202; LIVE METRICS</div>
<div style="font-size:13px;line-height:1.7">
API <span style="{pill}">{(api.get('rps') or 0):.1f} req/s</span> &middot; 5xx <span style="{pill}">{fmt_pct(api.get('error_ratio'))}</span> &middot; p95 <span style="{pill}">{fmt_s(api.get('p95'))}</span> &middot; p99 <span style="{pill}">{fmt_s(api.get('p99'))}</span><br>
DB pool <span style="{pill}">size {db.get('pool', {}).get('size', 0):.0f} / avail {db.get('pool', {}).get('available', 0):.0f} / waiting {db.get('pool', {}).get('waiting', 0):.0f}</span> &middot; slowest query p95 <span style="{pill}">{fmt_s(max((v['now'] for v in (db.get('query_p95') or {}).values()), default=None))}</span><br>
Node CPU <span style="{pill}">{fmt_pct(node.get('cpu'))}</span> &middot; Mem <span style="{pill}">{fmt_pct(node.get('mem'))}</span> &middot; Disk <span style="{pill}">{fmt_pct(node.get('disk'))}</span> &middot; IOWait <span style="{pill}">{fmt_pct(node.get('iowait'))}</span><br>
Disk IOPS R:{(node.get('disk_read_iops') or 0):.1f} W:{(node.get('disk_write_iops') or 0):.1f}<br>
External checks: {' &middot; '.join(f"<span style='{pill}'>{e(urllib.parse.urlparse(u).path or '/')} {'OK' if p.get('ok') == 1 else 'FAIL'} {p.get('status', 0):.0f}</span>" for u, p in (ev.get('probes') or {}).items()) or 'n/a'}
</div>
<div style="{h2}">&#128230; RECENT CHANGES</div>
<div style="font-size:13px">{e(change_txt or 'none in the last 2h')}</div>
<div style="{h2}">&#9889; USER IMPACT</div>
<div style="font-size:13px">{e(diag['user_impact'])}</div>
{f'<div style="{h2}">&#127919; GROUND TRUTH CHECK</div><div style="font-size:13px">{truth}</div>' if truth else ""}
{("<div style='font-size:12px;color:#5f6368;margin-top:6px'>Rule engine: " + e(hyps[0]['category']) + (" &mdash; AI agrees" if hyps[0]['category'] == diag['category'] else " &mdash; AI disagrees, see root cause") + "</div>") if hyps and not diag.get('_fallback') else ""}
{"<div style='font-size:12px;color:#5f6368;margin-top:6px'>Also considered: " + e(', '.join(f"{h['category']} ({h['score']})" for h in other)) + "</div>" if other else ""}
<div style="margin-top:16px;font-size:13px"><a href="{GRAFANA}/d/{DASHBOARD_UID}" style="color:#1a73e8">Dashboard</a> &middot; <a href="{e(grafana_logs)}" style="color:#1a73e8">Error logs</a> &middot; <a href="{GRAFANA}/alerting/list" style="color:#1a73e8">Alerts</a></div>
<div style="border-top:1px solid #eee;margin-top:14px;padding-top:8px;font-size:11px;color:#80868b">
alertmanager &middot; {e(CLUSTER)}{(' &middot; ' + e(REGION)) if REGION else ''} &middot; first fired {first.strftime('%Y-%m-%d %H:%M:%S UTC') if first else '?'} &middot; alerts: {e(', '.join(alert_names))} &middot; {ai_line}
</div></div></body></html>"""
    text = "\n".join([
        f"[{sev_label}] {title}", "", "ROOT CAUSE", diag["root_cause"],
        f"{diag['category']} | confidence: {diag['confidence']}", "", "EVIDENCE", *ev_lines, "",
        "RECOMMENDED ACTION", *[f"{i + 1}. {x}" for i, x in enumerate(diag["recommended_actions"][:4])], "",
        f"LIVE: API {(api.get('rps') or 0):.1f} req/s, 5xx {fmt_pct(api.get('error_ratio'))}, p95 {fmt_s(api.get('p95'))}; "
        f"node CPU {fmt_pct(node.get('cpu'))} mem {fmt_pct(node.get('mem'))} disk {fmt_pct(node.get('disk'))}",
        f"RECENT CHANGES: {change_txt or 'none'}", f"USER IMPACT: {diag['user_impact']}",
        *(["GROUND TRUTH: " + re.sub(r"<[^>]+>", "", html.unescape(truth))] if truth else []), "",
        f"alerts: {', '.join(alert_names)}"])
    subject = f"[{sev_label}]{' UPDATE' if update else ''} {title} ({diag['confidence']})"
    return subject, body, text


def send_email(subject, html_body, text_body, in_reply_to=None):
    """Returns the Message-ID on success (used to thread the AI follow-up), else None."""
    LAST["html"] = html_body
    msgid = make_msgid(domain=urllib.parse.urlparse(GRAFANA).hostname or "localhost")
    if not (SMTP_USER and MAIL_TO and os.path.exists(SMTP_PASSWORD_FILE)):
        log.warning("SMTP not configured; email not sent: %s", subject)
        return None
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, MAIL_FROM, ", ".join(MAIL_TO)
    msg["Date"], msg["Message-ID"] = formatdate(localtime=False), msgid
    if in_reply_to:
        msg["In-Reply-To"] = msg["References"] = in_reply_to
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(SMTP_USER, open(SMTP_PASSWORD_FILE).read().strip().replace(" ", ""))
            s.send_message(msg)
        STATS["emails_sent"] += 1
        log.info("email sent: %s", subject)
        return msgid
    except Exception as e:
        STATS["emails_failed"] += 1
        log.error("email failed: %s", e)
        return None


def push_to_loki(ns, diag, hyps, ev, subject):
    rec = {"subject": subject, "category": diag["category"], "confidence": diag["confidence"],
           "root_cause": diag["root_cause"], "evidence": evidence_block(ev, diag, hyps),
           "actions": diag["recommended_actions"], "ground_truth": ev.get("chaos"),
           "fallback": bool(diag.get("_fallback")), "llm_seconds": diag.get("_llm_seconds")}
    body = {"streams": [{"stream": {"source": "ai-diagnoser", "cluster": CLUSTER, "namespace": ns,
                                    "category": diag["category"]},
                         "values": [[str(time.time_ns()), json.dumps(rec)]]}]}
    try:
        _http(f"{LOKI}/loki/api/v1/push", data=body)
    except Exception as e:
        log.warning("loki push failed: %s", e)


# ---------------------------------------------------------------- pipeline

def handle(payload, dry_run=False):
    ns = (payload.get("commonLabels") or {}).get("namespace") or (payload.get("groupLabels") or {}).get("namespace") or APP_NAMESPACE
    alerts = payload.get("alerts", [])
    firing = [a for a in alerts if a.get("status") == "firing"]
    gk = payload.get("groupKey", ns)
    if not firing:
        prev = SEEN.pop(gk, None)
        api = ev_api(ns) if ns == APP_NAMESPACE else {}
        names = ", ".join(sorted({a["labels"].get("alertname") for a in alerts}))
        title = prev[2] if prev else names
        started = min((parse_ts(a.get("startsAt")) for a in alerts if a.get("startsAt")), default=None)
        dur = ago(started).replace(" ago", "") if started else "?"
        subj = f"[RESOLVED] {title}"
        body = f"""<div style="font-family:Arial,sans-serif;max-width:680px;border-left:6px solid {SEV_COLOR['resolved']};padding:14px 18px;background:#fff;border:1px solid #e0e0e0;border-radius:8px">
<div style="font-size:18px;font-weight:700"><span style="color:{SEV_COLOR['resolved']}">&#10004;</span> [RESOLVED] {html.escape(title)}</div>
<p style="font-size:14px">All alerts cleared after ~{dur}: {html.escape(names)}.</p>
<p style="font-size:13px">Now: API {(api.get('rps') or 0):.1f} req/s &middot; 5xx {fmt_pct(api.get('error_ratio'))} &middot; p95 {fmt_s(api.get('p95'))}</p></div>"""
        if not dry_run:
            send_email(subj, body, f"{subj}\nAll alerts cleared after ~{dur}: {names}")
        return {"resolved": True, "subject": subj}

    fps = frozenset(a["labels"].get("alertname") for a in firing)   # one email per kind of problem, not per pod
    prev = SEEN.get(gk)
    if prev and fps <= prev[0] and time.time() - prev[1] < 3600 and not dry_run:
        STATS["skipped_duplicates"] += 1
        log.info("same alert set already diagnosed for %s; skipping", gk)
        return {"skipped": True}
    update = bool(prev)

    t0 = time.time()
    ev = collect(ns)
    hyps = analyze(ev, firing)
    log.info("evidence collected in %.1fs; top hypotheses: %s", time.time() - t0,
             [(h["category"], h["score"]) for h in hyps[:3]])
    only_manual = all(a["labels"].get("alertname") == "ManualHealthCheck" for a in firing)
    severity = "critical" if any(a["labels"].get("severity") == "critical" for a in firing) else "warning"
    thread_id, quick_subject = None, None
    if QUICK_EMAIL and hyps and not dry_run:
        quick = fallback_diagnosis(hyps)
        quick.update(_quick=True, _alerts=firing)
        quick_subject, qbody, qtext = render_email(quick, ev, hyps, firing, severity, update=update)
        thread_id = send_email(quick_subject, qbody, qtext)
        push_to_loki(ns, quick, hyps, ev, quick_subject)
        log.info("quick email after %.1fs", time.time() - t0)
    try:
        if not hyps and not only_manual:
            diag = {"title": "No clear root cause found: " + ", ".join(sorted({a["labels"].get("alertname") for a in firing})),
                    "category": "UNKNOWN", "confidence": "LOW",
                    "root_cause": "The alert fired, but no metric, log, event or recent change crossed a threshold that explains it. "
                                  "It may have been transient; check the evidence below.",
                    "recommended_actions": [f"kubectl -n {APP_NAMESPACE} get pods,events --sort-by=.lastTimestamp"],
                    "user_impact": "", "_llm_seconds": 0}
        elif only_manual and not hyps:
            diag = {"title": f"{ns} looks healthy", "category": "UNKNOWN", "confidence": "HIGH",
                    "root_cause": "No alert is firing and no signal crossed a threshold.",
                    "recommended_actions": ["Nothing to do"], "user_impact": "None.", "_llm_seconds": 0}
        else:
            diag = ask_llm(evidence_text(ev, firing, hyps), hyps)
        STATS["diagnoses_ok"] += 1
    except Exception as e:
        log.error("LLM failed: %s", e)
        diag = fallback_diagnosis(hyps, type(e).__name__)
        STATS["diagnoses_fallback"] += 1
    if LLM_MODE == "lean" or diag.get("_fallback"):
        diag["user_impact"] = measured_impact(ev)
    diag["_alerts"] = firing
    if thread_id and diag.get("_fallback"):
        # the quick email already carried the rule-based diagnosis; nothing new to say
        SEEN[gk] = (fps, time.time(), quick["title"])
        log.info("AI unavailable; quick email stands for %s", gk)
        return LAST["result"]
    if QUICK_EMAIL and hyps and not dry_run:
        ev = collect(ns)   # refresh live numbers: the AI took minutes
    subject, body, text = render_email(diag, ev, hyps, firing, severity, update=update)
    if quick_subject:
        subject = "Re: " + quick_subject + f" | AI: {diag['category']} ({diag['confidence']})"
    LAST["result"] = {"subject": subject, "diagnosis": {k: v for k, v in diag.items() if k != "_alerts"},
                      "hypotheses": hyps, "chaos": ev.get("chaos"), "errors": ev.get("collection_errors")}
    if not dry_run:
        send_email(subject, body, text, in_reply_to=thread_id)
        push_to_loki(ns, diag, hyps, ev, subject)
        SEEN[gk] = (fps, time.time(), diag["title"])
    else:
        LAST["html"] = body
    log.info("done in %.1fs: %s", time.time() - t0, subject)
    return LAST["result"]


def worker():
    while True:
        payload, dry = JOBS.get()
        try:
            handle(payload, dry_run=dry)
        except Exception:
            log.error("job failed:\n%s", traceback.format_exc())
        finally:
            JOBS.task_done()


def current_alerts_payload(ns):
    """Build an Alertmanager-like payload from what Prometheus is firing right now (for /diagnose)."""
    alerts = []
    for m, _ in prom(f'ALERTS{{alertstate="firing",namespace="{ns}"}}'):
        labels = {k: v for k, v in m.items() if k not in ("__name__", "alertstate")}
        alerts.append({"status": "firing", "labels": labels, "annotations": {"summary": labels.get("alertname")},
                       "startsAt": datetime.now(timezone.utc).isoformat(), "fingerprint": json.dumps(labels, sort_keys=True)})
    if not alerts:
        alerts = [{"status": "firing", "labels": {"alertname": "ManualHealthCheck", "severity": "info", "namespace": ns},
                   "annotations": {"summary": "Manual diagnosis requested; nothing is firing"},
                   "startsAt": datetime.now(timezone.utc).isoformat(), "fingerprint": "manual"}]
    return {"groupKey": f"manual/{ns}/{time.time()}", "commonLabels": {"namespace": ns}, "alerts": alerts}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        if "/healthz" not in self.path and "/metrics" not in self.path:
            log.info("%s %s", self.command, self.path)

    def do_GET(self):
        if self.path.startswith("/healthz"):
            return self._send(200, "ok", "text/plain")
        if self.path.startswith("/metrics"):
            lines = [f"diagnoser_{k} {v}" for k, v in STATS.items()] + [f"diagnoser_queue_length {JOBS.qsize()}"]
            return self._send(200, "\n".join(lines) + "\n", "text/plain; version=0.0.4")
        if self.path.startswith("/last-llm.json"):
            return self._send(200, json.dumps(LAST.get("llm_raw"), indent=2))
        if self.path.startswith("/last.json"):
            return self._send(200, json.dumps(LAST["result"], indent=2, default=str))
        if self.path.startswith("/last"):
            return self._send(200, LAST["html"], "text/html; charset=utf-8")
        self._send(404, "not found", "text/plain")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        url = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(url.query)
        if url.path == "/alertmanager":
            STATS["webhooks"] += 1
            JOBS.put((json.loads(raw), False))
            return self._send(202, '{"queued": true}')
        if url.path == "/diagnose":
            ns = qs.get("namespace", [APP_NAMESPACE])[0]
            dry = qs.get("dry_run", ["0"])[0] == "1"
            JOBS.put((current_alerts_payload(ns), dry))
            return self._send(202, json.dumps({"queued": True, "dry_run": dry}))
        self._send(404, "not found", "text/plain")


def warm_model():
    """Load the model and cache the (fixed) system prompt prefix: later requests then only process the evidence."""
    try:
        body = {"model": MODEL, "stream": False, "keep_alive": "24h", "format": LLM_SCHEMA,
                "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": "warmup"}],
                "options": {"num_predict": 1, "num_ctx": int(ENV("LLM_NUM_CTX", "8192"))}}
        _http(f"{OLLAMA}/api/chat", data=body, timeout=600)
        log.info("model warm, system prompt cached")
    except Exception as e:
        log.warning("warmup failed: %s", e)


if __name__ == "__main__":
    threading.Thread(target=warm_model, daemon=True).start()
    for _ in range(1):
        threading.Thread(target=worker, daemon=True).start()
    log.info("diagnoser listening on :8080 (model=%s think=%s, mail_to=%s)", MODEL, LLM_THINK, MAIL_TO)
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
