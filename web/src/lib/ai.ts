// AI harnesses and models: pure helpers for the pickers, the canvas and the Settings page.
// The rules mirror src/agentline/agents.py (`choose`, `resolve`, `summary`).

import type {
  AiSummary,
  Defaults,
  Issue,
  KeyStatus,
  ModelNode,
  ProviderInfo,
  TestedModel,
} from "../types";

/** A Harness picker's "+ Add a harness…" entry (components/AddHarness.tsx). */
export const ADD_HARNESS = "\u0000add-harness";
/** Fired after a harness or other setting is saved outside Settings: App reloads the pickers. */
export const SETTINGS_CHANGED_EVENT = "agentline:settings-changed";

/** The harness a node runs when neither it nor the pipeline names one (agents.resolve). */
export const FALLBACK_HARNESS = "builtin";
/** agentline's own harness (`llm` before phase 35): what a node runs when it names only a
 * model and there is no default harness. */
export const BUILTIN_HARNESS = "builtin";

const str = (v: unknown): string | null => (typeof v === "string" && v !== "" ? v : null);

/**
 * A node's (harness, model) after pipeline defaults. Defaults are a pair: a node that picks a
 * different harness gets that harness's own default model, not the pipeline's default model.
 * Same as `agents.choose`.
 */
export function choose(
  nodeHarness: string | null,
  nodeModel: string | null,
  defaultHarness: string | null,
  defaultModel: string | null,
): [string | null, string | null] {
  const harness = nodeHarness ?? defaultHarness;
  if (nodeModel !== null) return [harness, nodeModel];
  if (nodeHarness === null || nodeHarness === defaultHarness) return [harness, defaultModel];
  return [harness, null];
}

/** The harness `resolve` would run for a chosen (harness, model). */
export function harnessFor(harness: string | null, model: string | null): string {
  return harness ?? (model ? BUILTIN_HARNESS : FALLBACK_HARNESS);
}

export type HarnessSource = "node" | "defaults" | "fallback";
export type ModelSource = "node" | "defaults" | "harness" | null;

export interface Effective {
  harness: string;
  model: string | null;
  harnessSource: HarnessSource;
  modelSource: ModelSource;
}

/** What an agent node (or the pipeline defaults, with `fields = {}`) will actually use. */
export function effective(
  fields: Record<string, unknown>,
  defaults: Defaults | null | undefined,
  ai?: AiSummary | null,
): Effective {
  const nodeHarness = str(fields.harness);
  const nodeModel = str(fields.model);
  const dHarness = str(defaults?.harness);
  const dModel = str(defaults?.model);
  const [a, m] = choose(nodeHarness, nodeModel, dHarness, dModel);
  const harness = harnessFor(a, m);
  const harnessSource: HarnessSource = nodeHarness ? "node" : dHarness ? "defaults" : "fallback";
  if (m !== null) return { harness, model: m, harnessSource, modelSource: nodeModel ? "node" : "defaults" };
  const own = ai?.harnesses.find((x) => x.name === harness)?.model ?? null;
  return { harness, model: own, harnessSource, modelSource: own ? "harness" : null };
}

/** The model an LLM router uses: its own, else the pipeline's default model. */
export function routerModel(
  fields: Record<string, unknown>,
  defaults: Defaults | null | undefined,
): { model: string | null; inherited: boolean } {
  const own = str(fields.model);
  if (own) return { model: own, inherited: false };
  return { model: str(defaults?.model), inherited: true };
}

/** Named models that suit a harness (the server computes this per harness). */
export function compatibleModels(ai: AiSummary | null | undefined, harness: string): string[] {
  return ai?.compatible_models[harness] ?? [];
}

/**
 * The Model select of "Chat uses": only the models the chosen harness supports (the server's
 * `compatible_models`, e.g. claude-code lists Anthropic models only). A saved model the harness
 * can't use stays visible, marked, so the select never silently shows something else.
 */
export function chatModelOptions(
  compatible: Record<string, string[]> | null | undefined,
  harness: string,
  current: string,
): Option[] {
  const names = compatible?.[harness] ?? [];
  const opts = names.map((m) => ({ value: m, label: m }));
  if (current && !names.includes(current)) opts.unshift({ value: current, label: `${current} (not supported by ${harness})` });
  return opts;
}

/**
 * The Chat panel's "default" model entry: what the engine uses when no model is picked. With the
 * harness left on default, the model saved under "Chat uses" (pass `saved`); else the harness's
 * own default model; a harness that needs a model and has none says so.
 */
export function chatDefaultModelLabel(
  ai: AiSummary | null | undefined,
  harness: string,
  saved: { harness: string; model: string | null } | null,
): string {
  const ready = ai?.compatible_models[harness] ?? [];
  const mark = (m: string) => (ready.includes(m) ? `default: ${m}` : `default: ${m} (not ready)`);
  if (saved && saved.harness === harness && saved.model) return mark(saved.model);
  const own = ai?.harnesses.find((h) => h.name === harness)?.model;
  if (own) return mark(own);
  const kind = ai?.harnesses.find((h) => h.name === harness)?.kind;
  const needs = kind ? ai?.kinds[kind]?.needs_model : false;
  return needs ? "pick a model" : `${harness}'s own model`;
}

/** After switching the Chat's harness: keep the model only if the new harness supports it. */
export function modelAfterHarnessChange(
  compatible: Record<string, string[]> | null | undefined,
  harness: string,
  model: string,
): string {
  return model && (compatible?.[harness] ?? []).includes(model) ? model : "";
}

/**
 * The harness whose compatible models a node's Model select lists: the node's or the default
 * harness; with neither, picking a model makes the node a `builtin` call.
 */
export function pickerHarness(
  fields: Record<string, unknown>,
  defaults: Defaults | null | undefined,
): string {
  return str(fields.harness) ?? str(defaults?.harness) ?? BUILTIN_HARNESS;
}

export interface Option {
  value: string;
  label: string;
}

export function harnessOptions(ai: AiSummary | null | undefined): Option[] {
  return (ai?.harnesses ?? []).map((a) => ({
    value: a.name,
    label: a.implicit ? `${a.name} (built-in)` : a.kind !== a.name ? `${a.name} (${a.kind})` : a.name,
  }));
}

/** Labels for the empty entries of a node's Harness and Model selects. */
export function emptyLabels(
  fields: Record<string, unknown>,
  defaults: Defaults | null | undefined,
  ai?: AiSummary | null,
): { harness: string; model: string } {
  const dHarness = str(defaults?.harness);
  const harness = `(pipeline default: ${dHarness ?? FALLBACK_HARNESS})`;
  const withoutModel = effective({ ...fields, model: undefined }, defaults, ai);
  const model =
    withoutModel.modelSource === "defaults"
      ? `(pipeline default: ${withoutModel.model})`
      : withoutModel.model
        ? `(harness default: ${withoutModel.model})`
        : "(harness default)";
  return { harness, model };
}

// -- field edits (pure; return new field maps) -----------------------------------------------

function without(fields: Record<string, unknown>, ...keys: string[]): Record<string, unknown> {
  const next = { ...fields };
  for (const k of keys) delete next[k];
  return next;
}

/** `harness` and `runner` are mutually exclusive: setting one removes the other. */
export function setHarness(
  fields: Record<string, unknown>,
  harness: string | undefined,
): Record<string, unknown> {
  const rest = without(fields, "agent"); // the old name of `harness`
  return harness ? { ...without(rest, "runner"), harness } : without(rest, "harness");
}

export function setRunner(
  fields: Record<string, unknown>,
  runner: string | undefined,
): Record<string, unknown> {
  return runner ? { ...without(fields, "harness", "agent"), runner } : without(fields, "runner");
}

export function setModel(
  fields: Record<string, unknown>,
  model: string | undefined,
): Record<string, unknown> {
  return model ? { ...fields, model } : without(fields, "model");
}

export type RouterMode = "switch" | "llm";

export function routerMode(fields: Record<string, unknown>): RouterMode {
  return fields.mode === "llm" ? "llm" : "switch";
}

/** Switch a router between `switch` (the default, no `mode:` key) and `llm` (prompt + model). */
export function setRouterMode(
  fields: Record<string, unknown>,
  mode: RouterMode,
): Record<string, unknown> {
  if (mode === "llm") {
    const next: Record<string, unknown> = { ...without(fields, "switch"), mode: "llm" };
    return next.prompt === undefined ? { ...next, prompt: "" } : next;
  }
  const next = without(fields, "mode", "prompt", "model");
  return next.switch === undefined ? { ...next, switch: "" } : next;
}

/** Set or clear one `defaults:` key. No keys left: `null` (the section is removed). */
export function setDefault(
  defaults: Defaults | null | undefined,
  key: keyof Defaults,
  value: string | number | undefined,
): Defaults | null {
  const next: Record<string, unknown> = { ...(defaults ?? {}) };
  if (value === undefined || value === "") delete next[key];
  else next[key] = value;
  return Object.keys(next).length ? (next as Defaults) : null;
}

/** Issues about the harness/model choice (the validator points most at the node, not a field). */
export function isAiIssue(i: Issue): boolean {
  return (
    i.field === "harness" ||
    i.field === "model" ||
    (!i.field && !!i.hint && i.hint.includes("Settings")) ||
    (!i.field && /llm router has no model|either 'harness'|needs runner/.test(i.message))
  );
}

// -- canvas ----------------------------------------------------------------------------------

export interface AiLine {
  harness: string | null;
  model: string | null;
  harnessInherited: boolean;
  modelInherited: boolean;
}

/** The small second line on a canvas node: `claude-code · opus`, or the router's model. */
export function aiLine(
  node: ModelNode,
  defaults: Defaults | null | undefined,
  ai?: AiSummary | null,
): AiLine | null {
  if (node.type === "agent" || node.type === "chat") {
    if (str(node.fields.runner)) return null; // a low-level runner: shown as the summary
    const e = effective(node.fields, defaults, ai);
    return {
      harness: e.harness,
      model: e.model,
      harnessInherited: e.harnessSource !== "node",
      modelInherited: e.modelSource !== "node",
    };
  }
  if (node.type === "router" && routerMode(node.fields) === "llm") {
    const r = routerModel(node.fields, defaults);
    return { harness: null, model: r.model, harnessInherited: true, modelInherited: r.inherited };
  }
  return null;
}

// -- Settings page ---------------------------------------------------------------------------

/** Models from a provider test, filtered by a search text and "free only". */
/** Toggle `id` in a checklist selection (a new Set: React state). */
export function toggled(sel: ReadonlySet<string>, id: string): Set<string> {
  const next = new Set(sel);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  return next;
}

/** "Select shown": every shown model that isn't added yet, plus what was already selected. */
export function selectShown(
  sel: ReadonlySet<string>,
  shown: TestedModel[],
  added: ReadonlySet<string>,
): Set<string> {
  return new Set([...sel, ...shown.map((m) => m.id).filter((id) => !added.has(id))]);
}

export function filterTested(models: TestedModel[], query: string, freeOnly: boolean) {
  const q = query.trim().toLowerCase();
  return models.filter(
    (m) =>
      (!freeOnly || m.free) &&
      (!q || m.id.toLowerCase().includes(q) || m.name.toLowerCase().includes(q)),
  );
}

export interface ProviderPreset {
  label: string;
  names: string[]; // preferred names, first free one wins
  body: Record<string, unknown>;
  help: string;
}

export const PROVIDER_PRESETS: ProviderPreset[] = [
  {
    label: "OpenRouter",
    names: ["openrouter"],
    body: {
      type: "openai-compatible",
      base_url: "https://openrouter.ai/api/v1",
      api_key_env: "OPENROUTER_API_KEY",
      description: "OpenRouter: many models, some free (:free)",
    },
    help: "Many models behind one key; ids ending in :free cost nothing.",
  },
  {
    label: "Ollama",
    names: ["ollama"],
    body: {
      type: "openai-compatible",
      base_url: "http://localhost:11434/v1",
      description: "Local Ollama server (no key)",
    },
    help: "A local model server; no key.",
  },
  {
    label: "OpenAI",
    names: ["openai"],
    body: {
      type: "openai-compatible",
      base_url: "https://api.openai.com/v1",
      api_key_env: "OPENAI_API_KEY",
      description: "OpenAI API",
    },
    help: "OpenAI's API (Codex, Aider, the built-in harness).",
  },
  {
    label: "Anthropic API key",
    names: ["anthropic", "anthropic-api"],
    body: {
      type: "anthropic",
      auth: "api-key",
      api_key_env: "ANTHROPIC_API_KEY",
      description: "Anthropic API (key)",
    },
    help: "Anthropic models billed to an API key.",
  },
  {
    label: "Claude subscription",
    names: ["anthropic", "claude-subscription"],
    body: {
      type: "anthropic",
      auth: "subscription",
      description: "Claude Code's own login (Pro/Max)",
    },
    help: "Claude Code uses its own login; only the claude-code harness can use this.",
  },
];

/** The first preferred name not already configured (implicit entries may be taken over). */
export function presetName(preset: ProviderPreset, providers: ProviderInfo[]): string {
  const taken = new Set(providers.filter((p) => !p.implicit).map((p) => p.name));
  for (const n of preset.names) if (!taken.has(n)) return n;
  const base = preset.names[preset.names.length - 1];
  for (let i = 2; ; i++) if (!taken.has(`${base}-${i}`)) return `${base}-${i}`;
}

/** Form values -> PUT body: trims strings, drops empties, parses numeric fields. */
/** Fields the server adds to entries for display; never sent back. */
const VIEW_ONLY = new Set(["name", "implicit", "builtin", "ready", "not_ready"]);

export function formBody(
  form: Record<string, unknown>,
  numeric: string[] = [],
): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(form)) {
    if (VIEW_ONLY.has(k) || v === null || v === undefined) continue;
    if (numeric.includes(k)) {
      const n = typeof v === "number" ? v : parseFloat(String(v));
      if (String(v).trim() !== "" && !Number.isNaN(n)) out[k] = n;
      continue;
    }
    if (typeof v === "string") {
      if (v.trim()) out[k] = v.trim();
    } else if (Array.isArray(v)) {
      if (v.length) out[k] = v;
    } else if (typeof v === "object") {
      if (Object.keys(v).length) out[k] = v;
    } else {
      out[k] = v;
    }
  }
  return out;
}

export const NAME_RE = /^[A-Za-z0-9_][A-Za-z0-9_.-]*$/;

/**
 * Named models a harness of `kind` can use, computed in the browser for a form whose kind is
 * not saved yet. Same rule as `compatible_models` in agents.summary.
 */
export function compatibleForKind(ai: AiSummary | null | undefined, kind: string): string[] {
  const k = ai?.kinds[kind];
  if (!ai || !k) return [];
  const provs = new Map(ai.providers.map((p) => [p.name, p]));
  return ai.models
    .filter((m) => {
      const p = provs.get(m.provider);
      return !!p && k.provider_types.includes(p.type) && (p.auth !== "subscription" || k.subscription);
    })
    .map((m) => m.name);
}

/** A `[models]` name from a provider's model id: `meta-llama/llama-3.3-70b:free` -> `llama-3.3-70b-free`. */
export function suggestModelName(id: string): string {
  const last = id.split("/").pop() ?? id;
  return last.replace(/[^A-Za-z0-9_.-]+/g, "-").replace(/^[.-]+|-+$/g, "") || "model";
}

/**
 * Save an API key. The value goes straight to the server and is not kept: the returned draft
 * is "" after a successful save (the caller puts it back into the input), and unchanged only
 * when the save failed, so the user can retry.
 */
export async function submitSecret(
  name: string,
  draft: string,
  put: (name: string, value: string) => Promise<{ keys: KeyStatus[] }>,
): Promise<{ draft: string; keys?: KeyStatus[]; error?: string }> {
  if (!draft.trim()) return { draft: "", error: "enter a value" };
  try {
    const r = await put(name, draft);
    return { draft: "", keys: r.keys };
  } catch (e) {
    return { draft, error: (e as Error).message };
  }
}
