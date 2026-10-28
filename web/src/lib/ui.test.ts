import { describe, expect, it } from "vitest";
import {
  MENU_CLOSED,
  MINIMAP_COLLAPSED,
  PanelStore,
  isAddNodeShortcut,
  isTogglePanelsShortcut,
  keepCenter,
  keyLike,
  menuReducer,
  miniMapReducer,
  panelKey,
  readCollapsed,
  writeCollapsed,
  type MiniMapEvent,
  type MiniMapState,
} from "./ui";

function memoryStorage(): Storage {
  const m = new Map<string, string>();
  return {
    get length() {
      return m.size;
    },
    clear: () => m.clear(),
    getItem: (k) => m.get(k) ?? null,
    key: (i) => [...m.keys()][i] ?? null,
    removeItem: (k) => void m.delete(k),
    setItem: (k, v) => void m.set(k, String(v)),
  };
}

const throwingStorage = (): Storage => {
  const s = memoryStorage();
  s.getItem = () => {
    throw new Error("SecurityError");
  };
  s.setItem = () => {
    throw new Error("QuotaExceededError");
  };
  return s;
};

describe("panel persistence", () => {
  it("defaults to open on wide and collapsed on narrow viewports", () => {
    const s = memoryStorage();
    expect(readCollapsed(() => s, "inspector", false)).toBe(false);
    expect(readCollapsed(() => s, "inspector", true)).toBe(true);
  });

  it("round-trips per panel and per width", () => {
    const s = memoryStorage();
    expect(writeCollapsed(() => s, "inspector", false, true)).toBe(true);
    expect(readCollapsed(() => s, "inspector", false)).toBe(true);
    expect(readCollapsed(() => s, "pipelines", false)).toBe(false); // other panel untouched
    expect(readCollapsed(() => s, "inspector", true)).toBe(true); // narrow: still its default
    writeCollapsed(() => s, "inspector", true, false);
    expect(readCollapsed(() => s, "inspector", true)).toBe(false);
    expect(s.getItem(panelKey("inspector", false))).toBe("1");
    expect(s.getItem(panelKey("inspector", true))).toBe("0");
  });

  it("ignores garbage values", () => {
    const s = memoryStorage();
    s.setItem(panelKey("runs", false), "maybe");
    expect(readCollapsed(() => s, "runs", false)).toBe(false);
  });

  it("survives a storage that throws or is missing", () => {
    expect(readCollapsed(throwingStorage, "runs", false)).toBe(false);
    expect(readCollapsed(throwingStorage, "runs", true)).toBe(true);
    expect(writeCollapsed(throwingStorage, "runs", false, true)).toBe(false);
    const accessorThrows = () => {
      throw new Error("localStorage is not available");
    };
    expect(readCollapsed(accessorThrows, "runs", false)).toBe(false);
    expect(writeCollapsed(accessorThrows, "runs", false, true)).toBe(false);
    expect(readCollapsed(() => null, "runs", true)).toBe(true);
    expect(writeCollapsed(() => undefined, "runs", true, false)).toBe(false);
  });
});

describe("panel store", () => {
  it("toggles, persists and notifies", () => {
    const s = memoryStorage();
    const store = new PanelStore(() => s, false);
    let calls = 0;
    const off = store.subscribe(() => calls++);
    store.toggle("inspector");
    expect(store.collapsed("inspector")).toBe(true);
    expect(s.getItem(panelKey("inspector", false))).toBe("1");
    store.set("inspector", true); // no change, no notification
    expect(calls).toBe(1);
    off();
    store.toggle("inspector");
    expect(calls).toBe(1);
    expect(new PanelStore(() => s, false).collapsed("inspector")).toBe(false);
  });

  it("toggleAll collapses everything if any panel is open, else opens all", () => {
    const store = new PanelStore(memoryStorage, false);
    store.set("pipelines", true); // one open, one collapsed
    store.toggleAll(["pipelines", "inspector"]);
    expect(store.collapsed("pipelines")).toBe(true);
    expect(store.collapsed("inspector")).toBe(true);
    store.toggleAll(["pipelines", "inspector"]);
    expect(store.collapsed("pipelines")).toBe(false);
    expect(store.collapsed("inspector")).toBe(false);
  });

  it("keeps working in memory when storage throws", () => {
    const store = new PanelStore(throwingStorage, false);
    store.toggle("runs");
    expect(store.collapsed("runs")).toBe(true);
  });

  it("switching width reloads that width's state", () => {
    const s = memoryStorage();
    const store = new PanelStore(() => s, false);
    store.set("inspector", true);
    let calls = 0;
    store.subscribe(() => calls++);
    store.setNarrow(true);
    expect(calls).toBe(1);
    expect(store.collapsed("pipelines")).toBe(true); // narrow default
    store.set("pipelines", false);
    store.setNarrow(false);
    expect(store.collapsed("inspector")).toBe(true);
    expect(store.collapsed("pipelines")).toBe(false);
  });
});

describe("minimap state", () => {
  const run = (events: MiniMapEvent[], from: MiniMapState = MINIMAP_COLLAPSED) =>
    events.reduce(miniMapReducer, from);

  it("grows on mouse hover and shrinks when the mouse leaves", () => {
    const s = run([{ type: "enter", pointer: "mouse" }]);
    expect(s.expanded).toBe(true);
    expect(run([{ type: "leave", pointer: "mouse" }], s).expanded).toBe(false);
  });

  it("a click or drag inside keeps it expanded", () => {
    const s = run([
      { type: "enter", pointer: "mouse" },
      { type: "down-inside", pointer: "mouse" },
      { type: "down-inside", pointer: "mouse" },
    ]);
    expect(s.expanded).toBe(true);
  });

  it("an outside click shrinks it", () => {
    const s = run([{ type: "enter", pointer: "mouse" }, { type: "down-outside" }]);
    expect(s.expanded).toBe(false);
  });

  it("touch: a tap expands it, enter/leave around taps are ignored, outside tap shrinks", () => {
    let s = run([
      { type: "enter", pointer: "touch" },
      { type: "down-inside", pointer: "touch" },
      { type: "leave", pointer: "touch" }, // fires after every touch lift
    ]);
    expect(s).toEqual({ expanded: true, via: "tap" });
    s = run(
      [
        { type: "enter", pointer: "touch" },
        { type: "down-inside", pointer: "touch" }, // a second tap pans, stays big
        { type: "leave", pointer: "touch" },
      ],
      s,
    );
    expect(s.expanded).toBe(true);
    expect(run([{ type: "down-outside" }], s).expanded).toBe(false);
  });

  it("touch enter alone doesn't expand it", () => {
    expect(run([{ type: "enter", pointer: "touch" }]).expanded).toBe(false);
  });

  it("keyboard focus expands it; blur and Escape shrink it", () => {
    const s = run([{ type: "focus", keyboard: true }]);
    expect(s).toEqual({ expanded: true, via: "focus" });
    expect(run([{ type: "blur" }], s).expanded).toBe(false);
    expect(run([{ type: "escape" }], s).expanded).toBe(false);
  });

  it("mouse focus (from a click) doesn't count as keyboard focus, and blur keeps a hover", () => {
    expect(run([{ type: "focus", keyboard: false }]).expanded).toBe(false);
    const s = run([{ type: "enter", pointer: "mouse" }, { type: "blur" }]);
    expect(s.expanded).toBe(true);
  });
});

describe("add-node menu state", () => {
  const r = (s: typeof MENU_CLOSED, e: Parameters<typeof menuReducer>[1]) => menuReducer(s, e, 5);

  it("toggles open and closed", () => {
    const open = r(MENU_CLOSED, { type: "toggle" });
    expect(open).toEqual({ open: true, active: 0 });
    expect(r(open, { type: "toggle" }).open).toBe(false);
  });

  it("open is idempotent; close and choose close it", () => {
    const open = r(MENU_CLOSED, { type: "open" });
    const moved = r(open, { type: "move", by: 2 });
    expect(r(moved, { type: "open" })).toBe(moved);
    expect(r(moved, { type: "close" })).toEqual(MENU_CLOSED);
    expect(r(moved, { type: "choose" })).toEqual(MENU_CLOSED);
    expect(r(MENU_CLOSED, { type: "close" })).toBe(MENU_CLOSED);
  });

  it("arrow keys wrap; Home/End jump", () => {
    const open = r(MENU_CLOSED, { type: "open" });
    expect(r(open, { type: "move", by: -1 }).active).toBe(4);
    expect(r({ open: true, active: 4 }, { type: "move", by: 1 }).active).toBe(0);
    expect(r(open, { type: "last" }).active).toBe(4);
    expect(r({ open: true, active: 3 }, { type: "first" }).active).toBe(0);
    expect(r(MENU_CLOSED, { type: "move", by: 1 })).toBe(MENU_CLOSED);
  });
});

describe("shortcuts", () => {
  const input = { tagName: "INPUT" };
  const div = { tagName: "DIV" };

  it("A / N open the add menu, not while typing or with modifiers", () => {
    expect(isAddNodeShortcut({ key: "a", target: div })).toBe(true);
    expect(isAddNodeShortcut({ key: "N", target: div })).toBe(true);
    expect(isAddNodeShortcut({ key: "a", target: input })).toBe(false);
    expect(isAddNodeShortcut({ key: "a", target: { tagName: "TEXTAREA" } })).toBe(false);
    expect(isAddNodeShortcut({ key: "a", target: { tagName: "DIV", isContentEditable: true } })).toBe(
      false,
    );
    expect(isAddNodeShortcut({ key: "a", metaKey: true, target: div })).toBe(false);
    expect(isAddNodeShortcut({ key: "b", target: div })).toBe(false);
  });

  it("backslash or Cmd/Ctrl+. toggles the panels", () => {
    expect(isTogglePanelsShortcut({ key: "\\", target: div })).toBe(true);
    expect(isTogglePanelsShortcut({ key: "\\", target: input })).toBe(false);
    expect(isTogglePanelsShortcut({ key: ".", metaKey: true, target: input })).toBe(true);
    expect(isTogglePanelsShortcut({ key: ".", ctrlKey: true })).toBe(true);
    expect(isTogglePanelsShortcut({ key: ".", target: div })).toBe(false);
  });
});

describe("keepCenter", () => {
  const v = { x: 10, y: 20, zoom: 0.8 };
  it("shifts by half the size change so the center stays put", () => {
    expect(keepCenter(v, { width: 600, height: 400 }, { width: 940, height: 400 })).toEqual({
      x: 180,
      y: 20,
      zoom: 0.8,
    });
    expect(keepCenter(v, { width: 600, height: 400 }, { width: 600, height: 300 }).y).toBe(-30);
  });
  it("leaves the first measurement and no-ops alone", () => {
    expect(keepCenter(v, { width: 0, height: 0 }, { width: 600, height: 400 })).toBe(v);
    expect(keepCenter(v, { width: 600, height: 400 }, { width: 600, height: 400 })).toBe(v);
  });
});

describe("keyLike", () => {
  it("reads prototype getters that an object spread would miss", () => {
    class FakeEvent {
      get key() {
        return "\\";
      }
      get metaKey() {
        return false;
      }
      get ctrlKey() {
        return false;
      }
      get altKey() {
        return false;
      }
      get target(): unknown {
        return { tagName: "TEXTAREA", isContentEditable: false };
      }
    }
    const e = new FakeEvent();
    expect(({ ...e } as { key?: string }).key).toBeUndefined(); // why keyLike exists
    const k = keyLike(e);
    expect(k.key).toBe("\\");
    expect(isTogglePanelsShortcut(k)).toBe(false); // typing in a textarea
    const onCanvas = { key: "\\", metaKey: false, ctrlKey: false, altKey: false, target: null };
    expect(isTogglePanelsShortcut(keyLike(onCanvas))).toBe(true);
  });
});
