import { useEffect, useMemo, useState } from "react";
import { AUTH_NEEDED_EVENT, api } from "./api";
import { Assistant } from "./components/Assistant";
import { CollapseButton, SidePanel, panels, usePanelBreakpoint, usePanelCollapsed } from "./components/Chrome";
import { Icon } from "./components/Icon";
import { Approvals } from "./components/Approvals";
import { Costs } from "./components/Costs";
import { Insights } from "./components/Insights";
import { Logs } from "./components/Logs";
import { Problems } from "./components/Problems";
import { Editor } from "./components/Editor";
import { ImportButton } from "./components/ImportExport";
import { RunList, RunView } from "./components/RunView";
import { Settings } from "./components/Settings";
import { SetupDialog } from "./components/SetupDialog";
import { SETTINGS_CHANGED_EVENT } from "./lib/ai";
import { assistantInbox } from "./lib/assistant";
import { isTogglePanelsShortcut, keyLike, type PanelName } from "./lib/ui";
import type { NodeTypes, PipelineSummary } from "./types";
import { AppReports } from "./components/AppReports";
import { LessonsPage } from "./components/Review";
import { CommandPalette } from "./components/CommandPalette";
import { EngineBanner } from "./components/EngineBanner";
import { SignIn } from "./components/SignIn";
import { setBadge } from "./lib/badge";
import { buildCommands, dialogOpen, isTypingTarget, SequenceKeys } from "./lib/commands";
import { REFRESH_EVENT, refresh, restartEngine, useEngineHealth } from "./lib/engine";
import { isMacApp } from "./lib/native";
import { startNotifier } from "./lib/notify";

// Hash routes: #/pipelines/<name>, #/runs/<id>, #/lessons, #/settings
type Route =
  | { view: "pipelines"; name?: string }
  | { view: "runs"; id?: string }
  | { view: "settings" }
  | { view: "costs" }
  | { view: "insights" }
  | { view: "approvals" }
  | { view: "problems"; issue?: number }
  | { view: "logs" }
  | { view: "lessons" }
  | { view: "app-reports" }
  | { view: "fix"; kind: string; id: string };

function parse(hash: string): Route {
  const [, view, ...rest] = hash.replace(/^#/, "").split("?")[0].split("/");
  const arg = rest.length ? decodeURIComponent(rest.join("/")) : undefined;
  if (view === "runs") return { view: "runs", id: arg };
  if (view === "settings") return { view: "settings" };
  if (view === "costs") return { view: "costs" };
  if (view === "insights") return { view: "insights" };
  if (view === "approvals") return { view: "approvals" };
  if (view === "problems") return { view: "problems", issue: arg && /^\d+$/.test(arg) ? Number(arg) : undefined };
  if (view === "logs") return { view: "logs" };
  if (view === "lessons") return { view: "lessons" };
  if (view === "app-reports") return { view: "app-reports" };
  if (view === "fix") return { view: "fix", kind: rest[0] ?? "", id: decodeURIComponent(rest.slice(1).join("/")) };
  return { view: "pipelines", name: arg };
}

function useRoute(): Route {
  const [route, setRoute] = useState(() => parse(window.location.hash));
  useEffect(() => {
    const on = () => setRoute(parse(window.location.hash));
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return route;
}

const ROUTE_PANELS: Record<Route["view"], PanelName[]> = {
  fix: [],
  costs: [],
  insights: [],
  approvals: [],
  problems: [],
  logs: [],
  lessons: [],
  "app-reports": [],
  pipelines: ["pipelines", "inspector"],
  runs: ["runs", "run-detail"],
  settings: [],
};

const isMac = typeof navigator !== "undefined" && /Mac/.test(navigator.platform);

const go = (path: string) => {
  window.location.hash = path;
};

/** Sign-in gate: while the engine refuses this browser's token, only <SignIn> shows. */
export function App() {
  const [signIn, setSignIn] = useState(false);
  const [session, setSession] = useState(0); // remounts Studio after a sign-in
  useEffect(() => {
    const on = () => setSignIn(true);
    window.addEventListener(AUTH_NEEDED_EVENT, on);
    return () => window.removeEventListener(AUTH_NEEDED_EVENT, on);
  }, []);
  // a browser (not the Mac app): the service worker routes notification clicks; notifications
  // start if they were turned on in Settings
  useEffect(() => {
    if (isMacApp()) return;
    if ("serviceWorker" in navigator && window.isSecureContext) {
      void navigator.serviceWorker.register("/sw.js").catch(() => {});
    }
    startNotifier();
  }, []);
  if (signIn)
    return (
      <SignIn
        onDone={() => {
          setSignIn(false);
          setSession((n) => n + 1);
        }}
      />
    );
  return <Studio key={session} />;
}

function Studio() {
  const route = useRoute();
  const engine = useEngineHealth();
  const [palette, setPalette] = useState(false);
  const [pipelines, setPipelines] = useState<PipelineSummary[]>([]);
  const [types, setTypes] = useState<NodeTypes | null>(null);
  const [error, setError] = useState<string | null>(null);
  // node types carry the harnesses and models for the pickers: reload them after Settings saves
  const [typesVersion, setTypesVersion] = useState(0);
  // a harness (or other setting) saved from a picker popup: reload what the pickers list
  useEffect(() => {
    const bump = () => setTypesVersion((v) => v + 1);
    window.addEventListener(SETTINGS_CHANGED_EVENT, bump);
    return () => window.removeEventListener(SETTINGS_CHANGED_EVENT, bump);
  }, []);
  const [pipelinesVersion, setPipelinesVersion] = useState(0);
  useEffect(() => {
    api
      .nodeTypes()
      .then((t) => {
        setTypes(t);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  }, [typesVersion]);
  // the engine came back, or Refresh: reload what this page shows
  const [refreshVersion, setRefreshVersion] = useState(0);
  useEffect(() => {
    const on = () => {
      setTypesVersion((v) => v + 1);
      setPipelinesVersion((v) => v + 1);
      setRefreshVersion((v) => v + 1);
    };
    window.addEventListener(REFRESH_EVENT, on);
    return () => window.removeEventListener(REFRESH_EVENT, on);
  }, []);
  useEffect(() => {
    if (route.view !== "pipelines") return;
    api
      .pipelines()
      .then((r) => setPipelines(r.pipelines))
      .catch((e: Error) => setError(e.message));
  }, [route, pipelinesVersion]);

  // #/fix/issue/<n> or #/fix/run/<id>: open the pipeline and ask the assistant to fix it
  useEffect(() => {
    if (route.view !== "fix") return;
    const req = route.kind === "run" ? api.runFix(route.id) : api.issueFix(Number(route.id));
    req
      .then((r) => {
        panels.set("assistant", false);
        assistantInbox.push(r.prompt);
        go(r.pipeline ? `/pipelines/${r.pipeline}` : "/pipelines");
      })
      .catch((e: Error) => {
        setError(`Couldn't prepare the fix request: ${e.message}`);
        go("/pipelines");
      });
  }, [route]);

  // badges: approvals waiting, problems open
  const [counts, setCounts] = useState({ approvals: 0, problems: 0 });
  useEffect(() => {
    let live = true;
    const tick = () =>
      Promise.all([api.approvals(), api.issues()])
        .then(([a, i]) => {
          if (!live) return;
          const open = i.issues.filter((x) => x.status === "open" || x.status === "in_progress").length;
          setCounts({ approvals: a.length, problems: open });
        })
        .catch(() => {});
    void tick();
    const t = setInterval(tick, 5000);
    return () => {
      live = false;
      clearInterval(t);
    };
  }, [route.view, refreshVersion]);
  useEffect(() => setBadge(counts.approvals), [counts.approvals]);

  // the Mac app's menu actions: the command palette (Cmd/Ctrl+K) and g-sequences
  const commands = useMemo(
    () =>
      buildCommands({
        go,
        refresh,
        restartEngine: () => void restartEngine().catch((e: Error) => setError(e.message)),
        toggleChat: () => panels.set("assistant", !panels.collapsed("assistant")),
        chatKey: isMac ? "⌘J" : "Ctrl+J",
      }),
    [],
  );
  useEffect(() => {
    const seq = new SequenceKeys(commands);
    const on = (e: KeyboardEvent) => {
      const mod = e.metaKey || e.ctrlKey;
      if (mod && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "k") {
        if (!palette && dialogOpen()) return;
        e.preventDefault();
        setPalette((p) => !p);
        return;
      }
      if (palette || mod || e.altKey || e.key.length !== 1) return;
      if (isTypingTarget(e.target as Element | null) || isTypingTarget(document.activeElement) || dialogOpen()) return;
      const c = seq.press(e.key);
      if (c) {
        e.preventDefault();
        c.run();
      }
    };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, [commands, palette]);

  const chatOpen = !usePanelCollapsed("assistant");
  // Cmd/Ctrl+J opens or closes the Chat from any page
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      if (e.key.toLowerCase() !== "j" || !(e.metaKey || e.ctrlKey) || e.altKey || e.shiftKey) return;
      e.preventDefault();
      panels.set("assistant", !panels.collapsed("assistant"));
    };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, []);

  usePanelBreakpoint();
  // `\` or Cmd/Ctrl+. hides or shows this view's side panels together
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      const names = ROUTE_PANELS[route.view];
      if (!names.length || !isTogglePanelsShortcut(keyLike(e)))
        return;
      e.preventDefault();
      panels.toggleAll(names);
    };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, [route.view]);

  return (
    <div className="app">
      <header className="top">
        <span className="logo">Agentline Studio</span>
        <nav>
          <a className={route.view === "pipelines" ? "on" : ""} href="#/pipelines">
            Pipelines
          </a>
          <a className={route.view === "runs" ? "on" : ""} href="#/runs">
            Runs
          </a>
          <a className={route.view === "approvals" ? "on" : ""} href="#/approvals">
            Approvals{counts.approvals > 0 && <span className="nav-badge">{counts.approvals}</span>}
          </a>
          <a className={route.view === "problems" ? "on" : ""} href="#/problems">
            Problems{counts.problems > 0 && <span className="nav-badge">{counts.problems}</span>}
          </a>
          <a className={route.view === "lessons" ? "on" : ""} href="#/lessons" title="What run reviews learned about each pipeline">
            Lessons
          </a>
          <a className={route.view === "insights" ? "on" : ""} href="#/insights" title="Trends of finished runs: time, cost, failures, ratings">
            Insights
          </a>
          <a className={route.view === "costs" ? "on" : ""} href="#/costs">
            Costs
          </a>
          <a className={route.view === "logs" ? "on" : ""} href="#/logs">
            Logs
          </a>
          <a className={route.view === "settings" ? "on" : ""} href="#/settings">
            Settings
          </a>
        </nav>
        <button
          type="button"
          className="palette-toggle"
          title={`Commands (${isMac ? "⌘" : "Ctrl+"}K)`}
          onClick={() => setPalette(true)}
        >
          <Icon name="search" />
          <kbd>{isMac ? "⌘K" : "Ctrl+K"}</kbd>
        </button>
        <button
          type="button"
          className={`chat-toggle ${chatOpen ? "on" : ""}`}
          aria-pressed={chatOpen}
          aria-controls={chatOpen ? "chat-panel" : undefined}
          title={`${chatOpen ? "Hide" : "Show"} the Chat (${isMac ? "⌘" : "Ctrl+"}J)`}
          onClick={() => panels.set("assistant", chatOpen)}
        >
          <Icon name="chat" />
          Chat
        </button>
      </header>
      <EngineBanner state={engine} />
      {error && engine === "up" && <div className="banner banner-error">{error}</div>}
      {palette && <CommandPalette commands={commands} onClose={() => setPalette(false)} />}
      <SetupDialog onDone={() => setTypesVersion((v) => v + 1)} />
      <main className="main">
        {route.view === "settings" ? (
          <section className="content">
            <Settings onChanged={() => setTypesVersion((v) => v + 1)} />
          </section>
        ) : route.view === "approvals" ? (
          <section className="content">
            <Approvals onChanged={() => setCounts((c) => ({ ...c, approvals: Math.max(0, c.approvals - 1) }))} />
          </section>
        ) : route.view === "problems" ? (
          <section className="content">
            <Problems issue={route.issue} />
          </section>
        ) : route.view === "app-reports" ? (
          <section className="content">
            <AppReports />
          </section>
        ) : route.view === "lessons" ? (
          <section className="content">
            <LessonsPage />
          </section>
        ) : route.view === "logs" ? (
          <section className="content">
            <Logs />
          </section>
        ) : route.view === "insights" ? (
          <section className="content">
            <Insights />
          </section>
        ) : route.view === "costs" ? (
          <section className="content">
            <Costs />
          </section>
        ) : route.view === "fix" ? (
          <p className="muted pad">Preparing the fix request…</p>
        ) : route.view === "pipelines" ? (
          <>
            <SidePanel name="pipelines" side="left" label="Pipelines" className="list">
              <div className="list-head">
                <h4>Pipelines</h4>
                <ImportButton
                  onImported={(name) => {
                    setPipelinesVersion((v) => v + 1);
                    go(`/pipelines/${name}`);
                  }}
                />
                <CollapseButton name="pipelines" side="left" label="Pipelines" />
              </div>
              {pipelines.map((p) => (
                <button
                  key={p.name}
                  className={`list-item ${p.name === route.name ? "on" : ""}`}
                  onClick={() => go(`/pipelines/${p.name}`)}
                >
                  <span className="grow">
                    <span>{p.pipeline ?? p.name}</span>
                    <span className="muted small">{p.path}</span>
                  </span>
                  {p.issues > 0 ? (
                    <span className="badge badge-issue">{p.issues}</span>
                  ) : (
                    <span className="small muted">{p.nodes} nodes</span>
                  )}
                </button>
              ))}
              {!pipelines.length && !error && <p className="muted pad">No *.yaml files.</p>}
            </SidePanel>
            <section className="content">
              {route.name && types ? (
                <Editor
                  name={route.name}
                  types={types}
                  onDeleted={() => {
                    setPipelinesVersion((v) => v + 1);
                    go("/pipelines");
                  }}
                />
              ) : (
                <p className="muted pad">Pick a pipeline, or ask the Chat to build one.</p>
              )}
            </section>
          </>
        ) : (
          <>
            <RunList selected={route.id} onSelect={(id) => go(`/runs/${id}`)} />
            <section className="content">
              {route.id && route.id !== "new" ? <RunView key={route.id} id={route.id} /> : <p className="muted pad">Pick a run.</p>}
            </section>
          </>
        )}
        {/* the Chat is global: the same conversation on every page, opened from the top bar */}
        {chatOpen && (
          <aside id="chat-panel" className="side side-assistant panel-right" aria-label="Chat">
            <Assistant
              types={types}
              onSettingsChanged={() => setTypesVersion((v) => v + 1)}
              onPipelinesChanged={(name) => {
                setPipelinesVersion((v) => v + 1);
                if (name && !(route.view === "pipelines" && name === route.name)) go(`/pipelines/${name}`);
              }}
            />
          </aside>
        )}
      </main>
    </div>
  );
}
