// A pipeline's start point (`start:`) and the Start run popup's input. Pure functions.

import type { Model, StartInfo, StartInput, StartPoint } from "../types";

/** Nodes without an incoming edge (edges with max_loops go back, so they don't count). */
export function startNodes(model: Model): string[] {
  const entered = new Set(model.edges.filter((e) => e.max_loops == null).map((e) => e.dst));
  return model.nodes.map((n) => n.id).filter((id) => !entered.has(id));
}

/** The node that takes the input: `start.node`, else the chat node, else the first start node. */
export function startNode(model: Model): string | null {
  if (model.start?.node) return model.start.node;
  const chat = model.nodes.find((n) => n.type === "chat");
  return chat?.id ?? startNodes(model)[0] ?? null;
}

export function startInput(model: Model): StartInput {
  const node = model.nodes.find((n) => n.id === startNode(model));
  if (node?.type === "chat") return "chat";
  return model.start?.input ?? "json";
}

/** Set or clear one `start:` key. Nothing left: `null` (the section is removed). */
export function setStart<K extends keyof StartPoint>(
  model: Model,
  key: K,
  value: StartPoint[K] | undefined,
): Model {
  const next: StartPoint = { ...(model.start ?? {}) };
  if (value === undefined || value === "") delete next[key];
  else next[key] = value;
  if (key === "input" && value !== "json" && value !== undefined) delete next.template;
  return { ...model, start: Object.keys(next).length ? next : null };
}

/** The text the popup starts with: the JSON template, or nothing to type over. */
export function initialText(info: StartInfo | null): string {
  if (!info || info.input !== "json") return "";
  return JSON.stringify(info.template ?? {}, null, 2);
}

/** What POST /runs gets from the popup's text, or an error to show. */
export function startBody(
  input: StartInput,
  text: string,
): { payload: Record<string, unknown> } | { text: string } | { error: string } {
  if (input !== "json") {
    return text.trim() ? { text: text.trim() } : { error: "Write what the run should do." };
  }
  if (!text.trim()) return { payload: {} };
  try {
    const v: unknown = JSON.parse(text);
    if (typeof v !== "object" || v === null || Array.isArray(v)) {
      return { error: "The payload must be a JSON object ({ … })." };
    }
    return { payload: v as Record<string, unknown> };
  } catch (e) {
    return { error: `payload isn't JSON: ${(e as Error).message}` };
  }
}

/** Template fields still empty ("" ), shown as a reminder before starting. */
export function emptyFields(value: unknown, prefix = ""): string[] {
  if (value === "") return [prefix];
  if (typeof value !== "object" || value === null || Array.isArray(value)) return [];
  return Object.entries(value).flatMap(([k, v]) => emptyFields(v, prefix ? `${prefix}.${k}` : k));
}
