// People who use the engine with their own token (spec 007, Settings > People). The engine
// never returns a token except once, in the reply that issues it.

export interface Person {
  name: string;
  admin: boolean;
  created_at: number;
}

export interface Identity {
  kind: "local" | "shared" | "person";
  name: string;
  admin: boolean;
}

/** Same rule as the engine (agentline.people.check_person_name). */
export const PERSON_NAME_RE = /^[A-Za-z0-9._@-]{1,64}$/;
const RESERVED = new Set(["shared-token", "local"]);

/** Why `name` can't be a person's name, or null when it can. */
export function personNameProblem(name: string, taken: string[] = []): string | null {
  const n = name.trim();
  if (!PERSON_NAME_RE.test(n)) return "1-64 letters, digits, '.', '_', '-' or '@' (no spaces)";
  if (RESERVED.has(n.toLowerCase())) return `"${n}" is reserved`;
  if (taken.some((t) => t.toLowerCase() === n.toLowerCase())) return `${n} is already here`;
  return null;
}

/** "alice (admin)", "the shared API token", "huu on the engine's machine" */
export function identityLabel(me: Identity): string {
  if (me.kind === "shared") return "the engine's shared API token";
  if (me.kind === "local") return `${me.name} on the engine's machine`;
  return `${me.name}${me.admin ? " (admin)" : ""}`;
}
