// People's feedback, run metrics, reports, insights and telemetry (phase 58;
// api/feedback.py). Client calls plus the pure helpers the components and tests use.
import { apiCall, apiRaw } from "../api";

export type Rating = "up" | "down" | "none";

export interface FeedbackEntry {
  id: number;
  at: number;
  run: string;
  node: string | null;
  iteration: number;
  rating: "up" | "down" | null;
  comment: string;
  by: string;
}

export interface FeedbackSummary {
  up: number;
  down: number;
  comments: number;
  run: { up: number; down: number };
  nodes: Record<string, { up: number; down: number }>;
  current: FeedbackEntry[];
}

export interface Tokens {
  input: number;
  output: number;
  cache_read: number;
  cache_write: number;
}

export interface NodeMetrics {
  node: string;
  kind: string;
  executions: number;
  iterations: number;
  attempts: number;
  retries: number;
  statuses: Record<string, number>;
  work_s: number;
  queued_s: number;
  waiting_s: number;
  cost_usd: number;
  tokens: Tokens | null;
  turns: number | null;
  models: string[];
  feedback: { up: number; down: number };
}

export interface RunMetrics {
  run: string;
  flow: string;
  status: string;
  finished: boolean;
  created_at: number;
  ended_at: number | null;
  wall_s: number;
  cost_usd: number;
  node_cost_usd: number;
  totals: Omit<NodeMetrics, "node" | "kind">;
  nodes: NodeMetrics[];
  feedback: { up: number; down: number; comments: number };
}

export interface PipelineTrend {
  pipeline: string;
  runs: number;
  done: number;
  dead: number;
  needs_review: number;
  success_rate: number;
  median_s: number;
  p90_s: number;
  cost_usd: number;
  cost_per_run: number;
  tokens: number;
  retries: number;
  loops: number;
  rejections: number;
  up: number;
  down: number;
}

export interface NodeTrend {
  node: string;
  kind: string;
  executions: number;
  failures: number;
  failure_rate: number;
  median_s: number;
  cost_usd: number;
  retries: number;
  up: number;
  down: number;
}

export interface DayTrend {
  day: string;
  runs: number;
  done: number;
  failed: number;
  cost_usd: number;
  median_s: number;
  up: number;
  down: number;
}

export interface Insights {
  days: number;
  since: number;
  pipeline: string | null;
  pipelines: PipelineTrend[];
  by_day: DayTrend[];
  nodes: NodeTrend[];
  adoptions: { at: number; node: string | null; verdict: string | null; summary?: string | null }[];
}

export interface TelemetryStatus {
  enabled: boolean;
  endpoint: string;
  service_name: string;
  headers_secret: string;
  headers_set: boolean;
  killed: boolean;
  last: { at: number; ok: boolean; error: string | null; sent: number } | null;
}

export type ReportFormat = "md" | "json";

const run = (id: string) => `/runs/${encodeURIComponent(id)}`;

export const feedbackApi = {
  list: (runId: string) =>
    apiCall<{ run: string; entries: FeedbackEntry[]; summary: FeedbackSummary }>("GET", `${run(runId)}/feedback`),
  rate: (runId: string, rating: Rating, comment = "", node?: string | null, iteration?: number) =>
    apiCall<{ entry: FeedbackEntry; summary: FeedbackSummary }>("POST", `${run(runId)}/feedback`, {
      rating,
      comment,
      node: node ?? undefined,
      iteration,
    }),
  metrics: (runId: string) => apiCall<RunMetrics>("GET", `${run(runId)}/metrics`),
  /** The report as text (Markdown or pretty JSON), with the API token if one is set. */
  report: async (runId: string, format: ReportFormat = "md") =>
    (await apiRaw("GET", `${run(runId)}/report?format=${format}`)).text(),
  insights: (days: number, pipeline?: string | null) =>
    apiCall<Insights>(
      "GET",
      `/insights?days=${days}${pipeline ? `&pipeline=${encodeURIComponent(pipeline)}` : ""}`,
    ),
  telemetry: () => apiCall<TelemetryStatus>("GET", "/telemetry"),
  setTelemetry: (body: Partial<Pick<TelemetryStatus, "enabled" | "endpoint" | "service_name" | "headers_secret">>) =>
    apiCall<TelemetryStatus>("PUT", "/telemetry", body),
  testTelemetry: () => apiCall<{ ok: boolean; error?: string; status?: number }>("POST", "/telemetry/test"),
};

/** The rating this browser's person ("app") gave a target, from the summary's current list. */
export function myRating(
  summary: FeedbackSummary | null | undefined,
  node: string | null = null,
  by = "app",
): "up" | "down" | null {
  const hit = summary?.current.filter((e) => e.by === by && (e.node ?? null) === node).pop();
  return hit?.rating ?? null;
}

/** Click a thumb: the same one again withdraws it. */
export function nextRating(current: "up" | "down" | null, clicked: "up" | "down"): Rating {
  return current === clicked ? "none" : clicked;
}

export function formatDuration(s: number | null | undefined): string {
  const v = Number(s ?? 0);
  if (!Number.isFinite(v) || v <= 0) return "0 s";
  if (v >= 3600) return `${(v / 3600).toFixed(1)} h`;
  if (v >= 90) return `${(v / 60).toFixed(1)} min`;
  return `${Math.round(v)} s`;
}

export function formatTokens(t: Tokens | null | undefined): string {
  if (!t) return "–";
  const k = (n: number) => (n >= 10_000 ? `${Math.round(n / 1000)}k` : n.toLocaleString("en-US"));
  return `${k(t.input)} in / ${k(t.output)} out`;
}

export function percent(rate: number): string {
  return `${Math.round(100 * rate)}%`;
}

/** Bar heights (0-100) for a day series; an all-zero series gives all zeros. */
export function barHeights(values: number[]): number[] {
  const max = Math.max(0, ...values);
  return values.map((v) => (max > 0 ? (100 * Math.max(0, v)) / max : 0));
}

/** Days on which an edit was adopted, for marking the chart. */
export function adoptionDays(ins: Insights): Set<string> {
  return new Set(ins.adoptions.map((a) => new Date(a.at * 1000).toISOString().slice(0, 10)));
}

export function reportFilename(runId: string, format: ReportFormat): string {
  return `run-${runId}.${format}`;
}

// -- opening the report dialog from elsewhere (the Chat's `run_report` action) --------------
export const OPEN_REPORT_EVENT = "agentline:open-report";
let pending: { run: string; format: ReportFormat } | null = null;

export function requestReport(runId: string, format: ReportFormat = "md"): void {
  pending = { run: runId, format };
  window.dispatchEvent(new CustomEvent(OPEN_REPORT_EVENT, { detail: pending }));
}

/** A report asked for before the run's page was open (taken once). */
export function takePendingReport(runId: string): ReportFormat | null {
  if (pending?.run !== runId) return null;
  const f = pending.format;
  pending = null;
  return f;
}
