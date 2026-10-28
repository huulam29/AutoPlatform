// Settings > Models (components/settings/ModelsPage.tsx): what the page shows. Only what is
// usable: a group per connected model source with its models; sources you set up that stopped
// working go to "Needs attention"; sources nobody connected appear only in Add models.

import type { ModelInfo, ProviderInfo, Settings } from "../types";

export interface ModelGroup {
  provider: ProviderInfo;
  models: ModelInfo[];
}

export function isFree(m: ModelInfo): boolean {
  return m.model.endsWith(":free") || (m.input_usd_per_mtok === 0 && m.output_usd_per_mtok === 0);
}

/** Does a model match the search box? Its name, id or source; "free" also matches free models. */
export function matches(m: ModelInfo, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  if (q === "free" && isFree(m)) return true;
  return [m.name, m.model, m.provider].some((x) => x.toLowerCase().includes(q));
}

/** Connected sources with their (matching) models: the ones you set up first, then built-ins. A
 * search hides groups with no match, unless the source's own name matches. */
export function modelGroups(data: Pick<Settings, "providers" | "models">, query = ""): ModelGroup[] {
  const q = query.trim().toLowerCase();
  const sources = data.providers
    .filter((p) => p.connected ?? p.ready)
    .map((p, i) => ({ p, i }))
    .sort((a, b) => Number(a.p.implicit) - Number(b.p.implicit) || a.i - b.i)
    .map(({ p }) => p);
  const out: ModelGroup[] = [];
  for (const p of sources) {
    const own = data.models.filter((m) => m.provider === p.name);
    const shown = q && p.name.toLowerCase().includes(q) ? own : own.filter((m) => matches(m, query));
    if (q && !shown.length) continue;
    out.push({ provider: p, models: shown });
  }
  return out;
}

/** Sources you set up that can't be used right now (key removed, signed out, server down). */
export function needsAttention(data: Pick<Settings, "providers">): ProviderInfo[] {
  return data.providers.filter((p) => p.user_set && !(p.connected ?? p.ready));
}

/** A model source's display name. */
export function sourceTitle(p: ProviderInfo): string {
  const known: Record<string, string> = {
    openrouter: "OpenRouter",
    anthropic: "Anthropic API",
    openai: "OpenAI",
    claude: "Claude subscription",
    ollama: "Ollama",
  };
  return known[p.name] ?? p.name;
}

/** A harness's status on the Harnesses page: can it run now, and if not, what's missing. */
export function harnessStatus(
  h: { name: string; kind: string; installed?: boolean | null },
  data: Pick<Settings, "kinds" | "compatible_models">,
): { word: string; ok: boolean } {
  if (h.installed === false) return { word: "Program not found", ok: false };
  const needsModel = data.kinds[h.kind]?.needs_model;
  if (needsModel && !(data.compatible_models[h.name] ?? []).length) return { word: "No model ready", ok: false };
  return { word: "Ready", ok: true };
}

/** How to install a CLI harness's program (Add harness > Not installed). */
export const INSTALL_COMMANDS: Record<string, string> = {
  "claude-code": "npm install -g @anthropic-ai/claude-code",
  codex: "npm install -g @openai/codex",
  aider: "python -m pip install aider-install && aider-install",
  opencode: "npm install -g @opencode/cli@latest",
  "hermes-agent": "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash",
};
