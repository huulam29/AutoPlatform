// Settings > MCP: the new-server popup's draft and what it saves (tested in mcp.test.ts).
// A secret row never puts its value in agentline.toml: the table gets `${KEY}` (after an
// optional prefix such as "Bearer ") and the value goes to the secrets file (PUT /api/secrets).
import type { McpServer } from "../types";

export type Transport = "stdio" | "http" | "sse";

export interface McpRow {
  name: string;
  /** a plain value, or a secret's value typed now ("" = keep the one already set) */
  value: string;
  secret: boolean;
  /** the secrets file key a secret row is stored under */
  keyName: string;
  /** text before the ${KEY} (an Authorization header's "Bearer ") */
  prefix: string;
}

export interface McpDraft {
  name: string;
  transport: Transport;
  command: string;
  args: string;
  url: string;
  env: McpRow[];
  headers: McpRow[];
  description: string;
  enabled: boolean;
  /** probes (run reviews) may use it: only for servers that read, never change anything */
  probeSafe: boolean;
}

export const NAME_RE = /^[A-Za-z0-9_][A-Za-z0-9_.-]*$/;
export const KEY_RE = /^[A-Z_][A-Z0-9_]*$/;
const REF = /^(.*?)\$\{([A-Za-z_][A-Za-z0-9_]*)\}$/;
const SECRET_NAME = /(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|COOKIE|SESSION|PRIVATE|(^|_)PAT($|_)|(^|_)AUTH)/i;
const SECRETISH = /(sk-[A-Za-z0-9]{8,}|ghp_[A-Za-z0-9]{8,}|xox[bp]-|Bearer [A-Za-z0-9._-]{12,})/;

export function emptyDraft(): McpDraft {
  return { name: "", transport: "stdio", command: "", args: "", url: "", env: [], headers: [], description: "", enabled: true, probeSafe: false };
}

export function keyPart(text: string): string {
  const k = text.toUpperCase().replace(/[^A-Z0-9_]+/g, "_").replace(/^_+|_+$/g, "") || "KEY";
  return /^[0-9]/.test(k) ? `_${k}` : k;
}

/** The key a secret row is stored under unless the user names it. */
export function suggestKeyName(server: string, row: string, kind: "env" | "headers"): string {
  if (kind === "env") return keyPart(row);
  const s = keyPart(server || "mcp");
  return row.toLowerCase() === "authorization" ? `${s}_TOKEN` : `${s}_${keyPart(row)}`;
}

/** Should a row typed as plain text be a secret? (name like *_TOKEN, or a token-like value) */
export function looksSecret(name: string, value: string, kind: "env" | "headers"): boolean {
  if (!value || REF.test(value)) return false;
  return kind === "headers" || SECRET_NAME.test(name) || SECRETISH.test(value);
}

export function newRow(server: string, kind: "env" | "headers", name = ""): McpRow {
  const auth = kind === "headers" && name.toLowerCase() === "authorization";
  return {
    name,
    value: "",
    secret: kind === "headers" || SECRET_NAME.test(name),
    keyName: name ? suggestKeyName(server, name, kind) : "",
    prefix: auth ? "Bearer " : "",
  };
}

function rows(table: Record<string, string> | undefined): McpRow[] {
  return Object.entries(table ?? {}).map(([name, v]) => {
    const m = REF.exec(v);
    return m
      ? { name, value: "", secret: true, keyName: m[2], prefix: m[1] }
      : { name, value: v, secret: false, keyName: "", prefix: "" };
  });
}

/** Shell-like split: spaces separate, quotes group ("a b" or 'a b'). */
export function splitArgs(text: string): string[] {
  const out: string[] = [];
  let cur = "";
  let quote: string | null = null;
  let has = false;
  for (const ch of text) {
    if (quote) {
      if (ch === quote) quote = null;
      else cur += ch;
    } else if (ch === '"' || ch === "'") {
      quote = ch;
      has = true;
    } else if (/\s/.test(ch)) {
      if (has || cur) out.push(cur);
      cur = "";
      has = false;
    } else cur += ch;
  }
  if (has || cur) out.push(cur);
  return out;
}

export function joinArgs(args: string[] | undefined): string {
  return (args ?? []).map((a) => (a === "" || /[\s"']/.test(a) ? `"${a.replace(/"/g, "'")}"` : a)).join(" ");
}

/** An existing server in the popup (edit). Its secrets show as rows with an empty value. */
export function draftFromServer(s: McpServer): McpDraft {
  return {
    name: s.name,
    transport: (s.transport as Transport) || "stdio",
    command: s.command ?? "",
    args: joinArgs(s.args),
    url: s.url ?? "",
    env: rows(s.env),
    headers: rows(s.headers),
    description: s.description ?? "",
    enabled: s.enabled !== false,
    probeSafe: s.probe_safe === true,
  };
}

function table(rs: McpRow[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const r of rs) {
    if (!r.name.trim()) continue;
    out[r.name.trim()] = r.secret ? `${r.prefix}\${${r.keyName}}` : r.value;
  }
  return out;
}

/** What Save writes: the agentline.toml table and the secret values typed now. */
export function draftToSave(d: McpDraft): { name: string; body: Record<string, unknown>; secrets: Record<string, string> } {
  const body: Record<string, unknown> = { transport: d.transport };
  const used = d.transport === "stdio" ? d.env : d.headers;
  if (d.transport === "stdio") {
    body.command = d.command.trim();
    const args = splitArgs(d.args);
    if (args.length) body.args = args;
    const env = table(d.env);
    if (Object.keys(env).length) body.env = env;
  } else {
    body.url = d.url.trim();
    const headers = table(d.headers);
    if (Object.keys(headers).length) body.headers = headers;
  }
  if (d.description.trim()) body.description = d.description.trim();
  if (!d.enabled) body.enabled = false;
  if (d.probeSafe) body.probe_safe = true;
  const secrets: Record<string, string> = {};
  for (const r of used) if (r.secret && r.name.trim() && r.value) secrets[r.keyName] = r.value;
  return { name: d.name.trim(), body, secrets };
}

export const STEPS = ["Kind", "Connection", "Keys and settings", "Test and save"] as const;

/** What stops the user leaving step `step` (0-based), as messages. */
export function stepProblems(d: McpDraft, step: number, taken: string[] = [], editing = false): string[] {
  const out: string[] = [];
  if (step >= 1) {
    if (!NAME_RE.test(d.name.trim())) out.push("Name: letters, digits, _ . - (no spaces)");
    else if (!editing && taken.includes(d.name.trim())) out.push(`A server named ${d.name.trim()} already exists`);
    if (d.transport === "stdio" && !d.command.trim()) out.push("Command: the program to start, e.g. npx");
    if (d.transport !== "stdio" && !/^https?:\/\/\S+$/.test(d.url.trim())) out.push("URL: http(s)://…");
  }
  if (step >= 2) {
    const kind = d.transport === "stdio" ? "env" : "headers";
    const seen = new Set<string>();
    for (const r of d.transport === "stdio" ? d.env : d.headers) {
      const n = r.name.trim();
      if (!n) continue;
      if (seen.has(n)) out.push(`${n} is listed twice`);
      seen.add(n);
      if (r.secret && !KEY_RE.test(r.keyName)) out.push(`${n}: the key name must be A-Z, 0-9 and _`);
      if (!r.secret && looksSecret(n, r.value, kind)) out.push(`${n} looks like a secret: tick Secret so it goes to the secrets file`);
    }
  }
  return out;
}

/** Secret rows with no value typed whose key isn't set yet (they'd fail when a node runs). */
export function missingKeys(d: McpDraft, setKeys: Set<string>): string[] {
  const used = d.transport === "stdio" ? d.env : d.headers;
  return used.filter((r) => r.secret && r.name.trim() && !r.value && !setKeys.has(r.keyName)).map((r) => r.keyName);
}

/** "dev-cycle: plan, build · other: review" */
export function usedByText(used: { pipeline: string; node: string }[] | undefined): string {
  const by = new Map<string, string[]>();
  for (const u of used ?? []) by.set(u.pipeline, [...(by.get(u.pipeline) ?? []), u.node]);
  return [...by].map(([p, nodes]) => `${p}: ${nodes.join(", ")}`).join(" · ");
}

/** The import request for the ticked detected servers ({id, name}). */
export function importItems(picked: Record<string, string | undefined>): { id: string; name: string }[] {
  return Object.entries(picked)
    .filter((e): e is [string, string] => typeof e[1] === "string")
    .map(([id, name]) => ({ id, name: name.trim() }));
}
