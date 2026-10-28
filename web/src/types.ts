// Shapes of the /api responses (see src/agentline/api/*.py and pipeline/roundtrip.py).

export type NodeType = "agent" | "action" | "router" | "gate" | "wait" | "chat";
export const NODE_TYPES: NodeType[] = ["agent", "action", "router", "gate", "wait", "chat"];

/** `start:` — the node that takes a run's input, and how (src/agentline/pipeline/start.py). */
export interface StartPoint {
  node?: string;
  input?: "json" | "text";
  template?: Record<string, unknown>;
}

export type StartInput = "json" | "text" | "chat";

/** GET /runs/start-info: what the Start run popup needs. */
export interface StartInfo {
  node: string | null;
  input: StartInput;
  template: Record<string, unknown> | null;
  fields: string[];
}

/** A proposed run action on a gatekeeper message; it runs only after a Confirm. */
export interface RunCard {
  type: "approve" | "reject" | "retry" | "cancel";
  node?: string;
  comment?: string;
  state: "proposed" | "done" | "dismissed" | "failed";
  deploy?: boolean; // approving lets a deploy-class action run: confirm twice
  error?: string;
}

/** One message of a run's conversation with its chat start node (id 0: the first message). */
export interface RunMessage {
  id: number;
  run_id: string;
  at: number;
  role: "user" | "agent" | "system";
  text: string;
  via: string | null;
  detail: { actions?: RunCard[]; kind?: string; error?: boolean; [k: string]: unknown };
}

export interface ModelNode {
  id: string;
  type: string | null;
  fields: Record<string, unknown>;
  prev_id?: string; // set by the UI on rename so the server keeps the node's position
}

export interface ModelEdge {
  src: string;
  dst: string;
  when?: string | null;
  max_loops?: number | null;
  on_exhausted?: string | null;
  chain_index?: number;
}

export interface Issue {
  path: string;
  message: string;
  line: number | null;
  col: number | null;
  hint: string | null;
  node_id: string | null;
  field: string | null;
  edge_index: number | null;
  edge_indexes: number[];
}

export interface Model {
  name: string | null;
  version: number | null;
  description: string | null;
  trigger: Record<string, unknown> | null;
  /** top-level `start:`; null when the file has none (the defaults apply) */
  start?: StartPoint | null;
  budget_usd: number | null;
  /** top-level `defaults:` (harness, model, max_turns); null when the file has none */
  defaults?: Defaults | null;
  nodes: ModelNode[];
  edges: ModelEdge[];
  issues: Issue[];
}

export interface PipelineSummary {
  name: string;
  path: string;
  pipeline: string | null;
  nodes: number;
  issues: number;
}

export interface FieldSpec {
  name: string;
  kind: string;
  required: boolean;
  options?: string[];
  source?: "runners" | "actions" | "waiters" | "workspaces" | "harnesses" | "models";
  suggest?: string[];
  keys?: string[];
  help?: string;
  min?: number;
}

export interface NodeTypes {
  types: Record<string, { required: string[]; fields: FieldSpec[] }>;
  runners: string[];
  actions: string[];
  deploy_class_actions: string[];
  waiters: string[];
  workspaces: string[];
  edge: { on_exhausted: string[]; default_when: string };
  ai: AiSummary;
  harnesses: string[];
  models: string[];
  defaults: { fields: string[] };
  /** names in the fetched skill library (empty until it's fetched) */
  skills?: string[];
}

// -- AI harnesses and model sources (src/agentline/agents.py `summary`, api/settings.py) -------

export interface Defaults {
  harness?: string;
  model?: string;
  max_turns?: number;
}

export interface HarnessKindInfo {
  runner: string;
  provider_types: string[];
  needs_model: boolean;
  subscription: boolean;
  default_bin: string | null;
  description: string;
}

export interface ProviderInfo {
  name: string;
  type: string; // anthropic | openai-compatible
  base_url: string | null;
  api_key_env: string | null;
  auth: string; // api-key | subscription
  headers: Record<string, string>;
  description: string;
  implicit: boolean;
  builtin?: boolean; // a built-in source (removing it lists it under [removed] providers)
  ready?: boolean; // its key is set / Claude Code is logged in
  not_ready?: string | null; // why not
  connected?: boolean; // usable now: shown as a group on the Models page
  user_set?: boolean; // set up by you: shown under "Needs attention" when not connected
}

export interface ModelInfo {
  name: string;
  provider: string;
  model: string;
  input_usd_per_mtok: number | null;
  output_usd_per_mtok: number | null;
  max_tokens: number | null;
  temperature: number | null;
  description: string;
  implicit?: boolean; // built in (not in agentline.toml)
  ready?: boolean; // its source can be used now
  hidden?: boolean; // kept out of every picker (the eye on the Models page)
}

export interface HarnessInfo {
  name: string;
  kind: string;
  bin: string | null;
  model: string | null;
  extra_args: string[];
  env: string[] | null;
  description: string;
  implicit: boolean;
  /** null: needs no program (builtin); else is its program on PATH */
  installed?: boolean | null;
}

export interface HarnessPreset extends HarnessInfo {
  installed: boolean; // its program is on PATH
}

export interface AiSummary {
  kinds: Record<string, HarnessKindInfo>;
  providers: ProviderInfo[];
  models: ModelInfo[];
  harnesses: HarnessInfo[];
  /** CLI harnesses that aren't set up yet ("+ Add a harness…") */
  presets?: HarnessPreset[];
  compatible_models: Record<string, string[]>; // usable models only (source ready)
  removed_builtins?: string[];
}

export interface KeyStatus {
  name: string;
  set: boolean;
  source: string | null; // "environment" | "secrets file" | null
  /** the feature whose page sets it: provider:<name>, mcp:<name>, chat:<platform>, github;
   * null: General > Other keys */
  owner?: string | null;
}

export interface McpServer {
  name: string;
  transport: string;
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  url?: string;
  headers?: Record<string, string>;
  description?: string;
  keys?: string[];
  enabled?: boolean;
  /** run reviews may replay nodes with it (probes); off: probes run without it */
  probe_safe?: boolean;
  /** pipeline nodes that allow it (`mcp: [name]`) */
  used_by?: { pipeline: string; node: string }[];
}

/** An MCP server found in another tool's config (GET /api/mcp/detect). Never a secret value:
 * `secrets` are the keys that move to the secrets file on import. */
export interface DetectedMcp {
  id: string;
  source: string;
  path: string;
  name: string;
  suggested: string;
  status: "new" | "configured" | "problem";
  problem?: string | null;
  transport?: string;
  command?: string;
  args?: string[];
  url?: string;
  env?: Record<string, string>;
  headers?: Record<string, string>;
  secrets: { key: string; from: string }[];
}

export interface Settings extends AiSummary {
  keys: KeyStatus[];
  mcp?: McpServer[];
  config_path: string;
  note?: string;
}

export type SettingsSection = "providers" | "models" | "harnesses" | "mcp";

export interface TestedModel {
  id: string;
  name: string;
  free: boolean;
  context_length: number | null;
}

export interface ProviderTest {
  ok: boolean;
  count?: number;
  models: TestedModel[];
  error?: string;
}

/** A subscription source's Claude Code login (`claude auth status`). */
export interface ClaudeLogin {
  cli: string | null;
  logged_in: boolean;
  method?: string | null;
  subscription?: string | null;
  email?: string | null;
  error?: string;
}

export interface DetectResult {
  found: boolean;
  bin?: string;
  version?: string | null;
  error?: string;
  note?: string;
}

export interface Run {
  id: string;
  flow: string;
  status: string;
  payload: unknown;
  cost_usd: number;
  budget_usd: number | null;
  error: string | null;
  created_at: number;
  updated_at: number;
  terminal: boolean;
}

export interface Execution {
  id: string;
  node_id: string;
  iteration: number;
  key: string;
  status: string;
  attempt: number;
  reclaims: number;
  cost_usd: number;
  reason: string | null;
  error: string | null;
  detail: Record<string, unknown>;
  result_ref: string | null;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
}

export interface LineageItem {
  name: string;
  hash: string;
  summary: string;
  size: number | null;
  from?: string | null;
}

export interface LineageEntry {
  execution_id: string;
  node: string;
  node_id: string;
  iteration: number;
  status: string;
  reads: LineageItem[];
  writes: LineageItem[];
}

export interface NextCommand {
  node: string | null;
  command: string;
  description: string;
}

export interface RunDetail {
  run: Run;
  executions: Execution[];
  lineage: LineageEntry[];
  effects: Record<string, unknown>[];
  costs: Record<string, unknown>[];
  pipeline: { name: string; model: Model } | null;
  next_commands: NextCommand[];
  /** for_each items (api/runs.py task_dict) */
  tasks?: TaskInfo[];
  /** per node: tasks pending / running / done / failed (a plain node counts as one task) */
  node_counts?: Record<string, TaskCounts>;
}

export interface TaskCounts {
  total: number;
  pending: number;
  running: number;
  done: number;
  failed: number;
}

export interface TaskInfo {
  execution_id: string;
  idx: number;
  node_id: string;
  iteration: number;
  status: string;
  attempts: number;
  item: unknown;
  error: string | null;
  started_at: number | null;
  finished_at: number | null;
}

export interface JournalEntry {
  id: number;
  at: number;
  execution_id: string | null;
  node_id: string | null;
  iteration: number;
  task: number | null;
  kind: string;
  message: string;
  detail: Record<string, unknown>;
}

export interface ArtifactValue {
  hash: string;
  kind: string;
  media_type: string;
  size: number;
  summary?: string;
  encoding: "json" | "text" | "base64";
  value: unknown;
  truncated: boolean;
}

// -- first-run setup (api/setup.py) -----------------------------------------------------------

/** Step 1: a model source and whether it can be used now (setup.source_status). */
export interface SetupSource {
  id: string;
  title: string;
  description: string;
  type: string;
  /** the env var holding its key; null for a login or a keyless local server */
  key: string | null;
  available: boolean;
  hint: string;
  local_models: string[];
}

/** Step 2: a harness, whether it can run now, and the models it can use (setup.harness_status). */
export interface SetupHarness {
  id: string;
  kind: string;
  title: string;
  description: string;
  available: boolean;
  hint: string;
  models: string[];
  /** the model picked by default; null = the harness's own default */
  model: string | null;
  /** the program it needs (claude, codex, aider, opencode, hermes); null for the built-in harness */
  cli: string | null;
  /** false: installed but not set up yet; choosing it adds it to Settings > Harnesses */
  configured?: boolean;
}

export interface SetupStatus {
  ready: boolean;
  first_run: boolean;
  needs_setup: boolean;
  choice: { harness: string; model: string | null } | null;
  assistant: { harness: string; model: string | null } | null;
  sources: SetupSource[];
  harnesses: SetupHarness[];
  managed: string | null;
}
