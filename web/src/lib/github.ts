// GitHub setup for failure-triage reports (Settings > General; engine: agentline/github_setup.py).
// Types and the pure helpers the section uses (tested in github.test.ts).

export interface GithubLogin {
  state: "idle" | "waiting" | "done" | "failed" | "cancelled";
  code: string | null;
  url: string | null;
  error: string | null;
}

export interface GithubSetup {
  repo: string | null;
  auto: boolean;
  labels: string[];
  error: string | null;
  token_source: "env" | "secrets" | "gh" | null;
  ready: boolean;
  gh: { installed: boolean; logged_in: boolean; user: string | null };
  detected: { repo: string; remote: string; protocol: "ssh" | "https"; where: string; url: string }[];
  login: GithubLogin;
}

export interface GithubTest {
  ok: boolean;
  repo?: string;
  private?: boolean;
  token_source?: string | null;
  error: string | null;
}

/** Where the token comes from, in words. */
export function tokenSourceLabel(s: GithubSetup["token_source"], user?: string | null): string {
  switch (s) {
    case "gh":
      return `GitHub CLI sign-in${user ? ` (${user})` : ""}`;
    case "secrets":
      return "pasted token (secrets file)";
    case "env":
      return "GITHUB_TOKEN in the environment";
    default:
      return "not signed in";
  }
}

/** The section's status chip: a status for its tone and the word to show. */
export function setupStatus(g: Pick<GithubSetup, "ready" | "repo" | "token_source">): { s: string; word: string } {
  if (g.ready) return { s: "done", word: "Ready" };
  if (g.repo || g.token_source) return { s: "needs-review", word: g.repo ? "Sign in needed" : "Repo needed" };
  return { s: "waiting", word: "Not set up" };
}

/** Detected remotes not already chosen, labelled "me/agentline (agentline, SSH)". */
export function detectedChoices(g: Pick<GithubSetup, "detected" | "repo">): { repo: string; label: string }[] {
  return g.detected
    .filter((d) => d.repo !== g.repo)
    .map((d) => ({ repo: d.repo, label: `${d.repo} (${d.where}, ${d.protocol.toUpperCase()})` }));
}

/** Whether the sign-in is still going (keep polling). */
export function loginPending(l: GithubLogin | null | undefined): boolean {
  return l?.state === "waiting";
}
