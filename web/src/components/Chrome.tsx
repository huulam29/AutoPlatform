// Editor chrome: collapsible side panels, the floating add-node button and the growing minimap.
// The state logic lives in ../lib/ui.ts (tested); this file wires it to React and the DOM.
import { Icon, type IconName } from "./Icon";
import { MiniMap, Panel, useReactFlow, useStore } from "@xyflow/react";
import {
  useCallback,
  useEffect,
  useId,
  useReducer,
  useRef,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import { NODE_TYPES, type NodeType } from "../types";
import {
  MENU_CLOSED,
  MINIMAP_COLLAPSED,
  NARROW_MAX_WIDTH,
  PanelStore,
  isAddNodeShortcut,
  keyLike,
  keepCenter,
  menuReducer,
  miniMapReducer,
  type PanelName,
  type Size,
} from "../lib/ui";

/* ------------------------------------------------------------------ panels */

const NARROW_QUERY = `(max-width: ${NARROW_MAX_WIDTH}px)`;

function safeStorage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function isNarrow(): boolean {
  try {
    return window.matchMedia(NARROW_QUERY).matches;
  } catch {
    return false;
  }
}

export const panels = new PanelStore(safeStorage, isNarrow());

/** Call once (in App): tracks the narrow/wide breakpoint so each width keeps its own panels. */
export function usePanelBreakpoint(): void {
  useEffect(() => {
    let mq: MediaQueryList;
    try {
      mq = window.matchMedia(NARROW_QUERY);
    } catch {
      return;
    }
    const on = () => panels.setNarrow(mq.matches);
    on();
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
}

export function usePanelCollapsed(name: PanelName): boolean {
  return useSyncExternalStore(panels.subscribe, () => panels.collapsed(name));
}

function Chevron({ dir }: { dir: "left" | "right" }) {
  return (
    <svg className={`chev chev-${dir}`} width="14" height="14" viewBox="0 0 16 16" aria-hidden>
      <path
        d={dir === "left" ? "M10 3 5 8l5 5" : "M6 3l5 5-5 5"}
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** The collapse button that sits in an expanded panel's header. */
export function CollapseButton({
  name,
  side,
  label,
}: {
  name: PanelName;
  side: "left" | "right";
  label: string;
}) {
  return (
    <button
      type="button"
      className="icon-btn"
      aria-label={`Collapse ${label}`}
      aria-expanded
      title={`Collapse ${label} (\\ toggles both panels)`}
      onClick={() => panels.set(name, true)}
    >
      <Chevron dir={side} />
    </button>
  );
}

/**
 * A side panel that collapses to a thin rail. `attention` puts a pulsing dot on the rail (e.g. a
 * node got selected while the inspector was collapsed); the caller clears it on expand.
 */
export function SidePanel({
  name,
  side,
  label,
  className,
  attention,
  children,
}: {
  name: PanelName;
  side: "left" | "right";
  label: string;
  className: string;
  attention?: boolean;
  children: ReactNode;
}) {
  const collapsed = usePanelCollapsed(name);
  if (!collapsed)
    return (
      <aside className={`${className} panel-${side}`} aria-label={label}>
        {children}
      </aside>
    );
  return (
    <aside className={`rail rail-${side}`} aria-label={`${label} (collapsed)`}>
      <button
        type="button"
        className="icon-btn rail-btn"
        aria-label={`Expand ${label}`}
        aria-expanded={false}
        title={`Expand ${label}`}
        onClick={() => panels.set(name, false)}
      >
        <Chevron dir={side === "left" ? "right" : "left"} />
        {attention && <span className="rail-dot" aria-label="new selection" role="status" />}
      </button>
      <span className="rail-label" aria-hidden>
        {label}
      </span>
    </aside>
  );
}

/** A header strip with a title and the collapse button, for panels without their own header. */
export function PanelHeader(props: { name: PanelName; side: "left" | "right"; label: string }) {
  return (
    <div className="side-head">
      <span className="side-title">{props.label}</span>
      <CollapseButton {...props} />
    </div>
  );
}

/* ------------------------------------------------------- keep view centered */

/** Inside a <ReactFlow>: when the pane resizes (a panel opens or closes), keep the graph point
 * at the center of the pane where it was instead of letting the graph slide sideways. */
export function KeepCenterOnResize() {
  const flow = useReactFlow();
  const width = useStore((s) => s.width);
  const height = useStore((s) => s.height);
  const prev = useRef<Size>({ width: 0, height: 0 });
  useEffect(() => {
    const next = { width, height };
    const v = flow.getViewport();
    const moved = keepCenter(v, prev.current, next);
    prev.current = next;
    if (moved !== v) void flow.setViewport(moved);
  }, [width, height, flow]);
  return null;
}

/* ----------------------------------------------------------------- minimap */

/** React Flow's MiniMap, small and translucent until hovered, focused or tapped. */
export function GrowingMiniMap() {
  const [s, dispatch] = useReducer(miniMapReducer, MINIMAP_COLLAPSED);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!s.expanded) return;
    const on = (e: PointerEvent) => {
      if (!ref.current?.contains(e.target as Node)) dispatch({ type: "down-outside" });
    };
    document.addEventListener("pointerdown", on, true);
    return () => document.removeEventListener("pointerdown", on, true);
  }, [s.expanded]);
  return (
    <Panel
      ref={ref}
      position="bottom-right"
      className={`minimap-wrap ${s.expanded ? "expanded" : ""}`}
      tabIndex={0}
      role="group"
      aria-label="Minimap: hover, focus or tap to enlarge; drag inside to pan"
      data-expanded={s.expanded}
      onPointerEnter={(e) => dispatch({ type: "enter", pointer: e.pointerType })}
      onPointerLeave={(e) => dispatch({ type: "leave", pointer: e.pointerType })}
      onPointerDown={(e) => dispatch({ type: "down-inside", pointer: e.pointerType })}
      onFocus={(e) =>
        dispatch({ type: "focus", keyboard: e.currentTarget.matches(":focus-visible") })
      }
      onBlur={() => dispatch({ type: "blur" })}
      onKeyDown={(e) => e.key === "Escape" && dispatch({ type: "escape" })}
    >
      <MiniMap pannable zoomable />
    </Panel>
  );
}

/* ---------------------------------------------------------------- add node */

const TYPE_HELP: Record<NodeType, string> = {
  agent: "an AI agent step",
  action: "a built-in action (Jira, GitHub, shell, deploy)",
  router: "branch on a value or an LLM decision",
  gate: "wait for a human approval",
  wait: "wait for an external event",
  chat: "a gatekeeper you talk to: takes the request, then reports (the start point)",
};

/**
 * The round `+` button floating on the canvas and its menu of node types. Picking a type (click,
 * Enter, or dragging it onto the canvas) calls `onAdd`. `A`/`N` open it while the canvas, or
 * nothing in particular, has focus; `canvasRef` scopes that.
 */
export function AddNodeButton({
  onAdd,
  disabled,
  dndType,
  canvasRef,
}: {
  onAdd: (t: NodeType) => void;
  disabled: boolean;
  dndType: string;
  canvasRef: React.RefObject<HTMLElement>;
}) {
  const count = NODE_TYPES.length;
  const [s, dispatch] = useReducer(
    (st: typeof MENU_CLOSED, e: Parameters<typeof menuReducer>[1]) => menuReducer(st, e, count),
    MENU_CLOSED,
  );
  const wrap = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const items = useRef<(HTMLElement | null)[]>([]);
  const menuId = useId();

  const close = useCallback((refocus: boolean) => {
    dispatch({ type: "close" });
    if (refocus) button.current?.focus();
  }, []);

  // A / N while the canvas (or the page itself) has focus
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      if (disabled || s.open || !isAddNodeShortcut(keyLike(e))) return;
      const active = document.activeElement;
      const onCanvas =
        !active || active === document.body || !!canvasRef.current?.contains(active);
      if (!onCanvas) return;
      e.preventDefault();
      dispatch({ type: "open" });
    };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, [disabled, s.open, canvasRef]);

  // outside click closes
  useEffect(() => {
    if (!s.open) return;
    const on = (e: PointerEvent) => {
      if (!wrap.current?.contains(e.target as Node)) dispatch({ type: "close" });
    };
    document.addEventListener("pointerdown", on, true);
    return () => document.removeEventListener("pointerdown", on, true);
  }, [s.open]);

  // roving focus inside the open menu
  useEffect(() => {
    if (s.open) items.current[s.active]?.focus();
  }, [s.open, s.active]);

  const choose = (t: NodeType) => {
    dispatch({ type: "choose" });
    button.current?.focus();
    onAdd(t);
  };

  return (
    <div className="add-node" ref={wrap}>
      <button
        ref={button}
        type="button"
        className="fab"
        aria-label="Add node"
        aria-haspopup="menu"
        aria-expanded={s.open}
        aria-controls={s.open ? menuId : undefined}
        title={disabled ? "Save or discard the YAML tab first" : "Add node (A)"}
        disabled={disabled}
        onClick={() => dispatch({ type: "toggle" })}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") {
            e.preventDefault();
            dispatch({ type: "open" });
          }
        }}
      >
        <svg width="18" height="18" viewBox="0 0 16 16" aria-hidden>
          <path d="M8 3v10M3 8h10" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
        </svg>
      </button>
      {s.open && (
        <div
          className="add-menu"
          role="menu"
          id={menuId}
          aria-label="Add node"
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              e.preventDefault();
              e.stopPropagation();
              close(true);
            } else if (e.key === "ArrowDown") {
              e.preventDefault();
              dispatch({ type: "move", by: 1 });
            } else if (e.key === "ArrowUp") {
              e.preventDefault();
              dispatch({ type: "move", by: -1 });
            } else if (e.key === "Home") {
              e.preventDefault();
              dispatch({ type: "first" });
            } else if (e.key === "End") {
              e.preventDefault();
              dispatch({ type: "last" });
            } else if (e.key === "Tab") {
              close(false);
            }
          }}
        >
          <div className="add-menu-head">Add node</div>
          {NODE_TYPES.map((t, i) => (
            <div
              key={t}
              ref={(el) => {
                items.current[i] = el;
              }}
              role="menuitem"
              tabIndex={i === s.active ? 0 : -1}
              className={`add-item tag-${t}`}
              draggable
              title="Click, or drag onto the canvas"
              onDragStart={(e) => {
                e.dataTransfer.setData(dndType, t);
                e.dataTransfer.effectAllowed = "copy";
              }}
              onDragEnd={() => dispatch({ type: "close" })}
              onClick={() => choose(t)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  choose(t);
                }
              }}
            >
              <span className="add-item-name">
                <Icon name={t as IconName} />
                {t}
              </span>
              <span className="add-item-help">{TYPE_HELP[t]}</span>
            </div>
          ))}
          <div className="add-menu-foot">
            <kbd>A</kbd> opens this · <kbd>Esc</kbd> closes
          </div>
        </div>
      )}
    </div>
  );
}
