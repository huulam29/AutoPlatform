// Thin fetch wrapper. The bearer token (when the server has AGENTLINE_API_TOKEN set) is kept in
// localStorage and asked for on the first 401.

import type { ChatMessage, ChatReply } from "./lib/assistant";
import type { ConversationSummary } from "./lib/conversations";
import type { GithubLogin, GithubSetup, GithubTest } from "./lib/github";
import type { GithubStatus, TriageRecord } from "./lib/triage";
import type { Adoption, Lesson, LessonStatus, Probe, ReviewConfig, ReviewRecord } from "./lib/review";
import type {
  ClaudeLogin,
  DetectedMcp,
  ArtifactValue,
  DetectResult,
  Issue,
  JournalEntry,
  KeyStatus,
  Model,
  NodeTypes,
  PipelineSummary,
  ProviderTest,
  SetupStatus,
  Run,
  RunCard,
  RunDetail,
  RunMessage,
  StartInfo,
  Settings,
  SettingsSection,
} from "./types";

const TOKEN_KEY = "agentline.token";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public body: unknown,
  ) {
    super(message);
  }
}

function token(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(value: string): void {
  try {
    localStorage.setItem(TOKEN_KEY, value);
  } catch {
    /* private mode: the token lasts for this request only */
  }
}

/** A pipeline file name ("folder/pipeline") as URL path segments. */
function pathName(name: string): string {
  return name.split("/").map(encodeURIComponent).join("/");
}

function query(q: Record<string, string | undefined>): string {
  const parts = Object.entries(q).filter(([, v]) => v) as [string, string][];
  return parts.length ? `?${new URLSearchParams(parts).toString()}` : "";
}

async function call<T>(
  method: string,
  path: string,
  body?: unknown,
  retry = true,
  signal?: AbortSignal,
): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const t = token();
  if (t) headers["Authorization"] = `Bearer ${t}`;
  const res = await fetch(`/api${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  });
  if (res.status === 401 && retry) {
    const entered = window.prompt("API token (AGENTLINE_API_TOKEN):");
    if (entered) {
      setToken(entered);
      return call<T>(method, path, body, false, signal);
    }
  }
  const text = await res.text();
  const data = text ? (JSON.parse(text) as unknown) : null;
  if (!res.ok) {
    const detail = (data as { detail?: unknown } | null)?.detail;
    throw new ApiError(res.status, typeof detail === "string" ? detail : res.statusText, data);
  }
  return data as T;
}

/** For binary bodies and downloads (export / import), with the same token handling. */
async function raw(method: string, path: string, body?: Blob): Promise<Response> {
  const headers: Record<string, string> = {};
  const t = token();
  if (t) headers["Authorization"] = `Bearer ${t}`;
  if (body) headers["Content-Type"] = "application/zip";
  const res = await fetch(`/api${path}`, { method, headers, body });
  if (!res.ok) {
    const text = await res.text();
    let detail = res.statusText;
    try {
      const d = (JSON.parse(text) as { detail?: unknown }).detail;
      if (typeof d === "string") detail = d;
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, detail, text);
  }
  return res;
}

export interface McpTest {
  ok: boolean;
  error?: string;
  tools?: { name: string; description?: string }[];
  note?: string;
}

export interface ChatPlatform {
  title: string;
  keys: { name: string; set: boolean }[];
  token_env: string;
  token_set: boolean;
  bot: string | null;
  linked: { chat_id: string | number; user_id?: string | number; username?: string; linked_at?: number } | null;
  pairing: { code: string; expires: number } | null;
  setup: string;
}

export interface ChatStatus {
  platforms: string[];
  telegram: ChatPlatform;
  slack?: ChatPlatform;
  discord?: ChatPlatform;
  notify: { gates: boolean; failures: boolean; done: boolean; questions: boolean; reviews: boolean };
  platform?: string;
  code?: string;
  link?: string | null;
}

export interface ChatInstructions {
  text: string;
  default: string;
  is_default: boolean;
  path: string;
}

export interface DryRunResult {
  status: string;
  error: string | null;
  nodes: { node: string; status: string; runs: number; error: string | null }[];
  routes: { router: string; label: string; iteration: number }[];
  calls: { kind: string; node: string; what: string }[];
  journal: { node: string | null; iteration: number; task: number | null; kind: string; message: string }[];
  not_loaded?: { skills: Record<string, string[]>; mcp: Record<string, string[]> };
}

export interface Approval {
  run_id: string;
  pipeline: string;
  node_id: string;
  execution_id: string;
  prompt: string;
  approvers: string[];
  waiting_since: number | null;
}

export interface IncidentSummary {
  id: string;
  source: string;
  kind: string;
  title: string;
  message?: string;
  count: number;
  first_seen: number;
  last_seen: number;
  status: string;
  version?: string;
  platform?: string;
  exception_type?: string | null;
  attachments?: string[];
}

export interface Incident extends IncidentSummary {
  traceback?: string;
  context?: Record<string, unknown>;
}

export interface IssueRecord {
  number: number;
  title: string;
  status: string;
  source: { kind: string; ref: string };
  detail?: string;
  created_at: number;
  updated_at: number;
  occurrences?: number;
  notes: { at: number; text: string }[];
  history: { at: number; from: string | null; to: string; note: string | null }[];
  /** the failure triage's verdict (agentline.triage) */
  triage?: { id: string; verdict: string; summary: string; github?: { url?: string; number?: number } | null };
}

export interface CostReport {
  days: number;
  total_usd: number;
  month_usd: number;
  month: string;
  budget_usd: number | null;
  by_day: { day: string; usd: number }[];
  by_pipeline: { pipeline: string; usd: number; runs: number }[];
  by_model: { model: string; usd: number }[];
  top_nodes: { node: string; usd: number }[];
}

export interface ImportResult {
  name: string;
  pipeline: string | null;
  issues: Issue[];
  missing: Record<string, string[]>;
  applied: string[];
  note?: string | null;
}

const enc = (name: string) => name.split("/").map(encodeURIComponent).join("/");

export const api = {
  pipelines: () => call<{ dir: string; pipelines: PipelineSummary[] }>("GET", "/pipelines"),
  pipeline: (name: string) =>
    call<{ name: string; path: string; source: string; model: Model }>(
      "GET",
      `/pipelines/${enc(name)}`,
    ),
  saveSource: (name: string, source: string) =>
    call<{ source: string; model: Model; issues: Issue[] }>(
      "PUT",
      `/pipelines/${enc(name)}/source`,
      { source },
    ),
  saveModel: (name: string, model: unknown, base_source: string) =>
    call<{ source: string; model: Model; warnings: string[]; issues: Issue[] }>(
      "PUT",
      `/pipelines/${enc(name)}/model`,
      { model, base_source },
    ),
  /** Validate `source`, or (with `model`) a dry-run apply of `model` onto `source`. */
  validate: (source: string, name?: string, model?: unknown) =>
    call<{ model: Model; issues: Issue[]; source: string; warnings: string[] }>(
      "POST",
      "/validate",
      { source, name, model },
    ),
  nodeTypes: () => call<NodeTypes>("GET", "/node-types"),
  runs: (status = "", limit = 50) =>
    call<{ runs: Run[] }>("GET", `/runs?status=${encodeURIComponent(status)}&limit=${limit}`),
  run: (id: string) => call<RunDetail>("GET", `/runs/${encodeURIComponent(id)}`),
  journal: (id: string, node?: string) =>
    call<{ journal: JournalEntry[] }>(
      "GET",
      `/runs/${encodeURIComponent(id)}/journal${node ? `?node=${encodeURIComponent(node)}` : ""}`,
    ),
  runFix: (id: string) =>
    call<{ prompt: string; pipeline: string | null }>("GET", `/runs/${encodeURIComponent(id)}/fix`),
  issueFix: (n: number) => call<{ prompt: string; pipeline: string | null }>("GET", `/issues/${n}/fix`),
  retryRun: (id: string, node?: string) =>
    call<{ retried: number }>("POST", `/runs/${encodeURIComponent(id)}/retry`, { node }),
  artifact: (hash: string) => call<ArtifactValue>("GET", `/artifacts/${hash}`),

  // Settings: harnesses, model sources (providers), models, keys. See api/settings.py.
  settings: () => call<Settings>("GET", "/settings"),
  putSetting: (section: SettingsSection, name: string, body: Record<string, unknown>) =>
    call<Settings>("PUT", `/settings/${section}/${encodeURIComponent(name)}`, body),
  /** `withModels` (model sources): also delete the models the source serves. */
  deleteSetting: (section: SettingsSection, name: string, withModels = false) =>
    call<Settings & { removed_models?: string[] }>(
      "DELETE",
      `/settings/${section}/${encodeURIComponent(name)}${withModels ? "?with_models=true" : ""}`,
    ),
  /** Write-only: the value goes to the server's secrets file and is never returned. */
  putSecret: (name: string, value: string) =>
    call<{ keys: KeyStatus[]; note?: string }>("PUT", `/secrets/${encodeURIComponent(name)}`, {
      value,
    }),
  deleteSecret: (name: string) =>
    call<{ keys: KeyStatus[] }>("DELETE", `/secrets/${encodeURIComponent(name)}`),
  testProvider: (name: string) =>
    call<ProviderTest>("POST", `/providers/${encodeURIComponent(name)}/test`),
  claudeLogin: (provider: string) =>
    call<ClaudeLogin>("GET", `/providers/${encodeURIComponent(provider)}/login`),
  /** Signs Claude Code out on this machine (`claude auth logout`). */
  claudeLogout: (provider: string) =>
    call<ClaudeLogin>("POST", `/providers/${encodeURIComponent(provider)}/logout`),
  /** Hide a model from every picker, or show it again (Settings > Models, the eye). */
  modelVisibility: (name: string, hidden: boolean) =>
    call<Settings & { note?: string }>("PUT", `/settings/models/${encodeURIComponent(name)}/visibility`, { hidden }),
  /** The source's chosen models: adds the missing ids, removes the unchecked ones (with
   * `addOnly`, removes nothing: the source's model checklist). */
  chooseModels: (provider: string, models: string[], addOnly = false) =>
    call<Settings & { added: string[]; removed: string[]; note?: string }>(
      "PUT",
      `/providers/${encodeURIComponent(provider)}/models`,
      addOnly ? { models, add_only: true } : { models },
    ),
  detectHarness: (name: string) =>
    call<DetectResult>("POST", `/harnesses/${encodeURIComponent(name)}/detect`),

  // Editor assistant (api/assistant.py): proposes; the panel applies what the user accepts.
  assistantChat: (
    body: {
      harness?: string;
      model?: string;
      messages: { role: string; content: string; attachments?: string[] }[];
      pipeline?: string;
      source?: string;
    },
    signal?: AbortSignal,
  ) => call<ChatReply>("POST", "/assistant/chat", body, true, signal),
  /** An image for the assistant; send the returned id in a message's `attachments`. */
  assistantAttach: async (file: Blob): Promise<{ id: string; path: string }> => {
    const headers: Record<string, string> = { "Content-Type": file.type || "application/octet-stream" };
    const t = token();
    if (t) headers["Authorization"] = `Bearer ${t}`;
    const res = await fetch("/api/assistant/attachments", { method: "POST", headers, body: file });
    const data = (await res.json()) as { id: string; path: string; detail?: string };
    if (!res.ok) throw new ApiError(res.status, data.detail ?? res.statusText, data);
    return data;
  },
  attachmentUrl: (id: string) => `/api/assistant/attachments/${encodeURIComponent(id)}`,
  /** The Chat's past conversations (agentline.conversations), newest first; `q` searches
   * titles and messages, archived ones included. */
  conversations: (q = "") =>
    call<{ conversations: ConversationSummary[]; archive_days: number }>(
      "GET",
      `/assistant/conversations${q.trim() ? `?q=${encodeURIComponent(q.trim())}` : ""}`,
    ),
  conversation: (id: string) =>
    call<Omit<ConversationSummary, "messages"> & { messages: ChatMessage[] }>("GET", `/assistant/conversations/${encodeURIComponent(id)}`),
  /** Create or replace a conversation's messages (the id is the panel's). */
  saveConversation: (id: string, messages: ChatMessage[]) =>
    call<ConversationSummary>("PUT", `/assistant/conversations/${encodeURIComponent(id)}`, { messages }),
  archiveConversation: (id: string, archived: boolean) =>
    call<ConversationSummary>(
      "POST",
      `/assistant/conversations/${encodeURIComponent(id)}/${archived ? "archive" : "unarchive"}`,
    ),
  assistantApply: (path: string, yaml: string, files: Record<string, string>) =>
    call<{ name: string; issues: string[] }>("POST", "/assistant/apply", { path, yaml, files }),
  skills: () =>
    call<{
      fetched: boolean;
      repo: string;
      skills: { name: string; description: string; source?: string; installed_from?: InstalledFrom }[];
    }>(
      "GET",
      "/skills",
    ),
  /** Delete a pipeline (its own folder goes with it; history keeps it). */
  deletePipeline: (name: string) =>
    call<{ deleted: string; removed: string }>("DELETE", `/pipelines/${enc(name)}`),
  /** The pipeline and its files as a .zip (agentline/bundle.py). */
  exportPipeline: async (name: string): Promise<{ blob: Blob; filename: string }> => {
    const res = await raw("GET", `/export/${enc(name)}`);
    const m = /filename="([^"]+)"/.exec(res.headers.get("content-disposition") ?? "");
    return { blob: await res.blob(), filename: m?.[1] ?? "pipeline.agentline.zip" };
  },
  importPipeline: async (
    file: Blob,
    opts: { folder?: string; overwrite?: boolean; applySettings?: boolean },
  ): Promise<ImportResult> => {
    const q = new URLSearchParams();
    if (opts.folder) q.set("folder", opts.folder);
    if (opts.overwrite) q.set("overwrite", "true");
    if (opts.applySettings) q.set("apply_settings", "true");
    const res = await raw("POST", `/import?${q.toString()}`, file);
    return (await res.json()) as ImportResult;
  },
  testMcp: (name: string) => call<McpTest>("POST", `/mcp/${encodeURIComponent(name)}/test`),
  /** Test a server that isn't saved; `secrets` are typed key values, used for this test only. */
  testMcpDraft: (name: string, body: Record<string, unknown>, secrets: Record<string, string>) =>
    call<McpTest>("POST", "/mcp/test", { name, body, secrets }),
  detectMcp: () => call<{ found: DetectedMcp[] }>("GET", "/mcp/detect"),
  importMcp: (items: { id: string; name?: string }[]) =>
    call<Settings & { imported: string[]; keys_set: string[] }>("POST", "/mcp/import", { items }),
  chat: () => call<ChatStatus>("GET", "/chat"),
  chatPair: (platform = "telegram") => call<ChatStatus>("POST", `/chat/${platform}/pair`),
  chatTest: async (platform = "telegram") => {
    await call<{ sent: boolean }>("POST", `/chat/${platform}/test`);
  },
  chatUnlink: (platform = "telegram") => call<ChatStatus>("DELETE", `/chat/${platform}`),
  /** What every Chat turn is told (agentline.chat_instructions); DELETE = the defaults. */
  chatInstructions: () => call<ChatInstructions>("GET", "/chat-instructions"),
  putChatInstructions: (text: string) => call<ChatInstructions>("PUT", "/chat-instructions", { text }),
  resetChatInstructions: () => call<ChatInstructions>("DELETE", "/chat-instructions"),
  /** Append one lesson line (failure triage) to the Chat instructions. */
  addLesson: (text: string) => call<ChatInstructions>("POST", "/chat-instructions/lesson", { text }),
  // Failure triage (api/triage.py)
  triageList: (run?: string) =>
    call<{ records: TriageRecord[]; github: GithubStatus }>(
      "GET",
      `/triage${run ? `?run=${encodeURIComponent(run)}` : ""}`,
    ),
  triageRun: (run: string, node?: string) => call<TriageRecord>("POST", "/triage", { run, node }),
  triageGithub: (id: string) => call<TriageRecord>("POST", `/triage/${encodeURIComponent(id)}/github`),
  // Run reviews, probes and lesson libraries (api/reviews.py)
  reviewList: (q: { run?: string; flow?: string } = {}) =>
    call<{ records: ReviewRecord[]; config: ReviewConfig }>("GET", `/reviews${query(q)}`),
  reviewRun: (run: string) => call<ReviewRecord>("POST", "/reviews", { run }),
  reviewProbe: (id: string, index: number) => call<Probe>("POST", `/reviews/${encodeURIComponent(id)}/probe`, { index }),
  /** apply a review's pipeline edit on the server (it keeps the files as they were for an undo) */
  reviewApply: (id: string, index: number) => call<Adoption>("POST", `/reviews/${encodeURIComponent(id)}/apply`, { index }),
  /** every pipeline with lessons or adopted edits (the Lessons page) */
  allLessons: () =>
    call<{ pipelines: { pipeline: string; flow: string; file: string; lessons: Lesson[]; adoptions: Adoption[] }[] }>("GET", "/lessons"),
  lessons: (pipeline: string) =>
    call<{ pipeline: string; flow: string; file: string; lessons: Lesson[]; adoptions: Adoption[] }>("GET", `/lessons/${pathName(pipeline)}`),
  lessonAdd: (pipeline: string, body: Partial<Lesson> & { cue: string; lesson: string; node: string }) =>
    call<Lesson>("POST", `/lessons/${pathName(pipeline)}`, { op: "add", ...body }),
  lessonStatus: (pipeline: string, id: number, status: LessonStatus, note = "") =>
    call<Lesson>("POST", `/lessons/${pathName(pipeline)}`, { op: "status", id, status, note }),
  /** agentline's own problems (crash reports, infrastructure verdicts): not on Problems. */
  appReports: () => call<{ reports: TriageRecord[]; github: GithubStatus }>("GET", "/app-reports"),
  deleteAppReport: (id: string) => call<{ deleted: string }>("DELETE", `/app-reports/${encodeURIComponent(id)}`),
  // GitHub for reports (api/github.py): never returns a token
  github: () => call<GithubSetup>("GET", "/github"),
  githubSave: (body: { repo?: string; auto?: boolean }) => call<GithubSetup>("PUT", "/github", body),
  githubLogin: () => call<GithubLogin>("POST", "/github/login"),
  githubLoginState: () => call<GithubLogin>("GET", "/github/login"),
  githubLoginCancel: () => call<GithubLogin>("DELETE", "/github/login"),
  githubTest: () => call<GithubTest>("POST", "/github/test"),
  chatNotify: (prefs: Partial<ChatStatus["notify"]>) =>
    call<ChatStatus>("PUT", "/chat/notify", prefs),
  dryRun: (body: { name: string; source?: string; payload?: unknown; routes?: Record<string, string[]> }) =>
    call<DryRunResult>("POST", "/dryrun", body),
  history: (name: string) =>
    call<{ available: boolean; commits: { commit: string; at: number; message: string }[] }>(
      "GET",
      `/history?name=${encodeURIComponent(name)}`,
    ),
  historyShow: (name: string, commit: string) =>
    call<{ source: string }>(
      "GET",
      `/history/show?name=${encodeURIComponent(name)}&commit=${encodeURIComponent(commit)}`,
    ),
  historyRestore: (name: string, commit: string) =>
    call<{ source: string }>("POST", "/history/restore", { name, commit }),
  costs: (days = 30) => call<CostReport>("GET", `/costs?days=${days}`),
  setBudget: (monthly_usd: number | null) =>
    call<{ budget_usd: number | null }>("PUT", "/budget", { monthly_usd }),
  approvals: () => call<Approval[]>("GET", "/approvals"),
  approve: (runId: string, node: string, comment: string) =>
    call<unknown>("POST", `/runs/${encodeURIComponent(runId)}/approve`, { node, comment }),
  reject: (runId: string, node: string, comment: string) =>
    call<unknown>("POST", `/runs/${encodeURIComponent(runId)}/reject`, { node, comment }),
  startRun: (pipeline: string, payload: unknown, text?: string) =>
    call<{ run_id: string; created: boolean }>(
      "POST",
      "/runs",
      text === undefined ? { pipeline, payload } : { pipeline, text },
    ),
  startInfo: (pipeline: string) =>
    call<StartInfo>("GET", `/runs/start-info?pipeline=${encodeURIComponent(pipeline)}`),
  runMessages: (id: string, after = 0) =>
    call<{ messages: RunMessage[]; thinking: boolean }>(
      "GET",
      `/runs/${encodeURIComponent(id)}/messages?after=${after}`,
    ),
  sendRunMessage: (id: string, text: string) =>
    call<{ id: number }>("POST", `/runs/${encodeURIComponent(id)}/messages`, { text }),
  applyRunCard: (id: string, message: number, index: number, dismiss = false) =>
    call<{ card: RunCard }>(
      "POST",
      `/runs/${encodeURIComponent(id)}/messages/${message}/apply`,
      { index, dismiss },
    ),
  logs: (limit = 500) => call<{ lines: string[] }>("GET", `/logs?limit=${limit}`),
  status: () =>
    call<{ version: string; pipelines: string[]; jira_poll?: Record<string, unknown>; restart_needed?: string[] }>(
      "GET",
      "/status",
    ),
  incidents: () => call<{ incidents: IncidentSummary[]; dir: string; new: number }>("GET", "/incidents"),
  incident: (id: string) => call<Incident>("GET", `/incidents/${encodeURIComponent(id)}`),
  incidentSeen: (id: string) => call<unknown>("POST", `/incidents/${encodeURIComponent(id)}/seen`),
  deleteIncident: (id: string) => call<unknown>("DELETE", `/incidents/${encodeURIComponent(id)}`),
  clearIncidents: () => call<unknown>("DELETE", "/incidents"),
  issues: () =>
    call<{ issues: IssueRecord[]; counts: Record<string, number> }>("GET", "/issues"),
  updateIssue: (n: number, body: { status?: string; note?: string }) =>
    call<IssueRecord>("PATCH", `/issues/${n}`, body),
  setup: () => call<SetupStatus>("GET", "/setup"),
  setupChoice: (harness: string, model?: string) =>
    call<SetupStatus>("PUT", "/setup/choice", { harness, model }),
  setupDismiss: () => call<SetupStatus>("POST", "/setup/dismiss"),
  /** Who reads images for Chat harnesses that can't see them (agentline/vision.py). */
  vision: () => call<VisionStatus>("GET", "/setup/vision"),
  setVision: (choice: string) => call<VisionStatus>("PUT", "/setup/vision", { choice }),
  putTranscript: (id: string, text: string) =>
    call<ImageTranscript>("PUT", `/assistant/transcripts/${encodeURIComponent(id)}`, { text }),
  redoTranscript: (id: string) =>
    call<ImageTranscript>("POST", `/assistant/transcripts/${encodeURIComponent(id)}/redo`),
  skill: (name: string) =>
    call<{ name: string; description: string; source: string; body: string }>(
      "GET",
      `/skills/${encodeURIComponent(name)}`,
    ),
  putSkill: (name: string, description: string, body: string) =>
    call<unknown>("PUT", `/skills/${encodeURIComponent(name)}`, { description, body }),
  deleteSkill: (name: string) => call<unknown>("DELETE", `/skills/${encodeURIComponent(name)}`),
  fetchSkills: () => call<{ fetched: boolean; skills: unknown[] }>("POST", "/skills/fetch"),
  /** The skills.sh marketplace (agentline/skill_sources.py). */
  searchSkills: (q: string) => call<{ query: string; skills: MarketSkill[] }>("GET", `/skills/search?q=${encodeURIComponent(q)}`),
  /** The skills a GitHub source has (owner/repo, owner/repo@skill, a URL, `npx skills add ...`). */
  previewSkills: (source: string) => call<SkillPreview>("POST", "/skills/preview", { source }),
  installSkills: (source: string, names?: string[]) =>
    call<{ result: { source: string; installed: string[] } }>("POST", "/skills/install", { source, names }),
  updateSkills: (names?: string[]) =>
    call<{ result: { updated: string[]; missing: string[] } }>("POST", "/skills/update", { names }),
};

export interface VisionStatus {
  choice: string;
  effective: string | null;
  options: { id: string; label: string; available: boolean; note?: string | null }[];
}

/** What a vision model read in an attached image (agentline/vision.py). */
export interface ImageTranscript {
  id: string;
  text: string;
  by: string;
  edited: boolean;
  cached?: boolean;
}

/** Where an installed skill came from (`<skills>/installed.json`). */
export type InstalledFrom = { source: string; repo: string; ref: string; path: string; sha: string; installed_at: number };
export type MarketSkill = {
  id: string;
  name: string;
  skill: string;
  source: string;
  installs: number;
  installed: boolean;
  exists: string | null;
};
export type SkillPreview = {
  source: string;
  repo: string;
  url: string;
  ref: string;
  sha: string;
  skills: { name: string; description: string; path: string; exists: string | null; installed_from: string | null }[];
  skipped: { path: string; reason: string }[];
};
