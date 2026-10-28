// Run reviews (engine: agentline/review.py, probe.py, lessons.py): the record types and the pure
// helpers the run view, the editor's Lessons tab and the Chat card share (review.test.ts).
import type { AssistantAction, ChatReply } from "./assistant";

export interface ReviewSignals {
  run: string;
  flow: string;
  status: string;
  cost_usd: number;
  duration_s: number;
  loops: { edge: string; count: number }[];
  retries: { node: string; iteration: number; attempts: number }[];
  manual_retries: number;
  gate_rejections: { node: string; iteration: number; comment: string }[];
  needs_review: string[];
  empty: string[];
  spike: Partial<Record<"cost" | "duration", { run: number; median: number }>> | null;
  triage: { id: string; node: string | null; verdict: string | null; status: string; summary: string }[];
  trouble: number;
}

export type RowVerdict = "problem" | "helped" | "hurt" | "no effect" | "confounded";

export interface ReviewRow {
  node: string;
  component: string;
  observed: string;
  evidence: string;
  verdict: RowVerdict;
}

export interface ProbeSide {
  status: string;
  output: string;
  error: string | null;
  diff: string;
  schema_errors?: string[];
}

export interface Probe {
  id: string;
  index: number;
  status: "running" | "done" | "failed" | "skipped";
  node: string | null;
  iteration: number | null;
  verdict: "better" | "worse" | "same" | null;
  reasoning: string;
  cost_usd: number;
  edit: string;
  notes: string[];
  error: string | null;
  baseline: ProbeSide | null;
  candidate: ProbeSide | null;
  samples: number;
}

export interface ReviewRecord {
  id: string;
  run: string;
  flow: string;
  manual: boolean;
  status: "running" | "done" | "failed" | "skipped";
  signals: ReviewSignals | null;
  summary: string;
  keep: boolean;
  rows: ReviewRow[];
  lesson: { node: string; component: string; cue: string; lesson: string; evidence: string; confidence: string } | null;
  actions: AssistantAction[];
  probes: Record<string, Probe>;
  /** proposal index -> adoption id */
  adopted: Record<string, string>;
  /** the lesson card's entry (L<n>) in the pipeline's lessons.md, once applied */
  lesson_added?: number | null;
  pipeline: string | null;
  harness: string | null;
  model: string | null;
  cost_usd: number;
  probe_cost_usd: number;
  error: string | null;
  created_at: number;
  updated_at: number;
}

export interface ReviewConfig {
  auto: "signals" | "all" | "off";
  spike_factor: number;
  max_probes: number;
  probe_budget_usd: number;
  followup_runs: number;
}

export type LessonStatus = "proposed" | "adopted" | "helped" | "hurt" | "no effect" | "dismissed";

export interface Lesson {
  id: number;
  node: string;
  component: string;
  cue: string;
  lesson: string;
  evidence: string;
  confidence: string;
  status: LessonStatus;
  review: string;
  runs: string;
  added: string;
  notes: string[];
}

export interface Rates {
  runs: number;
  trouble: number;
  cost_usd: number;
}

export interface Adoption {
  id: string;
  review: string;
  index: number;
  run: string;
  flow: string;
  pipeline: string | null;
  node: string | null;
  component: string | null;
  summary: string;
  at: number;
  commit: string | null;
  lessons: number[];
  baseline: Rates;
  after: Rates;
  verdict: "helped" | "hurt" | "no effect" | null;
  detail: string;
  actions: AssistantAction[];
}

/** The `<Status>` to show for a review: a status for its tone and the word to print. */
export function reviewStatus(
  r: Pick<ReviewRecord, "status" | "keep" | "actions"> & Partial<Pick<ReviewRecord, "adopted" | "lesson_added">>,
): { s: string; word: string } {
  if (r.status === "running") return { s: "running", word: "Reviewing" };
  if (r.status === "failed") return { s: "failed", word: "Review failed" };
  if (r.status === "skipped") return { s: "skipped", word: "Review skipped" };
  const open = r.actions.filter((a, i) => !a.issues.length && !isApplied(r, i)).length;
  if (!open) return { s: "done", word: r.keep ? "Nothing to change" : "Reviewed" };
  return { s: "needs-review", word: `${open} suggestion${open === 1 ? "" : "s"}` };
}

const fmtS = (s: number) => (s >= 90 ? `${(s / 60).toFixed(1)} min` : `${Math.round(s)} s`);

/** Whether proposal `i` was applied (an edit adopted, or the lesson added to the library). */
export function isApplied(r: Partial<Pick<ReviewRecord, "adopted" | "lesson_added">> & Pick<ReviewRecord, "actions">, i: number): boolean {
  const a = r.actions[i];
  if (!a) return false;
  if (a.type === "pipeline_lesson") return r.lesson_added != null;
  return !!r.adopted?.[String(i)];
}

/** The signals in words (same wording as review.signal_lines). */
export function signalLines(sig: ReviewSignals | null): string[] {
  if (!sig) return [];
  const out: string[] = [];
  for (const x of sig.loops) out.push(`loop ${x.edge} taken ${x.count} time(s)`);
  for (const x of sig.gate_rejections) out.push(`gate ${x.node} rejected${x.comment ? `: ${x.comment}` : ""}`);
  for (const x of sig.retries) out.push(`${x.node} (iteration ${x.iteration}) took ${x.attempts} attempts`);
  if (sig.manual_retries) out.push(`retried by hand ${sig.manual_retries} time(s)`);
  for (const n of sig.needs_review) out.push(`${n} stopped for review (needs-review)`);
  for (const n of sig.empty) out.push(`agent node ${n} finished with an empty result`);
  for (const [k, v] of Object.entries(sig.spike ?? {})) {
    if (!v) continue;
    const f = k === "cost" ? (x: number) => `$${x.toFixed(2)}` : fmtS;
    out.push(`${k} spike: ${f(v.run)} vs a median of ${f(v.median)}`);
  }
  for (const t of sig.triage) out.push(`triage of ${t.node || "the run"}: ${t.verdict || t.status}`);
  return out;
}

/** A row's verdict as a `<Status>` tone. */
export function rowStatus(v: RowVerdict): string {
  return { problem: "needs-review", helped: "done", hurt: "dead", "no effect": "skipped", confounded: "pending" }[v];
}

/** A probe as a `<Status>`: "Probed: better" etc. */
export function probeStatus(p: Pick<Probe, "status" | "verdict"> | null | undefined): { s: string; word: string } | null {
  if (!p) return null;
  if (p.status === "running") return { s: "running", word: "Probing" };
  if (p.status === "failed") return { s: "failed", word: "Probe failed" };
  if (p.status === "skipped") return { s: "skipped", word: "Not probed" };
  if (p.verdict === "better") return { s: "done", word: "Probed: better" };
  if (p.verdict === "worse") return { s: "dead", word: "Probed: worse" };
  return { s: "skipped", word: "Probed: no clear difference" };
}

export function lessonStatus(s: LessonStatus): string {
  return { proposed: "pending", adopted: "running", helped: "done", hurt: "dead", "no effect": "skipped", dismissed: "cancelled" }[s];
}

/** What an adoption's follow-up says so far. */
export function adoptionLine(a: Adoption, followupRuns = 5): string {
  if (a.verdict) return `${a.verdict}: ${a.detail}`;
  const n = a.after?.runs ?? 0;
  return `checking: ${n} of ${followupRuns} runs since (trouble before: ${a.baseline.trouble.toFixed(1)} per run)`;
}

/** The Lessons page's summary over every pipeline's library (dismissed lessons not counted). */
export function librarySummary(libs: { lessons: Lesson[]; adoptions: Adoption[] }[]): {
  pipelines: number;
  lessons: number;
  edits: number;
  checking: number;
  helped: number;
  hurt: number;
  noEffect: number;
} {
  const edits = libs.flatMap((l) => l.adoptions);
  const n = (v: Adoption["verdict"]) => edits.filter((a) => a.verdict === v).length;
  return {
    pipelines: libs.length,
    lessons: libs.flatMap((l) => l.lessons).filter((x) => x.status !== "dismissed").length,
    edits: edits.length,
    checking: n(null),
    helped: n("helped"),
    hurt: n("hurt"),
    noEffect: n("no effect"),
  };
}

export function latestReview(records: ReviewRecord[]): ReviewRecord | null {
  return records.reduce<ReviewRecord | null>((a, r) => (!a || r.created_at > a.created_at ? r : a), null);
}

/** Whether proposal `i` can be probed: a pipeline edit that validates. */
export function canProbe(r: ReviewRecord, i: number): boolean {
  const a = r.actions[i];
  return !!a && a.type === "pipeline" && !a.issues.length;
}

/** The review as a Chat message with its proposals as cards ("Open in Chat"). */
export function reviewReply(r: ReviewRecord): ChatReply {
  const head = r.status === "done" ? `**Review of run ${r.run}.** ${r.summary}` : `**Review of run ${r.run}: ${reviewStatus(r).word}.** ${r.error ?? ""}`;
  const parts = [head.trim()];
  const sig = signalLines(r.signals);
  if (sig.length) parts.push(sig.map((s) => `- ${s}`).join("\n"));
  const rows = r.rows.filter((x) => x.verdict !== "confounded");
  if (rows.length) parts.push(rows.map((x) => `- **${x.node}** ${x.component}: ${x.observed}`).join("\n"));
  if (r.actions.length) parts.push("Apply what you agree with (probe an edit first to see it on this run's inputs):");
  else if (r.keep) parts.push("Nothing in the pipeline's instructions to change.");
  return { message: parts.join("\n\n"), choices: [], actions: r.actions, harness: r.harness ?? "", model: r.model, cost_usd: r.cost_usd };
}

/** Whether the run view offers "Review" for a run status. */
export function canReview(status: string | undefined): boolean {
  return status === "done" || status === "dead";
}
