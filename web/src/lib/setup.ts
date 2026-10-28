// First-run setup (api/setup.py), pure helpers for components/SetupDialog.tsx: step 1 is the
// model sources (keys, a Claude login, a local server), step 2 the harness the Chat uses.

import type { SetupHarness, SetupStatus } from "../types";

export type SetupStep = "sources" | "harness";

/** Where the popup starts: the model sources, unless one already works (then the harness). */
export function firstStep(s: SetupStatus): SetupStep {
  return s.sources.some((x) => x.available) && !s.first_run ? "harness" : "sources";
}

/** The Next button of step 1: "Next" once a source works, else it's a skip. */
export function sourcesNextLabel(s: SetupStatus): string {
  return s.sources.some((x) => x.available) ? "Next: pick a harness" : "Skip this step";
}

/** The model choices for a harness card: its models, plus "its own default" for a CLI harness
 * that doesn't need one (Claude Code picks its own model). */
export function harnessModelOptions(h: SetupHarness): { value: string; label: string }[] {
  const opts = h.models.map((m) => ({ value: m, label: m }));
  return h.cli ? [{ value: "", label: `${h.title}'s own default` }, ...opts] : opts;
}

/** The harness the popup highlights: the saved / suggested one, else the first that can run. */
export function suggestedHarness(s: SetupStatus): string | null {
  const id = s.choice?.harness ?? s.assistant?.harness;
  if (id && s.harnesses.some((h) => h.id === id && h.available)) return id;
  return s.harnesses.find((h) => h.available)?.id ?? null;
}

/** Ask for the setup popup from anywhere (Settings > General: "Run setup again"). */
export const OPEN_SETUP_EVENT = "agentline:open-setup";
