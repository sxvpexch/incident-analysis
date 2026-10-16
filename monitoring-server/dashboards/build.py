#!/usr/bin/env python3
"""Generates the "service health" Grafana dashboard as a ConfigMap (picked up by the Grafana sidecar).
Reads the same variables as config.env: CLUSTER_NAME, APP_NAMESPACE, EXCLUDE_ROUTES, APP_INFO_METRIC, GROUND_TRUTH_METRIC."""
import json
import os

E = os.environ.get
CLUSTER, NS = E("CLUSTER_NAME", "my-cluster"), E("APP_NAMESPACE", "default")
EXCL, APP_INFO, TRUTH = E("EXCLUDE_ROUTES", "unmatched"), E("APP_INFO_METRIC", "app_info"), E("GROUND_TRUTH_METRIC", "")
P = {"type": "prometheus", "uid": "prometheus"}
L = {"type": "loki", "uid": "loki"}
S = f'cluster="{CLUSTER}",namespace="{NS}"'
C = f'cluster="{CLUSTER}"'
panels, y = [], 0
pid = iter(range(1, 200))

def row(title):
    global y
    panels.append({"type": "row", "title": title, "id": next(pid), "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "collapsed": False})
    y += 1

def ts(title, targets, x, w, h=8, unit="short", desc="", stack=False, legend=("mean", "max")):
    panels.append({
        "type": "timeseries", "title": title, "id": next(pid), "description": desc, "datasource": P,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [{"refId": chr(65 + i), "expr": e, "legendFormat": lf, "datasource": P} for i, (e, lf) in enumerate(targets)],
        "fieldConfig": {"defaults": {"unit": unit, "custom": {"lineWidth": 2, "fillOpacity": 8 if not stack else 60, "showPoints": "never",
                                                             "stacking": {"mode": "normal" if stack else "none"}}}, "overrides": []},
        "options": {"legend": {"displayMode": "table", "placement": "bottom", "calcs": list(legend)}, "tooltip": {"mode": "multi", "sort": "desc"}}})

def stat(title, expr, x, w, unit="short", thresholds=None, desc="", h=4, text_mode="value", legend="", decimals=None):
    steps = [{"color": "green", "value": None}] + [{"color": c, "value": v} for v, c in (thresholds or [])]
    d = {"unit": unit, "thresholds": {"mode": "absolute", "steps": steps}, "color": {"mode": "thresholds"}}
    if decimals is not None:
        d["decimals"] = decimals
    panels.append({"type": "stat", "title": title, "id": next(pid), "description": desc, "datasource": P,
                   "gridPos": {"x": x, "y": y, "w": w, "h": h},
                   "targets": [{"refId": "A", "expr": expr, "legendFormat": legend, "instant": True, "datasource": P}],
                   "fieldConfig": {"defaults": d, "overrides": []},
                   "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "background", "graphMode": "none", "textMode": text_mode, "justifyMode": "center"}})

def logs(title, expr, x, w, h=10):
    panels.append({"type": "logs", "title": title, "id": next(pid), "datasource": L, "gridPos": {"x": x, "y": y, "w": w, "h": h},
                   "targets": [{"refId": "A", "expr": expr, "datasource": L}],
                   "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending", "enableLogDetails": True, "prettifyLogMessage": False}})

# Headline tiles
row("Health at a glance")
stat("Requests / s", f'sum(rate(http_requests_total{{{S}}}[2m]))', 0, 4, "reqps", decimals=1)
stat("API 5xx ratio", f'sum(rate(http_requests_total{{{S},status=~"5.."}}[2m])) / sum(rate(http_requests_total{{{S}}}[2m])) or vector(0)', 4, 4, "percentunit", [(0.01, "orange"), (0.05, "red")])
stat("API p95 latency", f'histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{{{S},route!~"{EXCL}"}}[2m])))', 8, 4, "s", [(0.5, "orange"), (1, "red")])
stat("Slowest DB query p95", f'max(histogram_quantile(0.95, sum by (query, le) (rate(db_query_duration_seconds_bucket{{{S}}}[2m]))))', 12, 4, "s", [(0.1, "orange"), (0.25, "red")])
stat("Pods not ready", f'sum(kube_pod_status_ready{{{S},condition="false"}}) or vector(0)', 16, 4, "short", [(1, "red")])
stat("Restarts (30m)", f'sum(increase(kube_pod_container_status_restarts_total{{{S}}}[30m])) or vector(0)', 20, 4, "short", [(1, "orange"), (3, "red")], decimals=0)
y += 4
stat("Firing alerts", f'count(ALERTS{{alertstate="firing",namespace="{NS}"}}) or vector(0)', 0, 4, "short", [(1, "red")], h=3)
if TRUTH:
    stat("Injected fault (ground truth)", f'max by (mode) ({TRUTH}{{{S}}}) > 0', 4, 12, "short", [(0.0001, "purple")],
         desc=f"{TRUTH}: which fault is injected right now. Empty = none.", h=3, text_mode="value_and_name", legend="{{mode}}")
stat("App version", f'count by (version) ({APP_INFO}{{{S}}})', 16, 4, "short", h=3, text_mode="name", legend="{{version}}")
stat("TLS cert days left", f'min((certmanager_certificate_expiration_timestamp_seconds{{{C}}} - time()) / 86400)', 20, 4, "d", [(-1e9, "red"), (7, "orange"), (14, "green")], h=3, decimals=0)
panels[-1]["fieldConfig"]["defaults"]["thresholds"]["steps"] = [{"color": "red", "value": None}, {"color": "orange", "value": 7}, {"color": "green", "value": 14}]
y += 3

row("API: rate, errors, duration")
ts("Requests by status", [(f'sum by (status) (rate(http_requests_total{{{S}}}[2m]))', "{{status}}")], 0, 8, unit="reqps")
ts("5xx by route", [(f'sum by (route) (rate(http_requests_total{{{S},status=~"5.."}}[2m]))', "{{route}}")], 8, 8, unit="reqps")
ts("p95 latency by route", [(f'histogram_quantile(0.95, sum by (route, le) (rate(http_request_duration_seconds_bucket{{{S},route!~"{EXCL}"}}[2m])))', "{{route}}")], 16, 8, unit="s")
y += 8

row("Database")
ts("Query p95 by query", [(f'histogram_quantile(0.95, sum by (query, le) (rate(db_query_duration_seconds_bucket{{{S}}}[2m])))', "{{query}}")], 0, 8, unit="s")
ts("Connection pool", [(f'sum by (state) (db_pool_connections{{{S}}})', "{{state}}")], 8, 8, desc="waiting > 0 means requests queue for a DB connection")
ts("Postgres: longest active transaction", [(f'max(pg_stat_activity_max_tx_duration{{{S},state="active"}})', "longest active tx")], 16, 8, unit="s",
   desc="From pg_stat_activity via postgres-exporter: proves slowness is inside the database")
y += 8

row("Pods & resources")
ts("Memory working set vs limit", [(f'max by (pod) (container_memory_working_set_bytes{{{S},container!="",container!="POD",container!~"nginx-exporter"}}) / max by (pod) (kube_pod_container_resource_limits{{{S},resource="memory",container!~"nginx-exporter"}})', "{{pod}}")], 0, 8, unit="percentunit",
   desc="100% = OOMKilled")
ts("CPU usage vs limit", [(f'sum by (pod) (rate(container_cpu_usage_seconds_total{{{S},container!="",container!="POD",container!~"nginx-exporter"}}[2m])) / sum by (pod) (kube_pod_container_resource_limits{{{S},resource="cpu",container!~"nginx-exporter"}})', "{{pod}}")], 8, 8, unit="percentunit")
ts("CPU throttled periods", [(f'sum by (pod) (rate(container_cpu_cfs_throttled_periods_total{{{S},container!=""}}[2m])) / sum by (pod) (rate(container_cpu_cfs_periods_total{{{S},container!=""}}[2m]))', "{{pod}}")], 16, 8, unit="percentunit")
y += 8
ts("Container restarts", [(f'sum by (pod, container) (increase(kube_pod_container_status_restarts_total{{{S}}}[5m]))', "{{pod}}/{{container}}")], 0, 8, unit="short")
ts("Traefik: responses by code", [(f'sum by (code) (rate(traefik_service_requests_total{{{C},service=~"{NS}-.*"}}[2m]))', "{{code}}")], 8, 8, unit="reqps")
ts("App node: CPU / memory / disk used", [(f'1 - avg(rate(node_cpu_seconds_total{{{C},mode="idle"}}[2m]))', "CPU"),
                                         (f'1 - sum(node_memory_MemAvailable_bytes{{{C}}}) / sum(node_memory_MemTotal_bytes{{{C}}})', "memory"),
                                         (f'1 - min(node_filesystem_avail_bytes{{{C},mountpoint="/"}} / node_filesystem_size_bytes{{{C},mountpoint="/"}})', "disk /")], 16, 8, unit="percentunit")
y += 8

row("Logs, events & AI diagnoses")
logs("AI diagnoses", '{source="ai-diagnoser"} | json | line_format "{{.subject}}  |  {{.root_cause}}"', 0, 24, h=7)
y += 7
logs("Errors & warnings (app logs)", f'{{{S},source="container",level=~"error|warning|critical"}}', 0, 12)
logs("Kubernetes warning events", f'{{{S},source="k8s-event",level="warning"}} | json | line_format "{{{{.reason}}}} {{{{.involvedObject_kind}}}}/{{{{.involvedObject_name}}}}: {{{{.message}}}}"', 12, 12)
y += 10

dash = {"uid": E("DASHBOARD_UID", "service-health"), "title": f"{NS}: service health", "tags": ["incident-analysis"], "timezone": "utc",
        "refresh": "30s", "time": {"from": "now-1h", "to": "now"}, "schemaVersion": 39, "panels": panels,
        "links": [{"title": "Explore logs", "type": "link", "url": "/explore"}]}
cm = {"apiVersion": "v1", "kind": "ConfigMap",
      "metadata": {"name": "dashboard-service-health", "namespace": "monitoring", "labels": {"grafana_dashboard": "1"}},
      "data": {"service-health.json": json.dumps(dash, indent=1)}}
print(json.dumps(cm))
