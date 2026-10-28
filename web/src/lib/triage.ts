// Failure triage (engine: agentline/triage.py): the record's types and the pure helpers the
// run view, Problems and the Chat card share (tested in triage.test.ts).
import type { AssistantAction, ChatReply } from "./assistant";

export type Verdict = "ai-instruction" | "infrastructure" | "external";

export interface TriageRecord {
  id: string;
  run: string;
  flow: string;
  node: string | null;
  execution: string | null;
  trigger: string;
  manual: boolean;
  status: "running" | "done" | "failed" | "skipped";
  verdict: Verdict | null;
  summary: string;
  reasoning: string;
  lesson: string;
  report: { title: string; body: string; markdown: string } | null;
  actions: AssistantAction[];
  issue: number | null;
  github: { repo?: string; number?: number; url?: string; comment?: boolean } | null;
  github_error: string | null;
  fingerprint: string | null;
  pipeline: string | null;
  harness: string | null;
  /** records made before phase 35 name the harness here */
  agent?: string | null;
  model: string | null;
  cost_usd: number;
  error: string | null;
  created_at: number;
  updated_at: number;
  /** a crash / error report's id: an app report made without a model call */
  incident?: string;
  incident_count?: number;
}

export interface GithubStatus {
  repo: string | null;
  token_set: boolean;
  ready: boolean;
  auto: boolean;
  error: string | null;
}

export const VERDICT_LABEL: Record<Verdict, string> = {
  "ai-instruction": "AI instruction",
  infrastructure: "agentline issue",
  external: "External",
};

const VERDICT_HELP: Record<Verdict, string> = {
  "ai-instruction": "The pipeline's instructions led the agent wrong: fix the pipeline, and teach the Chat the lesson.",
  infrastructure: "agentline itself misbehaved: reported as an issue.",
  external: "Something outside agentline (a service, a key, a missing tool).",
};

export function verdictHelp(v: Verdict | null): string {
  return v ? VERDICT_HELP[v] : "";
}

/** The `<Status>` to show: a status for its tone (icon + color) and the word to print. */
export function triageStatus(r: Pick<TriageRecord, "status" | "verdict">): { s: string; word: string } {
  if (r.status === "running") return { s: "running", word: "Triaging" };
  if (r.status === "failed") return { s: "failed", word: "Triage failed" };
  if (r.status === "skipped") return { s: "skipped", word: "Triage skipped" };
  if (r.verdict === "infrastructure") return { s: "dead", word: VERDICT_LABEL.infrastructure };
  if (r.verdict === "external") return { s: "waiting", word: VERDICT_LABEL.external };
  return { s: "needs-review", word: r.verdict ? VERDICT_LABEL[r.verdict] : "Triage" };
}

/** The newest triage of a node (or of the whole run when `node` is null/undefined). */
export function latestTriage(records: TriageRecord[], node?: string | null): TriageRecord | null {
  const mine = node ? records.filter((r) => r.node === node) : records;
  return mine.reduce<TriageRecord | null>((a, r) => (!a || r.created_at > a.created_at ? r : a), null);
}

/** The triage as a Chat message with its proposals as cards ("Open in Chat"). */
export function triageReply(r: TriageRecord): ChatReply {
  const where = r.node ? ` at ${r.node}` : "";
  const head = r.verdict
    ? `**Triage of run ${r.run}${where}: ${VERDICT_LABEL[r.verdict]}.** ${r.summary}`
    : `**Triage of run ${r.run}${where}: ${triageStatus(r).word}.** ${r.error ?? ""}`;
  const parts = [head.trim()];
  if (r.reasoning) parts.push(r.reasoning);
  if (r.github?.url) parts.push(`Reported on GitHub: ${r.github.url}`);
  else if (r.report) parts.push("The report is on the run's issue in Problems.");
  if (r.actions.length) parts.push("Apply what you agree with:");
  return {
    message: parts.join("\n\n"),
    choices: [],
    actions: r.actions,
    harness: r.harness ?? r.agent ?? "",
    model: r.model,
    cost_usd: r.cost_usd,
  };
}

/** "Copy as GitHub issue": a title line and the report, ready to paste into a new issue. */
export function githubIssueMarkdown(r: TriageRecord): string {
  if (!r.report) return "";
  const title = r.report.title || r.summary || "pipeline failure";
  return `# [triage] ${title}\n\n${r.report.markdown}\n`;
}

/** What the report line says: where it went, or how to send it. */
export function reportLine(r: TriageRecord, gh: GithubStatus | null): string {
  if (r.verdict !== "infrastructure" || !r.report) return "";
  if (r.github?.url) return r.github.comment ? `Added to ${r.github.url}` : `Reported: ${r.github.url}`;
  if (r.github_error) return `GitHub said no: ${r.github_error}`;
  if (gh?.ready) return `Ready to report on ${gh.repo}`;
  if (gh?.error) return `[report] github_repo: ${gh.error}`;
  const missing = [
    ...(gh?.repo ? [] : ['github_repo = "owner/name" under [report] in agentline.toml']),
    ...(gh?.token_set ? [] : ["the GITHUB_TOKEN key"]),
  ];
  return `Saved on the run's issue. To report on GitHub, set ${missing.join(" and ")}.`;
}

/** Whether the run view offers "Triage" for a node / run status. */
export function canTriage(status: string | undefined): boolean {
  return status === "dead" || status === "needs-review" || status === "failed" || status === "done";
}

/** Where an app report comes from: "Crash report (app_crash), seen 2×", "Chat harness report"
 * (something the built-in Chat harness couldn't handle) or "Run r1 at build". */
export function appReportSource(r: TriageRecord): string {
  const seen = (r.incident_count ?? 1) > 1 ? `, seen ${r.incident_count}×` : "";
  if (r.incident && r.trigger === "chat-harness") return `Chat harness report${seen}`;
  if (r.incident) return `Crash report (${r.trigger})${seen}`;
  return `Run ${r.run}${r.node ? ` at ${r.node}` : ""}`;
}
