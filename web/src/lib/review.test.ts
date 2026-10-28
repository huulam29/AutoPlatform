import { describe, expect, it } from "vitest";
import type { AssistantAction } from "./assistant";
import {
  adoptionLine,
  canProbe,
  canReview,
  isApplied,
  librarySummary,
  latestReview,
  lessonStatus,
  probeStatus,
  reviewReply,
  reviewStatus,
  rowStatus,
  signalLines,
  type Adoption,
  type ReviewRecord,
  type ReviewSignals,
} from "./review";

const SIG: ReviewSignals = {
  run: "r1",
  flow: "p",
  status: "done",
  cost_usd: 0.4,
  duration_s: 300,
  loops: [{ edge: "r->write[again]", count: 2 }],
  retries: [{ node: "build", iteration: 1, attempts: 3 }],
  manual_retries: 1,
  gate_rejections: [{ node: "ok", iteration: 1, comment: "no tests" }],
  needs_review: ["deploy"],
  empty: ["plan"],
  spike: { cost: { run: 0.4, median: 0.1 }, duration: { run: 300, median: 60 } },
  triage: [{ id: "t1", node: "build", verdict: "external", status: "done", summary: "" }],
  trouble: 7,
};

const pipe = (issues: string[] = []): AssistantAction =>
  ({ type: "pipeline", path: "p/pipeline.yaml", yaml: "x", issues, node: "write", confidence: "medium" }) as AssistantAction;

function rec(over: Partial<ReviewRecord> = {}): ReviewRecord {
  return {
    id: "v1",
    run: "r1",
    flow: "p",
    manual: false,
    status: "done",
    signals: SIG,
    summary: "Two loops at write.",
    keep: false,
    rows: [
      { node: "write", component: "prompt", observed: "sent back twice", evidence: "check #1", verdict: "problem" },
      { node: "plan", component: "model", observed: "maybe", evidence: "", verdict: "confounded" },
    ],
    lesson: null,
    actions: [pipe(), pipe(["bad yaml"])],
    probes: {},
    adopted: {},
    pipeline: "p/pipeline",
    harness: "claude-code",
    model: null,
    cost_usd: 0.03,
    probe_cost_usd: 0,
    error: null,
    created_at: 10,
    updated_at: 10,
    ...over,
  };
}

describe("run reviews", () => {
  it("says the signals in words, like the engine", () => {
    expect(signalLines(SIG)).toEqual([
      "loop r->write[again] taken 2 time(s)",
      "gate ok rejected: no tests",
      "build (iteration 1) took 3 attempts",
      "retried by hand 1 time(s)",
      "deploy stopped for review (needs-review)",
      "agent node plan finished with an empty result",
      "cost spike: $0.40 vs a median of $0.10",
      "duration spike: 5.0 min vs a median of 60 s",
      "triage of build: external",
    ]);
    expect(signalLines(null)).toEqual([]);
  });

  it("status words count only proposals that validate", () => {
    expect(reviewStatus(rec())).toEqual({ s: "needs-review", word: "1 suggestion" });
    expect(reviewStatus(rec({ actions: [], keep: true }))).toEqual({ s: "done", word: "Nothing to change" });
    expect(reviewStatus(rec({ adopted: { "0": "a1" } }))).toEqual({ s: "done", word: "Reviewed" }); // applied: not open
    const lesson = { type: "pipeline_lesson", pipeline: "p", node: "w", cue: "c", lesson: "l", issues: [] } as AssistantAction;
    expect(isApplied(rec({ actions: [lesson] }), 0)).toBe(false);
    expect(isApplied(rec({ actions: [lesson], lesson_added: 2 }), 0)).toBe(true);
    expect(reviewStatus(rec({ actions: [pipe(), lesson], adopted: { "0": "a" }, lesson_added: 2 })).word).toBe("Reviewed");
    expect(reviewStatus(rec({ status: "running" })).word).toBe("Reviewing");
    expect(reviewStatus(rec({ status: "skipped" })).s).toBe("skipped");
  });

  it("tones for rows, probes and lessons", () => {
    expect(rowStatus("problem")).toBe("needs-review");
    expect(rowStatus("hurt")).toBe("dead");
    expect(probeStatus({ status: "done", verdict: "better" })).toEqual({ s: "done", word: "Probed: better" });
    expect(probeStatus({ status: "done", verdict: "worse" })?.s).toBe("dead");
    expect(probeStatus({ status: "done", verdict: "same" })?.word).toBe("Probed: no clear difference");
    expect(probeStatus({ status: "skipped", verdict: null })?.word).toBe("Not probed");
    expect(probeStatus(undefined)).toBeNull();
    expect(lessonStatus("helped")).toBe("done");
    expect(lessonStatus("dismissed")).toBe("cancelled");
  });

  it("only valid pipeline edits can be probed; done and dead runs can be reviewed", () => {
    expect(canProbe(rec(), 0)).toBe(true);
    expect(canProbe(rec(), 1)).toBe(false);
    expect(canProbe(rec(), 5)).toBe(false);
    expect(canReview("done") && canReview("dead")).toBe(true);
    expect(canReview("running")).toBe(false);
  });

  it("the newest review wins", () => {
    expect(latestReview([rec({ id: "a", created_at: 1 }), rec({ id: "b", created_at: 5 })])?.id).toBe("b");
    expect(latestReview([])).toBeNull();
  });

  it("Open in Chat: the summary, signals, findings (not the confounded ones) and the cards", () => {
    const r = reviewReply(rec());
    expect(r.message).toContain("**Review of run r1.** Two loops at write.");
    expect(r.message).toContain("- loop r->write[again] taken 2 time(s)");
    expect(r.message).toContain("- **write** prompt: sent back twice");
    expect(r.message).not.toContain("maybe");
    expect(r.actions).toHaveLength(2);
    expect(reviewReply(rec({ actions: [], keep: true })).message).toContain("Nothing in the pipeline's instructions to change.");
  });

  it("the Lessons page summary", () => {
    const l = (status: string) => ({ status }) as never;
    const a = (verdict: string | null) => ({ verdict }) as never;
    expect(
      librarySummary([
        { lessons: [l("adopted"), l("dismissed")], adoptions: [a(null), a("helped")] },
        { lessons: [l("proposed")], adoptions: [a("hurt"), a("no effect"), a("helped")] },
      ]),
    ).toEqual({ pipelines: 2, lessons: 2, edits: 5, checking: 1, helped: 2, hurt: 1, noEffect: 1 });
    expect(librarySummary([])).toMatchObject({ pipelines: 0, lessons: 0, edits: 0 });
  });

  it("an adoption's follow-up line", () => {
    const ad = { verdict: null, detail: "", after: { runs: 2, trouble: 0, cost_usd: 0 }, baseline: { runs: 5, trouble: 1.4, cost_usd: 0 } } as unknown as Adoption;
    expect(adoptionLine(ad, 5)).toBe("checking: 2 of 5 runs since (trouble before: 1.4 per run)");
    expect(adoptionLine({ ...ad, verdict: "helped", detail: "trouble 1.4 -> 0.2 per run over 5 runs" })).toBe("helped: trouble 1.4 -> 0.2 per run over 5 runs");
  });
});
