# API contract (all under `/api`, same auth as the rest)

## Feedback
`GET /runs/{run}/feedback` → `{run, entries: [Feedback], summary: Summary}`

`POST /runs/{run}/feedback` `{rating: "up"|"down"|"none", comment?: str, node?: str, iteration?: int}`
→ `{entry: Feedback, summary: Summary}`; 404 no run, 400 bad rating / unknown node / comment > 2000.

```text
Feedback = {id, at, run, node|null, iteration, rating: "up"|"down"|null, comment, by}
Summary  = {up, down, comments, run: {up, down}, nodes: {<node>: {up, down}},
            current: [Feedback]}   # newest per (by, node, iteration); rating null = withdrawn
```

## Metrics
`GET /runs/{run}/metrics` → `RunMetrics`

```text
RunMetrics = {run, flow, status, finished: bool, created_at, ended_at|null,
  wall_s, cost_usd (run total, incl. review/triage), node_cost_usd,
  totals: NodeMetrics-without-node, nodes: [NodeMetrics], feedback: {up, down}}
NodeMetrics = {node, kind, executions, iterations, attempts, retries, statuses: {status: n},
  work_s, queued_s, waiting_s, cost_usd,
  tokens: {input, output, cache_read, cache_write} | null, turns|null, models: [str],
  feedback: {up, down}}
```

## Report
`GET /runs/{run}/report?format=md|json` → `text/markdown` or JSON, `Content-Disposition:
attachment; filename=run-<id>.<ext>`. Redacted.

## Insights
`GET /insights?days=30&pipeline=` → `{days, since, pipelines: [PipelineTrend], nodes: [NodeTrend]
(only with pipeline), by_day: [{day, runs, done, failed, cost_usd, median_s, down, up}],
adoptions: [{at, node, verdict}] (only with pipeline)}`

```text
PipelineTrend = {pipeline, runs, done, dead, needs_review, success_rate, median_s, p90_s,
  cost_usd, cost_per_run, tokens, retries, loops, rejections, up, down}
NodeTrend = {node, executions, failures, failure_rate, median_s, cost_usd, retries, up, down}
```

## Telemetry
`GET /telemetry` → `{enabled, endpoint, service_name, headers_secret, headers_set: bool,
 killed: bool (AGENTLINE_TELEMETRY=off), last: {at, ok, error, sent}|null, queued}`

`PUT /telemetry` `{enabled?, endpoint?, service_name?, headers_secret?}` → same as GET; 400 when
the endpoint isn't http(s) or the secret name isn't `[A-Z_][A-Z0-9_]*`.

`POST /telemetry/test` → `{ok, error?, status?}` (sends one span named `agentline.test`).

## Chat actions
- `{"type": "feedback", "run": "<id>", "rating": "up"|"down"|"none", "comment"?: "...", "node"?: "..."}`
- `{"type": "run_report", "run": "<id>", "format"?: "md"|"json"}` (opens the Report dialog)
- `{"type": "telemetry", "enabled"?: bool, "endpoint"?: "...", "headers_secret"?: "NAME",
   "service_name"?: "...", "op"?: "test"}`

## Chat links
- `/rate <run> up|down [comment]`; run notices (done / dead / needs-review) carry 👍 / 👎 buttons
  (`fu:<short>`, `fd:<short>`), recorded `by = <platform>:@<user>`.

## Config
```toml
[review]
on_feedback = "on"   # "off": feedback never triggers a review by itself

[telemetry]
enabled = false
endpoint = "http://localhost:4318"
service_name = "agentline"
headers_secret = "OTEL_HEADERS"   # optional; its value "k=v,k2=v2" comes from the secrets file / env
```
