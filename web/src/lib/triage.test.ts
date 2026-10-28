import { describe, expect, it } from "vitest";
import { assistantInbox } from "./assistant";
import {
  appReportSource,
  canTriage,
  githubIssueMarkdown,
  latestTriage,
  reportLine,
  triageReply,
  triageStatus,
  verdictHelp,
  type GithubStatus,
  type TriageRecord,
} from "./triage";

function rec(over: Partial<TriageRecord> = {}): TriageRecord {
  return {
    id: "t1",
    run: "r1",
    flow: "p",
    node: "build",
    execution: "x1",
    trigger: "dead",
    manual: false,
    status: "done",
    verdict: "ai-instruction",
    summary: "The plan had no steps.",
    reasoning: "The prompt never asks for steps.",
    lesson: "Give planners a schema.",
    report: null,
    actions: [{ type: "lesson", text: "Give planners a schema.", issues: [] }],
    issue: 3,
    github: null,
    github_error: null,
    fingerprint: "abc",
    pipeline: "p",
    harness: "claude-code",
    model: null,
    cost_usd: 0.02,
    error: null,
    created_at: 10,
    updated_at: 11,
    ...over,
  };
}

const NO_GH: GithubStatus = { repo: null, token_set: false, ready: false, auto: false, error: null };
const INFRA = rec({
  verdict: "infrastructure",
  report: { title: "Scheduler drops results", body: "b", markdown: "**Pipeline** `p`\n\n## What happened" },
});

describe("triage", () => {
  it("shows the verdict as a status tone and a word", () => {
    expect(triageStatus(rec())).toEqual({ s: "needs-review", word: "AI instruction" });
    expect(triageStatus(INFRA)).toEqual({ s: "dead", word: "agentline issue" });
    expect(triageStatus(rec({ verdict: "external" }))).toEqual({ s: "waiting", word: "External" });
    expect(triageStatus(rec({ status: "running", verdict: null }))).toEqual({ s: "running", word: "Triaging" });
    expect(triageStatus(rec({ status: "skipped", verdict: null })).word).toBe("Triage skipped");
    expect(triageStatus(rec({ status: "failed", verdict: null })).s).toBe("failed");
    expect(verdictHelp("external")).toMatch(/outside agentline/);
    expect(verdictHelp(null)).toBe("");
  });

  it("picks the newest triage of a node or the run", () => {
    const rs = [rec({ id: "a", created_at: 1 }), rec({ id: "b", created_at: 5 }), rec({ id: "c", node: "plan", created_at: 9 })];
    expect(latestTriage(rs, "build")?.id).toBe("b");
    expect(latestTriage(rs)?.id).toBe("c");
    expect(latestTriage(rs, "deploy")).toBeNull();
    expect(latestTriage([])).toBeNull();
  });

  it("becomes a Chat card with its proposals", () => {
    const r = triageReply(rec());
    expect(r.message).toMatch(/^\*\*Triage of run r1 at build: AI instruction\.\*\* The plan had no steps\./);
    expect(r.message).toContain("The prompt never asks for steps.");
    expect(r.actions).toHaveLength(1);
    expect(r.cost_usd).toBe(0.02);
    expect(triageReply({ ...INFRA, github: { url: "https://github.test/o/r/issues/1" } }).message).toContain("Reported on GitHub: https://github.test/o/r/issues/1");
    expect(triageReply(INFRA).message).toContain("on the run's issue in Problems");
    expect(triageReply(rec({ status: "skipped", verdict: null, error: "budget used up", reasoning: "", actions: [] })).message).toBe(
      "**Triage of run r1 at build: Triage skipped.** budget used up",
    );
  });

  it("the Chat inbox carries a ready card as well as text", () => {
    const reply = triageReply(rec());
    assistantInbox.push("fix it");
    assistantInbox.push(reply);
    expect(assistantInbox.take()).toBe("fix it");
    expect(assistantInbox.take()).toBe(reply);
    expect(assistantInbox.take()).toBeUndefined();
  });

  it("copies the report as a GitHub issue", () => {
    expect(githubIssueMarkdown(INFRA)).toBe("# [triage] Scheduler drops results\n\n**Pipeline** `p`\n\n## What happened\n");
    expect(githubIssueMarkdown(rec())).toBe("");
  });

  it("says where the report went or what's missing to send it", () => {
    expect(reportLine(rec(), NO_GH)).toBe("");
    expect(reportLine(INFRA, NO_GH)).toBe(
      'Saved on the run\'s issue. To report on GitHub, set github_repo = "owner/name" under [report] in agentline.toml and the GITHUB_TOKEN key.',
    );
    expect(reportLine(INFRA, { ...NO_GH, repo: "o/r" })).toBe("Saved on the run's issue. To report on GitHub, set the GITHUB_TOKEN key.");
    expect(reportLine(INFRA, { ...NO_GH, repo: "o/r", token_set: true, ready: true })).toBe("Ready to report on o/r");
    expect(reportLine({ ...INFRA, github: { url: "u" } }, NO_GH)).toBe("Reported: u");
    expect(reportLine({ ...INFRA, github: { url: "u", comment: true } }, NO_GH)).toBe("Added to u");
    expect(reportLine({ ...INFRA, github_error: "HTTP 401" }, NO_GH)).toBe("GitHub said no: HTTP 401");
  });

  it("offers Triage on failed or finished nodes only", () => {
    expect(["dead", "needs-review", "done", "running", "pending"].map(canTriage)).toEqual([true, true, true, false, false]);
  });

  it("names where an app report comes from", () => {
    expect(appReportSource({ ...INFRA, incident: "abc", trigger: "app_crash", incident_count: 2 })).toBe("Crash report (app_crash), seen 2×");
    expect(appReportSource({ ...INFRA, incident: "abc", trigger: "api_error" })).toBe("Crash report (api_error)");
    expect(appReportSource({ ...INFRA, incident: "abc", trigger: "chat-harness", incident_count: 3 })).toBe(
      "Chat harness report, seen 3×",
    );
    expect(appReportSource(INFRA)).toBe("Run r1 at build");
  });
});
