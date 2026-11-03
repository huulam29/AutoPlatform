import { describe, expect, it } from "vitest";
import {
  actionDetails,
  actionTitle,
  applyAction,
  autoApplicable,
  cardInput,
  dryRunSummary,
  instructionSkills,
  instructionsState,
  needsOwnClick,
  type ApplyDeps,
  type AssistantAction,
  type ChatMessage,
} from "./assistant";
import type { Lesson, Probe, ReviewRecord } from "./review";
import type { TriageRecord } from "./triage";

/** A fake API that records each call as "name arg1 arg2 …". */
function fakeDeps(): { deps: ApplyDeps; calls: string[] } {
  const calls: string[] = [];
  const rec =
    <T,>(name: string, out: T) =>
    async (...args: unknown[]): Promise<T> => {
      calls.push([name, ...args.map((a) => (typeof a === "string" ? a : JSON.stringify(a)))].join(" "));
      return out;
    };
  const deps: ApplyDeps = {
    putSetting: rec("putSetting", {}),
    deleteSetting: rec("deleteSetting", {}),
    claudeLogout: rec("claudeLogout", {}),
    chooseModels: rec("chooseModels", { note: "Added 1, removed 0 models." }),
    modelVisibility: rec("modelVisibility", {}),
    putSecret: rec("putSecret", {}),
    deleteSecret: rec("deleteSecret", {}),
    setupChoice: rec("setupChoice", {}),
    putChatInstructions: rec("putChatInstructions", {}),
    fetchSkills: rec("fetchSkills", {}),
    installSkills: rec("installSkills", { result: { installed: ["docker-build"] } }),
    putSkill: rec("putSkill", {}),
    deleteSkill: rec("deleteSkill", {}),
    importMcp: rec("importMcp", { imported: ["github"], keys_set: ["GH"] }),
    chatPair: rec("chatPair", { code: "123", link: "https://t.me/bot?start=123" }),
    chatTest: rec("chatTest", {}),
    chatUnlink: rec("chatUnlink", {}),
    chatNotify: rec("chatNotify", {}),
    native: rec("native", { path: "/p" }),
    macApp: () => true, // the engine rows below are the Mac app's; browser cases set false
    restartEngine: rec("restartEngine", {}),
    setPipelinesDir: rec("setPipelinesDir", { pipelines_dir: "/srv/p", note: null, seeded: false }),
    reveal: rec("reveal", {}),
    exportReports: rec("exportReports", {}),
    incidentSeen: rec("incidentSeen", {}),
    assistantApply: rec("assistantApply", { name: "dev/pipeline" }),
    exportPipeline: rec("exportPipeline", { filename: "dev.agentline.zip" }),
    importPipeline: rec("importPipeline", { name: "imported/pipeline", issues: [], applied: ["models.x"] }),
    dryRun: rec("dryRun", { status: "done", error: null, nodes: [{ node: "a", status: "done" }, { node: "b", status: "skipped" }] }),
    historyRestore: rec("historyRestore", {}),
    deletePipeline: rec("deletePipeline", { deleted: "demo/pipeline", removed: "demo/" }),
    setVision: rec("setVision", {}),
    startRun: rec("startRun", { run_id: "r42" }),
    sendRunMessage: rec("sendRunMessage", { id: 1 }),
    retryRun: rec("retryRun", { retried: 2 }),
    approve: rec("approve", {}),
    reject: rec("reject", {}),
    updateIssue: rec("updateIssue", {}),
    deleteIncident: rec("deleteIncident", {}),
    clearIncidents: rec("clearIncidents", {}),
    setBudget: rec("setBudget", {}),
    addLesson: rec("addLesson", {}),
    triageRun: rec("triageRun", { id: "t1", status: "done", verdict: "ai-instruction", summary: "No steps.", error: null } as unknown as TriageRecord),
    triageGithub: rec("triageGithub", { github: { url: "https://github.test/o/r/issues/1" } } as unknown as TriageRecord),
    githubSave: rec("githubSave", {}),
    githubLogin: rec("githubLogin", { state: "waiting", code: "AB12-CD34", url: "https://github.com/login/device", error: null }),
    githubTest: rec("githubTest", { ok: true, repo: "o/r", error: null }),
    reviewRun: rec("reviewRun", { id: "v1", status: "done", summary: "Two loops at write.", error: null } as unknown as ReviewRecord),
    reviewProbe: rec("reviewProbe", { status: "done", verdict: "better", error: null } as unknown as Probe),
    lessonAdd: rec("lessonAdd", { id: 3 } as unknown as Lesson),
    archiveConversation: rec("archiveConversation", {}),
  };
  return { deps, calls };
}

const act = (a: Record<string, unknown>): AssistantAction => ({ issues: [], ...a }) as unknown as AssistantAction;

// every action type: what applying calls, and what it reports
const CASES: [Record<string, unknown>, string, Record<string, unknown>?][] = [
  [{ type: "setting", section: "mcp", name: "gh", body: { command: "npx", enabled: false } }, 'putSetting mcp gh {"command":"npx","enabled":false}', { changed: "settings" }],
  [{ type: "delete_setting", section: "models", name: "m" }, "deleteSetting models m false", { changed: "settings" }],
  [
    { type: "delete_setting", section: "providers", name: "or", with_models: true },
    "deleteSetting providers or true",
    { changed: "settings" },
  ],
  [{ type: "source_models", provider: "or", models: ["a/b"] }, 'chooseModels or ["a/b"]', { result: "Added 1, removed 0 models." }],
  [{ type: "model_visibility", model: "free", hidden: true }, "modelVisibility free true", {}],
  [{ type: "delete_key", name: "K" }, "deleteSecret K"],
  [{ type: "chat_uses", harness: "codex", model: "gpt" }, "setupChoice codex gpt"],
  [{ type: "chat_instructions", text: "- Be brief." }, "putChatInstructions - Be brief.", { result: "Used from the next message on" }],
  [{ type: "fetch_skills" }, "fetchSkills"],
  [{ type: "skill", name: "ours", body: "b" }, "putSkill ours  b"],
  [{ type: "delete_skill", name: "ours" }, "deleteSkill ours"],
  [{ type: "install_skills", source: "docker/skills", names: ["docker-build"] }, `installSkills docker/skills ["docker-build"]`],
  [{ type: "mcp_import", items: [{ id: "claude-code:github" }] }, 'importMcp [{"id":"claude-code:github"}]', { result: "Imported github; keys saved: GH" }],
  [{ type: "chat_app", platform: "telegram", op: "pair" }, "chatPair telegram", { result: "Open https://t.me/bot?start=123 and press Start" }],
  [{ type: "chat_app", platform: "slack", op: "test" }, "chatTest slack"],
  [{ type: "chat_app", platform: "slack", op: "unlink" }, "chatUnlink slack"],
  [{ type: "chat_notify", gates: false, done: true }, 'chatNotify {"gates":false,"done":true}'],
  [{ type: "engine", op: "restart" }, "native engine.restart"],
  [{ type: "engine", op: "choose_folder" }, "native folder.choose"],
  [{ type: "pipeline", path: "dev/pipeline.yaml", yaml: "y" }, "assistantApply dev/pipeline.yaml y {}", { changed: "pipelines", pipeline: "dev/pipeline" }],
  [{ type: "export", pipeline: "dev" }, "exportPipeline dev", { result: "Saved dev.agentline.zip" }],
  [{ type: "dry_run", pipeline: "dev", payload: { k: 1 } }, 'dryRun {"name":"dev","payload":{"k":1}}', { result: "done: 2 nodes (b skipped)" }],
  [{ type: "restore", pipeline: "dev", commit: "abcdef12" }, "historyRestore dev abcdef12", { changed: "pipelines", pipeline: "dev" }],
  [{ type: "start_run", pipeline: "dev-cycle", payload: { key: "T-1" } }, 'startRun dev-cycle {"key":"T-1"}', { run: "r42" }],
  [{ type: "start_run", pipeline: "helpdesk", text: "fix login" }, "startRun helpdesk {} fix login", { run: "r42" }],
  [{ type: "run_message", run: "r1", text: "retry qa" }, "sendRunMessage r1 retry qa", { run: "r1", result: "Sent" }],
  [{ type: "retry", run: "r1", node: "plan" }, "retryRun r1 plan", { result: "Retried 2 steps" }],
  [{ type: "gate", run: "r1", node: "ok", decision: "approve", comment: "ship" }, "approve r1 ok ship", { run: "r1" }],
  [{ type: "gate", run: "r1", node: "ok", decision: "reject" }, "reject r1 ok "],
  [{ type: "issue", number: 3, status: "fixed", note: "done" }, 'updateIssue 3 {"status":"fixed","note":"done"}'],
  [{ type: "incident", op: "delete", id: "i1" }, "deleteIncident i1"],
  [{ type: "incident", op: "clear" }, "clearIncidents"],
  [{ type: "budget", monthly_usd: 25 }, "setBudget 25"],
  [{ type: "budget", monthly_usd: null }, "setBudget null"],
  [{ type: "triage", run: "r1", node: "build" }, "triageRun r1 build", { run: "r1", result: "AI instruction: No steps.", triage: { id: "t1" } }],
  [{ type: "lesson", text: "Give planners a schema." }, "addLesson Give planners a schema.", { changed: "settings", result: "Added to the Chat instructions" }],
  [{ type: "report_issue", triage: "t1" }, "triageGithub t1", { result: "Reported: https://github.test/o/r/issues/1" }],
  [{ type: "github", op: "save", repo: "o/r", auto: true }, 'githubSave {"repo":"o/r","auto":true}', { changed: "settings", result: "Reports go to o/r" }],
  [{ type: "github", op: "login" }, "githubLogin", { result: "Enter code AB12-CD34 at https://github.com/login/device (Settings > General shows when it's done)" }],
  [{ type: "github", op: "test" }, "githubTest", { result: "Can open issues in o/r" }],
  [{ type: "delete_pipeline", pipeline: "demo/pipeline" }, "deletePipeline demo/pipeline", { changed: "pipelines", result: "Deleted demo/" }],
  [{ type: "chat_vision", choice: "haiku-api" }, "setVision haiku-api", { changed: "settings" }],
  [{ type: "review", run: "r1" }, "reviewRun r1", { run: "r1", result: "Two loops at write.", review: { id: "v1" } }],
  [{ type: "probe", review: "v1", index: 0 }, "reviewProbe v1 0", { result: "Probe: better than the original (1 run)" }],
  [{ type: "conversation", op: "archive", id: "c-1" }, "archiveConversation c-1 true", { conversations: true, result: "Archived (History > Archived)" }],
  [{ type: "conversation", op: "unarchive", id: "c-1" }, "archiveConversation c-1 false", { conversations: true, result: "Back in History" }],
  [
    { type: "pipeline_lesson", pipeline: "dev/pipeline", node: "write", cue: "when x", lesson: "do y", confidence: "high", why: "w" },
    'lessonAdd dev/pipeline {"node":"write","cue":"when x","lesson":"do y","confidence":"high"}',
    { changed: "pipelines", result: "Lesson L3 is in the pipeline's lesson library" },
  ],
];

describe("Chat parity: applying each action", () => {
  for (const [a, call, out] of CASES) {
    it(`${String(a.type)} ${String(a.op ?? a.decision ?? "")}`, async () => {
      const { deps, calls } = fakeDeps();
      const r = await applyAction(act(a), deps);
      expect(calls).toEqual([call]);
      if (out) expect(r).toMatchObject(out);
      expect(actionTitle(act(a))).toBeTruthy();
    });
  }

  it("a key's value goes to /api/secrets only, and is required", async () => {
    const { deps, calls } = fakeDeps();
    await expect(applyAction(act({ type: "key", name: "K" }), deps)).rejects.toThrow("value");
    await applyAction(act({ type: "key", name: "K" }), deps, { value: "sk-1" });
    expect(calls).toEqual(["putSecret K sk-1"]);
    expect(cardInput(act({ type: "key", name: "K" }))).toBe("secret");
  });

  it("import needs a file picked on the card", async () => {
    const { deps, calls } = fakeDeps();
    const a = act({ type: "import", folder: "x", apply_settings: true });
    expect(cardInput(a)).toBe("file");
    await expect(applyAction(a, deps)).rejects.toThrow(".zip");
    const r = await applyAction(a, deps, { file: new Blob(["zip"]) });
    expect(calls[0]).toMatch(/^importPipeline .* {"folder":"x","applySettings":true}$/);
    expect(r).toMatchObject({ changed: "pipelines", pipeline: "imported/pipeline", result: "Imported imported/pipeline; settings added: models.x" });
  });
});

describe("Chat cards", () => {
  it("Apply all skips deletes, gates, runs, the engine, downloads and anything needing input", () => {
    const m: ChatMessage = {
      role: "assistant",
      content: "",
      reply: {
        message: "",
        choices: [],
        harness: "claude-code",
        cost_usd: 0,
        actions: [
          act({ type: "budget", monthly_usd: 5 }),
          act({ type: "gate", run: "r", node: "g", decision: "approve" }),
          act({ type: "delete_setting", section: "harnesses", name: "a" }),
          act({ type: "chat_notify", gates: true }),
          act({ type: "start_run", pipeline: "p" }),
          act({ type: "engine", op: "restart" }),
          act({ type: "issue", number: 1, status: "fixed" }),
          act({ type: "export", pipeline: "p" }),
        ],
      },
    };
    expect(autoApplicable(m)).toEqual([0, 3, 6]);
    expect(needsOwnClick(act({ type: "gate", run: "r", node: "g", decision: "reject" }))).toBe(true);
    // triage costs a model call and a GitHub issue is public: each waits for its own click
    expect(needsOwnClick(act({ type: "triage", run: "r" }))).toBe(true);
    expect(needsOwnClick(act({ type: "report_issue", triage: "t1" }))).toBe(true);
    // reviews and probes cost model calls; a lesson is part of Apply all
    expect(needsOwnClick(act({ type: "review", run: "r" }))).toBe(true);
    expect(needsOwnClick(act({ type: "probe", review: "v1", index: 0 }))).toBe(true);
    expect(needsOwnClick(act({ type: "pipeline_lesson", pipeline: "p", node: "n", cue: "c", lesson: "l" }))).toBe(false);
    expect(needsOwnClick(act({ type: "lesson", text: "x" }))).toBe(false);
  });

  it("titles and details say what applying does", () => {
    expect(actionTitle(act({ type: "setting", section: "mcp", name: "gh", body: { enabled: false } }))).toBe("Turn off MCP server gh");
    expect(actionTitle(act({ type: "gate", run: "r1", node: "deploy", decision: "reject" }))).toBe("Reject deploy in run r1");
    expect(actionTitle(act({ type: "budget", monthly_usd: null }))).toBe("Remove the monthly budget");
    expect(actionTitle(act({ type: "triage", run: "r1", node: "build" }))).toBe("Triage run r1 at build");
    expect(actionDetails(act({ type: "lesson", text: "Give planners a schema." }))).toEqual(["- Give planners a schema."]);
    expect(actionTitle(act({ type: "mcp_import", items: [{ id: "codex:sentry" }] }))).toBe("Import MCP server sentry");
    expect(actionDetails(act({ type: "chat_notify", gates: true, done: false }))).toEqual(["gates: on", "done: off"]);
    expect(actionDetails(act({ type: "issue", number: 2, status: "in_progress", note: "on it" }))).toEqual(["status: in_progress", "note: on it"]);
    expect(dryRunSummary({ status: "failed", error: "boom", nodes: [] })).toBe("failed: 0 nodes; boom");
  });
});

describe("Chat instructions", () => {
  it("finds the skills the instructions name", () => {
    expect(instructionSkills("- New pipeline: invoke the `interview-me` skill.\n- Reviews: `code-review-and-quality`, `interview-me`.")).toEqual([
      "interview-me",
      "code-review-and-quality",
    ]);
    expect(instructionSkills("no skills, `Not A Skill`")).toEqual([]);
  });
  it("enables Save and Reset", () => {
    expect(instructionsState("a", "a", "a")).toEqual({ dirty: false, canReset: false });
    expect(instructionsState("b", "a", "a")).toEqual({ dirty: true, canReset: true });
    expect(instructionsState("b", "b", "a")).toEqual({ dirty: false, canReset: true });
  });
  it("the card shows the new text", () => {
    expect(actionDetails(act({ type: "chat_instructions", text: "- a\n- b\n" }))).toEqual(["- a", "- b"]);
    expect(actionTitle(act({ type: "chat_instructions", text: "x" }))).toBe("Change the Chat instructions");
  });
});
