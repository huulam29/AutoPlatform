// Settings > Skills > Add skills: what the one input means (a marketplace search or a GitHub
// source like `docker/skills`) and how found skills are labelled. Server: skill_sources.py.

/** A GitHub source (owner/repo, a URL, a pasted `npx skills add ...`) rather than search words. */
export function looksLikeSource(text: string): boolean {
  const t = text.trim();
  if (!t) return false;
  if (/^(npx|bunx|pnpx|pnpm)\s/.test(t) || /^(https?:\/\/|github[.:])/.test(t)) return true;
  return !/\s/.test(t) && /^[\w.-]+\/[\w.-]+/.test(t);
}

/** 1234 -> "1.2k", 564507 -> "565k". */
export function formatInstalls(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`;
  if (n >= 10_000) return `${Math.round(n / 1000)}k`;
  if (n >= 1000) return `${(n / 1000).toFixed(1).replace(/\.0$/, "")}k`;
  return String(n);
}

/** What installing a found skill does to one already here with the same name. */
export function clashNote(exists: string | null | undefined, installedFrom?: string | null): string | null {
  switch (exists) {
    case "installed":
      return installedFrom ? `replaces the one from ${installedFrom}` : "replaces the installed one";
    case "local":
      return "your own skill of this name wins";
    case "fetched":
      return "replaces the library's";
    case "builtin":
      return "replaces the built-in one";
    default:
      return null;
  }
}

/** The source that installs one search result (`owner/repo@skill`). */
export function resultSource(r: { source: string; skill: string }): string {
  return `${r.source}@${r.skill}`;
}
