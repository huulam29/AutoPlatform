// The Logs page's pure parts: line levels, filtering and search (tested in logs.test.ts).

export const LINE_COUNTS = [500, 1000, 2000, 5000] as const;

export type Level = "debug" | "info" | "warning" | "error";
export type LevelFilter = "all" | "warning" | "error";

// `2026-10-20 09:13:33,085 INFO    uvicorn.access: ...` (logs.TextFormatter)
const LEVEL_RE = /^\d{4}-\d\d-\d\d[ T][\d:.,]+\s+(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)\b/;

/** A line's level, or null for a line with none (a traceback line, `[app] ...`, raw output). */
export function levelOf(line: string): Level | null {
  const m = LEVEL_RE.exec(line);
  if (!m) return null;
  const l = m[1];
  if (l === "DEBUG") return "debug";
  if (l === "INFO") return "info";
  if (l === "WARNING" || l === "WARN") return "warning";
  return "error"; // ERROR, CRITICAL
}

export interface LogLine {
  /** position in the fetched lines, a stable React key */
  n: number;
  text: string;
  level: Level | null;
}

/**
 * Lines to show: at least `level` (a line without a level, such as a traceback, belongs to the
 * entry above it), containing `query` (case-insensitive) when one is given.
 */
export function filterLines(lines: string[], level: LevelFilter, query: string): LogLine[] {
  const min = level === "error" ? 2 : level === "warning" ? 1 : 0;
  const rank = (l: Level | null) => (l === "error" ? 2 : l === "warning" ? 1 : 0);
  const q = query.trim().toLowerCase();
  const out: LogLine[] = [];
  let current: Level | null = null;
  lines.forEach((text, n) => {
    const own = levelOf(text);
    if (own) current = own;
    const effective = own ?? current;
    if (min > 0 && rank(effective) < min) return;
    if (q && !text.toLowerCase().includes(q)) return;
    out.push({ n, text, level: effective });
  });
  return out;
}

/** `text` split around case-insensitive matches of `query`, for highlighting. */
export function splitMatches(text: string, query: string): { text: string; hit: boolean }[] {
  const q = query.trim();
  if (!q) return [{ text, hit: false }];
  const lower = text.toLowerCase();
  const ql = q.toLowerCase();
  const parts: { text: string; hit: boolean }[] = [];
  let i = 0;
  for (let j = lower.indexOf(ql); j >= 0; j = lower.indexOf(ql, i)) {
    if (j > i) parts.push({ text: text.slice(i, j), hit: false });
    parts.push({ text: text.slice(j, j + q.length), hit: true });
    i = j + q.length;
  }
  if (i < text.length) parts.push({ text: text.slice(i), hit: false });
  return parts;
}

/** How many lines arrived after the shown snapshot (the buffer trims from the top). */
export function newSince(shown: string[], latest: string[]): number {
  if (!shown.length) return latest.length;
  const last = shown[shown.length - 1];
  const at = latest.lastIndexOf(last);
  return at < 0 ? latest.length : latest.length - 1 - at;
}
