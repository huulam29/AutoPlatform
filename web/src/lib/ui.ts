// Pure UI state for the editor chrome: collapsible panels (persisted), the growing minimap and
// the add-node menu. No React and no DOM globals here, so it all runs under vitest in node.

/* ------------------------------------------------------------------ panels */

export type PanelName = "pipelines" | "inspector" | "runs" | "run-detail" | "assistant";

/** Panels that start collapsed even on wide viewports (opened on demand). */
const START_COLLAPSED: ReadonlySet<PanelName> = new Set<PanelName>(["assistant"]);

/** Viewports narrower than this start with every panel collapsed. */
export const NARROW_MAX_WIDTH = 767;

/** Narrow and wide viewports remember their panels separately, so a panel opened on a desktop
 * doesn't come back open (and squeeze the canvas) on a phone. */
export function panelKey(name: PanelName, narrow: boolean): string {
  return `agentline.panel.${name}${narrow ? ".narrow" : ""}`;
}

/** Collapsed unless remembered otherwise; narrow viewports default to collapsed. Never throws:
 * a missing, blocked or broken localStorage falls back to the default. */
export function readCollapsed(
  storage: () => Storage | null | undefined,
  name: PanelName,
  narrow: boolean,
): boolean {
  try {
    const v = storage()?.getItem(panelKey(name, narrow));
    if (v === "1") return true;
    if (v === "0") return false;
  } catch {
    // private mode, blocked site data, quota: use the default
  }
  return narrow || START_COLLAPSED.has(name);
}

/** Returns false when the value could not be stored (the in-memory state still applies). */
export function writeCollapsed(
  storage: () => Storage | null | undefined,
  name: PanelName,
  narrow: boolean,
  collapsed: boolean,
): boolean {
  try {
    const s = storage();
    if (!s) return false;
    s.setItem(panelKey(name, narrow), collapsed ? "1" : "0");
    return true;
  } catch {
    return false;
  }
}

/** A tiny external store (for useSyncExternalStore) holding every panel's collapsed state, so
 * the app-level shortcut can toggle panels owned by different components. */
export class PanelStore {
  private state = new Map<PanelName, boolean>();
  private listeners = new Set<() => void>();
  private narrow: boolean;

  constructor(
    private storage: () => Storage | null | undefined,
    narrow: boolean,
  ) {
    this.narrow = narrow;
  }

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };

  isNarrow(): boolean {
    return this.narrow;
  }

  /** Switching between narrow and wide reloads each panel's remembered state for that width. */
  setNarrow(narrow: boolean): void {
    if (narrow === this.narrow) return;
    this.narrow = narrow;
    this.state.clear();
    this.emit();
  }

  collapsed(name: PanelName): boolean {
    let v = this.state.get(name);
    if (v === undefined) {
      v = readCollapsed(this.storage, name, this.narrow);
      this.state.set(name, v);
    }
    return v;
  }

  set(name: PanelName, collapsed: boolean): void {
    if (this.collapsed(name) === collapsed) return;
    this.state.set(name, collapsed);
    writeCollapsed(this.storage, name, this.narrow, collapsed);
    this.emit();
  }

  toggle(name: PanelName): void {
    this.set(name, !this.collapsed(name));
  }

  /** The "toggle both" shortcut: if any of them is open, collapse them all; else open them all. */
  toggleAll(names: PanelName[]): void {
    const anyOpen = names.some((n) => !this.collapsed(n));
    for (const n of names) this.set(n, anyOpen);
  }

  private emit(): void {
    for (const fn of [...this.listeners]) fn();
  }
}

/* ----------------------------------------------------------------- minimap */

export type MiniMapState = {
  expanded: boolean;
  /** what expanded it; a keyboard-focus expansion ends on blur, the others don't */
  via: "hover" | "focus" | "tap" | null;
};

export type MiniMapEvent =
  | { type: "enter"; pointer: string } // pointerenter; pointer = PointerEvent.pointerType
  | { type: "leave"; pointer: string } // pointerleave
  | { type: "down-inside"; pointer: string } // pointerdown inside the minimap
  | { type: "down-outside" } // pointerdown anywhere else
  | { type: "focus"; keyboard: boolean }
  | { type: "blur" }
  | { type: "escape" };

export const MINIMAP_COLLAPSED: MiniMapState = { expanded: false, via: null };

/**
 * Small by default; grows on mouse hover, keyboard focus, or a tap/click inside. A click or drag
 * inside (which pans the view) never collapses it. It shrinks when the mouse leaves, on a
 * pointerdown outside, on Escape, or on blur of a keyboard-focus expansion.
 *
 * Touch pointers fire pointerenter/pointerleave around every tap, so only mouse (and pen)
 * enter/leave count as hover; touch relies on down-inside / down-outside.
 */
export function miniMapReducer(s: MiniMapState, e: MiniMapEvent): MiniMapState {
  const hovering = (p: string) => p === "mouse" || p === "pen";
  switch (e.type) {
    case "enter":
      return hovering(e.pointer) && !s.expanded ? { expanded: true, via: "hover" } : s;
    case "leave":
      return hovering(e.pointer) && s.expanded ? MINIMAP_COLLAPSED : s;
    case "down-inside":
      if (s.expanded) return s;
      return { expanded: true, via: hovering(e.pointer) ? "hover" : "tap" };
    case "down-outside":
      return s.expanded ? MINIMAP_COLLAPSED : s;
    case "focus":
      return e.keyboard && !s.expanded ? { expanded: true, via: "focus" } : s;
    case "blur":
      return s.via === "focus" ? MINIMAP_COLLAPSED : s;
    case "escape":
      return s.expanded ? MINIMAP_COLLAPSED : s;
  }
}

/* --------------------------------------------------------------- add menu */

export type MenuState = { open: boolean; active: number };

export type MenuEvent =
  | { type: "toggle" }
  | { type: "open" }
  | { type: "close" } // Escape, outside click, focus left the menu
  | { type: "choose" } // an item was picked (click, Enter, drop on the canvas)
  | { type: "move"; by: number } // ArrowDown (+1) / ArrowUp (-1), wraps
  | { type: "first" }
  | { type: "last" };

export const MENU_CLOSED: MenuState = { open: false, active: 0 };

export function menuReducer(s: MenuState, e: MenuEvent, count: number): MenuState {
  switch (e.type) {
    case "toggle":
      return s.open ? MENU_CLOSED : { open: true, active: 0 };
    case "open":
      return s.open ? s : { open: true, active: 0 };
    case "close":
    case "choose":
      return s.open ? MENU_CLOSED : s;
    case "move":
      if (!s.open || count <= 0) return s;
      return { open: true, active: (((s.active + e.by) % count) + count) % count };
    case "first":
      return s.open ? { open: true, active: 0 } : s;
    case "last":
      return s.open ? { open: true, active: Math.max(0, count - 1) } : s;
  }
}

/* --------------------------------------------------------------- shortcuts */

export type KeyLike = {
  key: string;
  metaKey?: boolean;
  ctrlKey?: boolean;
  altKey?: boolean;
  /** the focused element; shortcuts are off while typing */
  target?: { tagName?: string; isContentEditable?: boolean } | null;
};

/** A KeyboardEvent's fields as a plain object. (`{...event}` doesn't work: `key`, `metaKey` and
 * friends are prototype getters, so a spread copies none of them.) */
export function keyLike(e: {
  key: string;
  metaKey: boolean;
  ctrlKey: boolean;
  altKey: boolean;
  target: unknown;
}): KeyLike {
  const t = e.target as { tagName?: unknown; isContentEditable?: unknown } | null;
  return {
    key: e.key,
    metaKey: e.metaKey,
    ctrlKey: e.ctrlKey,
    altKey: e.altKey,
    target: t
      ? {
          tagName: typeof t.tagName === "string" ? t.tagName : undefined,
          isContentEditable: t.isContentEditable === true,
        }
      : null,
  };
}

export function isTyping(target: KeyLike["target"]): boolean {
  if (!target) return false;
  const tag = (target.tagName ?? "").toUpperCase();
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || !!target.isContentEditable;
}

/** `A` or `N` (no modifiers, not while typing) opens the add-node menu. */
export function isAddNodeShortcut(e: KeyLike): boolean {
  if (e.metaKey || e.ctrlKey || e.altKey || isTyping(e.target)) return false;
  return e.key === "a" || e.key === "A" || e.key === "n" || e.key === "N";
}

/** `\` (not while typing) or Cmd/Ctrl + `.` (anywhere) toggles the side panels together. */
export function isTogglePanelsShortcut(e: KeyLike): boolean {
  if (e.key === "." && (e.metaKey || e.ctrlKey) && !e.altKey) return true;
  return e.key === "\\" && !e.metaKey && !e.ctrlKey && !e.altKey && !isTyping(e.target);
}

/* ------------------------------------------------------------ canvas resize */

export type Viewport = { x: number; y: number; zoom: number };
export type Size = { width: number; height: number };

/** When the canvas changes size (a side panel opens or closes, the window resizes), shift the
 * viewport so the graph point at the center of the canvas stays at the center. The first
 * measurement (from 0) is left alone: fitView handles that. */
export function keepCenter(v: Viewport, prev: Size, next: Size): Viewport {
  if (prev.width <= 0 || prev.height <= 0 || next.width <= 0 || next.height <= 0) return v;
  if (prev.width === next.width && prev.height === next.height) return v;
  return {
    x: v.x + (next.width - prev.width) / 2,
    y: v.y + (next.height - prev.height) / 2,
    zoom: v.zoom,
  };
}
