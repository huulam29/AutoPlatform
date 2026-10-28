// The editor assistant's state and pure helpers (tested in assistant.test.ts). The chat panel
// (components/Assistant.tsx) talks to /api/assistant; the editor publishes the open pipeline
// here and reloads when the assistant writes it.
import { VERDICT_LABEL, type TriageRecord } from "./triage";
import type { Lesson, Probe, ReviewRecord } from "./review";


type Why = { why?: string };
export type SettingAction = {
  type: "setting";
  section: "providers" | "models" | "harnesses" | "mcp";
  name: string;
  body: Record<string, unknown>;
} & Why;
export type KeyAction = { type: "key"; name: string } & Why;
export type FetchSkillsAction = { type: "fetch_skills" } & Why;
export type SkillAction = { type: "skill"; name: string; description?: string; body: string } & Why;
export type RetryAction = { type: "retry"; run: string; node?: string } & Why;
export type PipelineAction = {
  type: "pipeline";
  path: string;
  yaml: string;
  files?: Record<string, string>;
  summary?: string;
  exists?: boolean;
  /** from a run review: the review, the node it changes, how sure it is, its probe */
  review?: string;
  node?: string;
  confidence?: "high" | "medium" | "low";
  probe?: { status: Probe["status"]; verdict: Probe["verdict"]; reasoning: string };
};
export type PipelineLessonAction = {
  type: "pipeline_lesson";
  pipeline: string;
  node: string;
  component?: string;
  cue: string;
  lesson: string;
  evidence?: string;
  confidence?: string;
  review?: string;
  runs?: string;
};
export type MoreAction = (
  | { type: "delete_setting"; section: SettingAction["section"]; name: string; with_models?: boolean }
  | { type: "claude_logout"; provider: string }
  | { type: "source_models"; provider: string; models: string[] }
  | { type: "model_visibility"; model: string; hidden: boolean }
  | { type: "delete_key"; name: string }
  | { type: "chat_uses"; harness: string; model?: string | null }
  | { type: "chat_vision"; choice: string }
  | { type: "chat_instructions"; text: string }
  | { type: "delete_skill"; name: string }
  | { type: "install_skills"; source: string; names?: string[] }
  | { type: "mcp_import"; items: { id: string; name?: string }[] }
  | { type: "chat_app"; platform: string; op: "pair" | "test" | "unlink" }
  | { type: "chat_notify"; gates?: boolean; failures?: boolean; done?: boolean; questions?: boolean; reviews?: boolean }
  | { type: "engine"; op: "restart" | "choose_folder" }
  | { type: "export"; pipeline: string }
  | { type: "import"; folder?: string; overwrite?: boolean; apply_settings?: boolean }
  | { type: "dry_run"; pipeline: string; payload?: unknown; routes?: Record<string, string[]> }
  | { type: "restore"; pipeline: string; commit: string }
  | { type: "delete_pipeline"; pipeline: string }
  | { type: "start_run"; pipeline: string; payload?: unknown; text?: string }
  | { type: "run_message"; run: string; text: string }
  | { type: "gate"; run: string; node: string; decision: "approve" | "reject"; comment?: string }
  | { type: "issue"; number: number; status?: string | null; note?: string }
  | { type: "incident"; op: "delete" | "clear"; id?: string }
  | { type: "budget"; monthly_usd: number | null }
  | { type: "triage"; run: string; node?: string }
  | { type: "lesson"; text: string }
  | { type: "report_issue"; triage: string }
  | { type: "github"; op?: "save" | "login" | "test"; repo?: string; auto?: boolean }
  | { type: "review"; run: string }
  | { type: "probe"; review: string; index: number }
  | { type: "conversation"; op: "archive" | "unarchive"; id: string }
  | PipelineLessonAction
) &
  Why;
export type AssistantAction = (
  | SettingAction
  | KeyAction
  | FetchSkillsAction
  | PipelineAction
  | SkillAction
  | RetryAction
  | MoreAction
) & {
  issues: string[];
};

export type ActionState = "pending" | "applying" | "applied" | "failed";

export interface ChatReply {
  message: string;
  choices: string[];
  actions: AssistantAction[];
  harness: string;
  model?: string | null;
  cost_usd: number;
  repaired?: boolean;
  /** images the harness couldn't see, as a vision model read them */
  transcripts?: { id: string; text: string; by: string; edited: boolean; cached?: boolean }[];
  /** the built-in harness's lookups this turn (chat_harness.py) */
  steps?: string[];
  /** what the built-in harness couldn't handle, recorded as an App report */
  report?: { id: string; class: string; title: string } | null;
}

/** "Looked at: read dev/prompts/plan.md, run r1" under a built-in harness reply. */
export function lookedAt(reply: ChatReply | undefined): string | null {
  const steps = reply?.steps ?? [];
  return steps.length ? `Looked at: ${steps.join(", ")}` : null;
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  /** image ids (POST /api/assistant/attachments) */
  attachments?: string[];
  reply?: ChatReply;
  /** per action index: applied or not, why it failed, what it showed (a dry run's outcome) */
  states?: Record<number, { state: ActionState; error?: string; result?: string; run?: string }>;
}

const SECTION_NAME: Record<string, string> = {
  providers: "model source",
  models: "model",
  harnesses: "harness",
  mcp: "MCP server",
};

export function actionTitle(a: AssistantAction): string {
  switch (a.type) {
    case "setting": {
      const off = a.section === "mcp" && a.body.enabled === false;
      return `${off ? "Turn off" : "Set"} ${SECTION_NAME[a.section] ?? a.section} ${a.name}`;
    }
    case "delete_setting":
      return `Delete ${SECTION_NAME[a.section] ?? a.section} ${a.name}${a.with_models ? " and its models" : ""}`;
    case "claude_logout":
      return "Sign Claude Code out";
    case "source_models":
      return `Choose the models of ${a.provider} (${a.models.length})`;
    case "model_visibility":
      return `${a.hidden ? "Hide" : "Show"} ${a.model} ${a.hidden ? "from" : "in"} pickers`;
    case "key":
      return `Set key ${a.name}`;
    case "delete_key":
      return `Delete key ${a.name}`;
    case "chat_uses":
      return `Chat uses ${a.harness}${a.model ? ` / ${a.model}` : ""}`;
    case "chat_vision":
      return `Images are read by ${a.choice === "@auto" ? "the automatic pick" : a.choice === "@claude-code" ? "Claude Code" : a.choice}`;
    case "chat_instructions":
      return "Change the Chat instructions";
    case "fetch_skills":
      return "Download the skill library";
    case "skill":
      return `Add your skill ${a.name}`;
    case "delete_skill":
      return `Delete skill ${a.name}`;
    case "install_skills":
      return a.names?.length ? `Install ${a.names.join(", ")} from ${a.source}` : `Install the skills of ${a.source}`;
    case "mcp_import":
      return `Import MCP server${a.items.length === 1 ? "" : "s"} ${a.items.map((i) => i.name || i.id.split(":").pop()).join(", ")}`;
    case "chat_app":
      return { pair: "Link", test: "Send a test to", unlink: "Unlink" }[a.op] + ` ${a.platform}`;
    case "chat_notify":
      return "Change chat notifications";
    case "engine":
      return a.op === "restart" ? "Restart the engine" : "Choose the pipelines folder";
    case "pipeline":
      return `${a.exists ? "Update" : "Create"} pipeline ${a.path}`;
    case "export":
      return `Export ${a.pipeline}`;
    case "import":
      return "Import a pipeline";
    case "dry_run":
      return `Dry run ${a.pipeline}`;
    case "restore":
      return `Restore ${a.pipeline} to ${a.commit.slice(0, 8)}`;
    case "delete_pipeline":
      return `Delete pipeline ${a.pipeline}`;
    case "start_run":
      return `Start ${a.pipeline}`;
    case "run_message":
      return `Message run ${a.run}`;
    case "retry":
      return `Retry run ${a.run}${a.node ? ` at ${a.node}` : ""}`;
    case "gate":
      return `${a.decision === "approve" ? "Approve" : "Reject"} ${a.node} in run ${a.run}`;
    case "issue":
      return `Update issue #${a.number}`;
    case "incident":
      return a.op === "clear" ? "Delete all crash reports" : `Delete crash report ${a.id}`;
    case "budget":
      return a.monthly_usd == null ? "Remove the monthly budget" : `Set the monthly budget to $${a.monthly_usd}`;
    case "triage":
      return `Triage run ${a.run}${a.node ? ` at ${a.node}` : ""}`;
    case "lesson":
      return "Add a lesson to the Chat instructions";
    case "report_issue":
      return "Report it on GitHub";
    case "github":
      return a.op === "login" ? "Sign in with GitHub" : a.op === "test" ? "Test the GitHub setup" : `Report to GitHub repo ${a.repo ?? "(unchanged)"}`;
    case "review":
      return `Review run ${a.run}`;
    case "probe":
      return `Probe proposal ${a.index} of review ${a.review}`;
    case "pipeline_lesson":
      return `Add a lesson for ${a.node} to ${a.pipeline}`;
    case "conversation":
      return `${a.op === "archive" ? "Archive" : "Unarchive"} the conversation ${a.id}`;
  }
}

/** The card's detail lines (what applying it would do). */
export function actionDetails(a: AssistantAction): string[] {
  const json = (v: unknown) => JSON.stringify(v);
  switch (a.type) {
    case "setting":
      return settingLines(a.body);
    case "source_models":
      return a.models;
    case "chat_instructions":
      return a.text.trimEnd().split("\n");
    case "chat_notify":
      return (["gates", "failures", "done", "questions", "reviews"] as const)
        .filter((k) => a[k] !== undefined)
        .map((k) => `${k}: ${a[k] ? "on" : "off"}`);
    case "mcp_import":
      return a.items.map((i) => (i.name ? `${i.id} as ${i.name}` : i.id));
    case "start_run":
      if (a.text !== undefined) return [`"${a.text}"`];
      return a.payload !== undefined ? [`payload ${json(a.payload)}`] : [];
    case "run_message":
      return [`"${a.text}"`];
    case "dry_run":
      return a.payload !== undefined ? [`payload ${json(a.payload)}`] : [];
    case "gate":
      return a.comment ? [`comment: ${a.comment}`] : [];
    case "issue":
      return [...(a.status ? [`status: ${a.status}`] : []), ...(a.note ? [`note: ${a.note}`] : [])];
    case "import":
      return [
        ...(a.folder ? [`into folder ${a.folder}`] : []),
        ...(a.overwrite ? ["replaces a folder of the same name"] : []),
        ...(a.apply_settings ? ["also adds the settings it brings"] : []),
      ];
    case "restore":
      return ["writes that version back (the current one stays in the history)"];
    case "delete_pipeline":
      return ["removes it (its own folder too); pipeline history keeps a copy"];
    case "install_skills":
      return ["downloads them from GitHub; skills are instructions your agents follow"];
    case "lesson":
      return [`- ${a.text}`];
    case "report_issue":
      return ["creates an issue in the repo set under [report] (once)"];
    case "github":
      return (a.op ?? "save") === "save" && a.auto !== undefined ? [`create issues automatically: ${a.auto ? "on" : "off"}`] : [];
    case "pipeline_lesson":
      return [
        `when: ${a.cue}`,
        `lesson: ${a.lesson}`,
        ...(a.component ? [`component: ${a.component}`] : []),
        ...(a.confidence ? [`confidence: ${a.confidence}`] : []),
        "goes to the pipeline's lesson library, read by later reviews and the Chat",
      ];
    case "review":
      return ["asks the Chat's model about the whole run; counts against the run's budget"];
    case "probe":
      return ["replays the node with the edit on the run's inputs and compares; counts against the run's budget"];
    default:
      return [];
  }
}

/** Actions "Apply all" leaves for their own click: they need input (a key value, a file), act
 * on something outside (a gate, the engine, a download) or delete something. */
const OWN_CLICK = new Set([
  "key",
  "import",
  "export",
  "engine",
  "gate",
  "delete_setting",
  "claude_logout",
  "delete_key",
  "delete_skill",
  "install_skills",
  "incident",
  "restore",
  "delete_pipeline",
  "chat_app",
  "start_run",
  "run_message",
  "triage",
  "report_issue",
  "github",
  "review",
  "probe",
]);
export function needsOwnClick(a: AssistantAction): boolean {
  return OWN_CLICK.has(a.type);
}

/** What applying needs from the user on the card: a key's value, or a .zip to import. */
export function cardInput(a: AssistantAction): "secret" | "file" | null {
  return a.type === "key" ? "secret" : a.type === "import" ? "file" : null;
}

/** The API calls applying needs (a subset of `api`, so tests can pass fakes). */
export interface ApplyDeps {
  putSetting(section: SettingAction["section"], name: string, body: Record<string, unknown>): Promise<unknown>;
  deleteSetting(section: SettingAction["section"], name: string, withModels?: boolean): Promise<unknown>;
  claudeLogout(provider: string): Promise<unknown>;
  chooseModels(provider: string, models: string[]): Promise<{ note?: string }>;
  modelVisibility(name: string, hidden: boolean): Promise<unknown>;
  putSecret(name: string, value: string): Promise<unknown>;
  deleteSecret(name: string): Promise<unknown>;
  setupChoice(harness: string, model?: string): Promise<unknown>;
  setVision(choice: string): Promise<unknown>;
  putChatInstructions(text: string): Promise<unknown>;
  fetchSkills(): Promise<unknown>;
  putSkill(name: string, description: string, body: string): Promise<unknown>;
  deleteSkill(name: string): Promise<unknown>;
  installSkills(source: string, names?: string[]): Promise<{ result: { installed: string[] } }>;
  importMcp(items: { id: string; name?: string }[]): Promise<{ imported: string[]; keys_set: string[] }>;
  chatPair(platform: string): Promise<{ code?: string; link?: string | null }>;
  chatTest(platform: string): Promise<unknown>;
  chatUnlink(platform: string): Promise<unknown>;
  chatNotify(prefs: Record<string, boolean>): Promise<unknown>;
  native(action: string): Promise<{ path?: string }>;
  assistantApply(path: string, yaml: string, files: Record<string, string>): Promise<{ name: string }>;
  exportPipeline(name: string): Promise<{ filename: string }>;
  importPipeline(file: Blob, opts: { folder?: string; overwrite?: boolean; applySettings?: boolean }): Promise<{ name: string; issues: unknown[]; applied: string[] }>;
  dryRun(body: { name: string; payload?: unknown; routes?: Record<string, string[]> }): Promise<{ status: string; error: string | null; nodes: { node: string; status: string }[] }>;
  historyRestore(name: string, commit: string): Promise<unknown>;
  deletePipeline(name: string): Promise<{ deleted: string; removed: string }>;
  startRun(pipeline: string, payload: unknown, text?: string): Promise<{ run_id: string }>;
  sendRunMessage(run: string, text: string): Promise<unknown>;
  retryRun(id: string, node?: string): Promise<{ retried: number }>;
  approve(run: string, node: string, comment: string): Promise<unknown>;
  reject(run: string, node: string, comment: string): Promise<unknown>;
  updateIssue(n: number, body: { status?: string; note?: string }): Promise<unknown>;
  deleteIncident(id: string): Promise<unknown>;
  clearIncidents(): Promise<unknown>;
  setBudget(usd: number | null): Promise<unknown>;
  addLesson(text: string): Promise<unknown>;
  triageRun(run: string, node?: string): Promise<TriageRecord>;
  triageGithub(id: string): Promise<TriageRecord>;
  githubSave(body: { repo?: string; auto?: boolean }): Promise<unknown>;
  githubLogin(): Promise<{ state: string; code: string | null; url: string | null; error: string | null }>;
  githubTest(): Promise<{ ok: boolean; repo?: string; error: string | null }>;
  reviewRun(run: string): Promise<ReviewRecord>;
  reviewProbe(id: string, index: number): Promise<Probe>;
  lessonAdd(pipeline: string, body: Partial<Lesson> & { cue: string; lesson: string; node: string }): Promise<Lesson>;
  archiveConversation(id: string, archived: boolean): Promise<unknown>;
}

export interface Applied {
  /** shown on the card after applying */
  result?: string;
  /** what the rest of the UI should reload */
  changed?: "settings" | "pipelines";
  /** a pipeline written or restored: the editor reloads it */
  pipeline?: string;
  /** a run started: link to it */
  run?: string;
  /** a triage ran: the Chat shows it as its own card */
  triage?: TriageRecord;
  /** a review ran: the Chat shows it as its own card */
  review?: ReviewRecord;
  /** a conversation was archived or unarchived: History reloads */
  conversations?: boolean;
}

/** Apply one proposal through the same API the GUI page uses. `input` is what the card
 * collected: a key's value (sent to /api/secrets only, never to the model) or a file. */
export async function applyAction(a: AssistantAction, deps: ApplyDeps, input: { value?: string; file?: Blob } = {}): Promise<Applied> {
  switch (a.type) {
    case "setting":
      await deps.putSetting(a.section, a.name, a.body);
      return { changed: "settings" };
    case "delete_setting":
      await deps.deleteSetting(a.section, a.name, a.with_models === true);
      return { changed: "settings" };
    case "claude_logout":
      await deps.claudeLogout(a.provider);
      return { changed: "settings", result: "Signed out. Run `claude auth login` in Terminal to sign in again." };
    case "source_models": {
      const r = await deps.chooseModels(a.provider, a.models);
      return { changed: "settings", result: r.note };
    }
    case "model_visibility":
      await deps.modelVisibility(a.model, a.hidden);
      return { changed: "settings" };
    case "key":
      if (!input.value) throw new Error("type the key's value first");
      await deps.putSecret(a.name, input.value);
      return { changed: "settings" };
    case "delete_key":
      await deps.deleteSecret(a.name);
      return { changed: "settings" };
    case "chat_uses":
      await deps.setupChoice(a.harness, a.model ?? undefined);
      return { changed: "settings" };
    case "chat_vision":
      await deps.setVision(a.choice);
      return { changed: "settings" };
    case "chat_instructions":
      await deps.putChatInstructions(a.text);
      return { changed: "settings", result: "Used from the next message on" };
    case "fetch_skills":
      await deps.fetchSkills();
      return { changed: "settings" };
    case "skill":
      await deps.putSkill(a.name, a.description ?? "", a.body);
      return { changed: "settings" };
    case "delete_skill":
      await deps.deleteSkill(a.name);
      return { changed: "settings" };
    case "install_skills": {
      const r = await deps.installSkills(a.source, a.names);
      return { changed: "settings", result: `Installed ${r.result.installed.join(", ")}` };
    }
    case "mcp_import": {
      const r = await deps.importMcp(a.items);
      const keys = r.keys_set.length ? `; keys saved: ${r.keys_set.join(", ")}` : "";
      return { changed: "settings", result: `Imported ${r.imported.join(", ")}${keys}` };
    }
    case "chat_app":
      if (a.op === "pair") {
        const r = await deps.chatPair(a.platform);
        return { result: r.link ? `Open ${r.link} and press Start` : `Send this code to the bot: ${r.code ?? "?"}` };
      }
      if (a.op === "test") {
        await deps.chatTest(a.platform);
        return { result: "Test message sent" };
      }
      await deps.chatUnlink(a.platform);
      return {};
    case "chat_notify": {
      const prefs: Record<string, boolean> = {};
      for (const k of ["gates", "failures", "done", "questions", "reviews"] as const) if (typeof a[k] === "boolean") prefs[k] = a[k] as boolean;
      await deps.chatNotify(prefs);
      return {};
    }
    case "engine": {
      const r = await deps.native(a.op === "restart" ? "engine.restart" : "folder.choose");
      return a.op === "restart" ? { result: "Restarting the engine" } : { result: r.path ? `Takes effect when the engine restarts: ${r.path}` : "No folder chosen" };
    }
    case "pipeline": {
      const r = await deps.assistantApply(a.path, a.yaml, a.files ?? {});
      return { changed: "pipelines", pipeline: r.name };
    }
    case "export": {
      const r = await deps.exportPipeline(a.pipeline);
      return { result: `Saved ${r.filename}` };
    }
    case "import": {
      if (!input.file) throw new Error("choose the .zip to import first");
      const r = await deps.importPipeline(input.file, { folder: a.folder, overwrite: a.overwrite, applySettings: a.apply_settings });
      const applied = r.applied.length ? `; settings added: ${r.applied.join(", ")}` : "";
      return { changed: "pipelines", pipeline: r.name, result: `Imported ${r.name}${r.issues.length ? ` (${r.issues.length} issues to fix)` : ""}${applied}` };
    }
    case "dry_run": {
      const r = await deps.dryRun({ name: a.pipeline, payload: a.payload, routes: a.routes });
      return { result: dryRunSummary(r) };
    }
    case "restore":
      await deps.historyRestore(a.pipeline, a.commit);
      return { changed: "pipelines", pipeline: a.pipeline };
    case "delete_pipeline": {
      const r = await deps.deletePipeline(a.pipeline);
      return { changed: "pipelines", result: `Deleted ${r.removed}` };
    }
    case "start_run": {
      const r =
        a.text !== undefined
          ? await deps.startRun(a.pipeline, a.payload ?? {}, a.text)
          : await deps.startRun(a.pipeline, a.payload ?? {});
      return { run: r.run_id, result: `Started run ${r.run_id}` };
    }
    case "run_message":
      await deps.sendRunMessage(a.run, a.text);
      return { run: a.run, result: "Sent" };
    case "retry": {
      const r = await deps.retryRun(a.run, a.node);
      return { run: a.run, result: `Retried ${r.retried} step${r.retried === 1 ? "" : "s"}` };
    }
    case "gate":
      await (a.decision === "approve" ? deps.approve : deps.reject)(a.run, a.node, a.comment ?? "");
      return { run: a.run };
    case "issue":
      await deps.updateIssue(a.number, { ...(a.status ? { status: a.status } : {}), ...(a.note ? { note: a.note } : {}) });
      return {};
    case "incident":
      if (a.op === "clear") await deps.clearIncidents();
      else await deps.deleteIncident(a.id ?? "");
      return {};
    case "budget":
      await deps.setBudget(a.monthly_usd);
      return {};
    case "triage": {
      const r = await deps.triageRun(a.run, a.node);
      const what = r.verdict ? `${VERDICT_LABEL[r.verdict]}: ${r.summary}` : `${r.status}${r.error ? `: ${r.error}` : ""}`;
      return { run: a.run, triage: r, result: what };
    }
    case "lesson":
      await deps.addLesson(a.text);
      return { changed: "settings", result: "Added to the Chat instructions" };
    case "report_issue": {
      const r = await deps.triageGithub(a.triage);
      return { result: r.github?.url ? `Reported: ${r.github.url}` : "Reported" };
    }
    case "github": {
      if (a.op === "login") {
        const r = await deps.githubLogin();
        if (r.error) throw new Error(r.error);
        return { result: r.code ? `Enter code ${r.code} at ${r.url ?? "https://github.com/login/device"} (Settings > General shows when it's done)` : r.state };
      }
      if (a.op === "test") {
        const r = await deps.githubTest();
        if (!r.ok) throw new Error(r.error ?? "the test failed");
        return { result: `Can open issues in ${r.repo}` };
      }
      await deps.githubSave({ ...(a.repo ? { repo: a.repo } : {}), ...(a.auto !== undefined ? { auto: a.auto } : {}) });
      return { changed: "settings", result: a.repo ? `Reports go to ${a.repo}` : "Saved" };
    }
    case "review": {
      const r = await deps.reviewRun(a.run);
      return { run: a.run, review: r, result: r.status === "done" ? r.summary || "Reviewed" : `${r.status}${r.error ? `: ${r.error}` : ""}` };
    }
    case "probe": {
      const p = await deps.reviewProbe(a.review, a.index);
      const what = p.status === "done" ? `${p.verdict ?? "same"} than the original (1 run)` : `${p.status}${p.error ? `: ${p.error}` : ""}`;
      return { result: `Probe: ${what}` };
    }
    case "pipeline_lesson": {
      const { type: _type, pipeline, issues: _issues, why: _why, ...body } = a;
      const x = await deps.lessonAdd(pipeline, body);
      return { changed: "pipelines", result: `Lesson L${x.id} is in the pipeline's lesson library` };
    }
    case "conversation":
      await deps.archiveConversation(a.id, a.op === "archive");
      return { conversations: true, result: a.op === "archive" ? "Archived (History > Archived)" : "Back in History" };
  }
}

/** "done: 5 nodes (plan done, deploy skipped)" */
export function dryRunSummary(r: { status: string; error: string | null; nodes: { node: string; status: string }[] }): string {
  const odd = r.nodes.filter((n) => n.status !== "done").map((n) => `${n.node} ${n.status}`);
  return `${r.status}: ${r.nodes.length} node${r.nodes.length === 1 ? "" : "s"}${odd.length ? ` (${odd.join(", ")})` : ""}${r.error ? `; ${r.error}` : ""}`;
}

/** One line per table entry, e.g. `provider = openrouter`. */
export function settingLines(body: Record<string, unknown>): string[] {
  return Object.entries(body)
    .filter(([, v]) => v !== null && v !== undefined && v !== "")
    .map(([k, v]) => `${k} = ${typeof v === "string" ? v : JSON.stringify(v)}`);
}

/** The pipeline's editor name: `dev-cycle/pipeline.yaml` -> `dev-cycle/pipeline`. */
export function pipelineName(path: string): string {
  return path.replace(/\.yaml$/, "");
}

/** What goes to the server: text only, with what the user did about each proposal, so the
 * Chat knows what's already applied. */
export function toHistory(
  messages: ChatMessage[],
): { role: string; content: string; attachments?: string[] }[] {
  return messages.map((m) => {
    if (m.role === "user" || !m.reply)
      // keep attached images: without their ids the Chat never hears of them
      return { role: m.role, content: m.content, ...(m.attachments?.length ? { attachments: m.attachments } : {}) };
    const done = m.reply.actions.map((a, i) => {
      const st = m.states?.[i]?.state ?? "pending";
      const label = actionTitle(a);
      return st === "applied"
        ? `${label}: applied`
        : st === "failed"
          ? `${label}: failed (${m.states?.[i]?.error ?? "?"})`
          : `${label}: not applied yet`;
    });
    return {
      role: "assistant",
      content: done.length
        ? `${m.reply.message}\n[proposals: ${done.join("; ")}]`
        : m.reply.message,
    };
  });
}

/** Actions "Apply all" can do without asking (not the ones that need their own click;
 * invalid ones wait). */
export function autoApplicable(m: ChatMessage): number[] {
  return (m.reply?.actions ?? [])
    .map((a, i) => [a, i] as const)
    .filter(([a, i]) => !needsOwnClick(a) && !a.issues.length && m.states?.[i]?.state !== "applied")
    .map(([, i]) => i);
}

/* ------------------------------------------------------- editor <-> assistant */

export interface OpenPipeline {
  name: string;
  source: string;
  dirty: boolean;
}

type Listener = () => void;

/** The pipeline open in the editor (published by Editor), and "this pipeline changed on disk"
 * notices (sent by the assistant after it writes one). */
class EditorLink {
  private open: OpenPipeline | null = null;
  private changed = new Set<(name: string) => void>();
  private listeners = new Set<Listener>();

  current(): OpenPipeline | null {
    return this.open;
  }

  publish(p: OpenPipeline | null): void {
    this.open = p;
    for (const fn of [...this.listeners]) fn();
  }

  subscribe = (fn: Listener): (() => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };

  onChanged(fn: (name: string) => void): () => void {
    this.changed.add(fn);
    return () => this.changed.delete(fn);
  }

  notifyChanged(name: string): void {
    for (const fn of [...this.changed]) fn(name);
  }
}

export const editorLink = new EditorLink();

/** Messages other parts of the UI hand to the assistant: text to send ("Fix this"), or a
 * ready reply to show as a card without asking the model (a triage, "Open in Chat"). */
class AssistantInbox {
  private queue: (string | ChatReply)[] = [];
  private listeners = new Set<() => void>();

  push(item: string | ChatReply): void {
    this.queue.push(item);
    for (const fn of [...this.listeners]) fn();
  }

  take(): string | ChatReply | undefined {
    return this.queue.shift();
  }

  subscribe(fn: () => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }
}

export const assistantInbox = new AssistantInbox();

/* ------------------------------------------------------------------ storage */

const STORE_KEY = "agentline.assistant.chat";
const HARNESS_KEY = "agentline.assistant.harness";
const MODEL_KEY = "agentline.assistant.model";

/** The chat kept in the browser before phase 48 (moved into a server conversation once). */
export function loadChat(storage: () => Storage | null): ChatMessage[] {
  try {
    const v = storage()?.getItem(STORE_KEY);
    const parsed = v ? (JSON.parse(v) as unknown) : [];
    return Array.isArray(parsed) ? (parsed as ChatMessage[]) : [];
  } catch {
    return [];
  }
}

/** Drop the chat kept in the browser before phase 48 (it moved to the server). */
export function clearOldChat(storage: () => Storage | null): void {
  try {
    storage()?.removeItem(STORE_KEY);
  } catch {
    /* ignore */
  }
}

/** The messages as they are kept (PUT /assistant/conversations/{id}): applying/in-flight
 * states don't survive a reload. */
export function forStorage(messages: ChatMessage[]): ChatMessage[] {
  return messages.map((m) =>
    m.states
      ? {
          ...m,
          states: Object.fromEntries(
            Object.entries(m.states).map(([k, v]) => [
              k,
              v.state === "applying" ? { state: "pending" as const } : v,
            ]),
          ),
        }
      : m,
  );
}

/** "" = the engine's default (the first-run setup choice, else claude-code). */
export function loadHarness(storage: () => Storage | null): string {
  try {
    const s = storage();
    const old = s?.getItem("agentline.assistant.agent"); // its key before phase 35
    return s?.getItem(HARNESS_KEY) ?? (old === "llm" ? "builtin" : old) ?? "";
  } catch {
    return "";
  }
}

/** The model picked in the Chat panel; "" = the harness's default. */
export function loadChatModel(storage: () => Storage | null): string {
  try {
    return storage()?.getItem(MODEL_KEY) ?? "";
  } catch {
    return "";
  }
}

export function saveChatModel(storage: () => Storage | null, model: string): void {
  try {
    storage()?.setItem(MODEL_KEY, model);
  } catch {
    /* ignore */
  }
}

export function saveHarness(storage: () => Storage | null, harness: string): void {
  try {
    storage()?.setItem(HARNESS_KEY, harness);
  } catch {
    /* ignore */
  }
}

/* ------------------------------------------------------- Chat instructions */

/** Skills the instructions name in backticks (`interview-me`), like the engine's
 * chat_instructions.skill_names: shown under the editor so a typo is easy to spot. */
export function instructionSkills(text: string): string[] {
  return [...new Set([...text.matchAll(/`([a-z0-9][a-z0-9_.-]*)`/g)].map((m) => m[1]))];
}

/** The editor's buttons: Save when changed, Reset when not already the defaults. */
export function instructionsState(draft: string, saved: string, defaults: string): { dirty: boolean; canReset: boolean } {
  return { dirty: draft !== saved, canReset: saved !== defaults || draft !== saved };
}

/** The line under an image transcript card: who read it, and whether it was reused. */
export function transcriptByline(t: { by: string; edited: boolean; cached?: boolean }): string {
  if (t.edited) return "corrected by you";
  return `read by ${t.by}${t.cached ? " (saved from an earlier turn)" : ""}`;
}
