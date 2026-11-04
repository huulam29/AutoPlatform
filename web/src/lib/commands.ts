// The command palette's commands and keyboard sequences (spec 006 US6): every action in the Mac
// app's menus, reachable in a browser, where Cmd+N / Cmd+R / Cmd+1-4 belong to the browser.

export interface Command {
  id: string;
  title: string;
  section: "Go" | "Run" | "Engine" | "View";
  keywords?: string[];
  /** shown next to the title, e.g. "g r" or "n" */
  shortcut?: string;
  run: () => void;
}

export interface CommandDeps {
  go: (route: string) => void;
  refresh: () => void;
  restartEngine: () => void;
  toggleChat: () => void;
  /** e.g. "⌘J" or "Ctrl+J" */
  chatKey: string;
}

export function buildCommands(d: CommandDeps): Command[] {
  const page = (id: string, title: string, route: string, shortcut?: string, keywords: string[] = []): Command => ({
    id,
    title,
    section: "Go",
    shortcut,
    keywords,
    run: () => d.go(route),
  });
  return [
    { id: "start-run", title: "Start Run…", section: "Run", shortcut: "n", keywords: ["new", "run"], run: () => d.go("/runs/new") },
    page("pipelines", "Pipelines", "/pipelines", "g p", ["editor"]),
    page("runs", "Runs", "/runs", "g r"),
    page("approvals", "Approvals", "/approvals", "g a", ["gates", "approve"]),
    page("problems", "Problems", "/problems", "g x", ["issues"]),
    page("lessons", "Lessons", "/lessons", undefined, ["reviews"]),
    page("costs", "Costs", "/costs", undefined, ["budget", "spend"]),
    page("app-reports", "App reports", "/app-reports", undefined, ["crash", "export"]),
    page("logs", "Show Logs", "/logs", "g l", ["log"]),
    page("settings", "Settings", "/settings", "g s", ["preferences"]),
    { id: "refresh", title: "Refresh", section: "View", keywords: ["reload"], run: d.refresh },
    { id: "toggle-chat", title: "Toggle Chat", section: "View", shortcut: d.chatKey, keywords: ["assistant"], run: d.toggleChat },
    { id: "restart-engine", title: "Restart Engine", section: "Engine", keywords: ["reboot"], run: d.restartEngine },
  ];
}

const words = (s: string) => s.toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);

/** Commands where every typed word starts a word of the title or a keyword; titles whose
 * initials match the query rank first, then title-prefix matches, then the rest in order. */
export function filterCommands(list: Command[], query: string): Command[] {
  const q = words(query);
  if (!q.length) return list;
  const lead = query.trim().toLowerCase();
  const scored: { c: Command; score: number; i: number }[] = [];
  list.forEach((c, i) => {
    const title = words(c.title);
    const initials = title.map((w) => w[0]).join("");
    const hay = [...title, ...(c.keywords ?? []).flatMap(words)];
    const byWords = q.every((w) => hay.some((h) => h.startsWith(w)));
    const byInitials = q.length === 1 && initials.startsWith(q[0]);
    if (!byWords && !byInitials) return;
    const score = q.length === 1 && initials === q[0] ? 0 : c.title.toLowerCase().startsWith(lead) ? 1 : 2;
    scored.push({ c, score, i });
  });
  return scored.sort((a, b) => a.score - b.score || a.i - b.i).map((x) => x.c);
}

/** Where typed keys are text, not shortcuts. */
export function isTypingTarget(el: Element | null): boolean {
  if (!el) return false;
  const tag = el.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
  if ((el as HTMLElement).isContentEditable) return true;
  return !!el.closest?.(".cm-editor, [contenteditable=true]");
}

/** A dialog is open (its keys win over shortcuts and the palette). */
export const dialogOpen = (doc: Document = document): boolean =>
  !!doc.querySelector('[role="dialog"]:not(.palette), dialog[open]');

/** Two-key sequences ("g" then a letter within `windowMs`) and single keys ("n"). */
export class SequenceKeys {
  private pending: { key: string; at: number } | null = null;

  constructor(
    private commands: Command[],
    private windowMs = 1500,
  ) {}

  /** The command for this key press, or null; `now` for tests. */
  press(key: string, now = Date.now()): Command | null {
    const k = key.toLowerCase();
    if (this.pending && now - this.pending.at <= this.windowMs) {
      const seq = `${this.pending.key} ${k}`;
      this.pending = null;
      return this.commands.find((c) => c.shortcut === seq) ?? null;
    }
    this.pending = null;
    if (this.commands.some((c) => c.shortcut?.startsWith(`${k} `))) {
      this.pending = { key: k, at: now };
      return null;
    }
    return this.commands.find((c) => c.shortcut === k) ?? null;
  }
}
