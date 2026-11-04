// The Mac app's banners above Studio, for a plain browser (spec 006 US1, US5): the engine isn't
// answering or is restarting, and "quit unexpectedly last time". The Mac app draws its own.
import { useEffect, useState } from "react";
import { api, type IncidentSummary } from "../api";
import { REFRESH_EVENT, type EngineState } from "../lib/engine";
import { isMacApp } from "../lib/native";

export const UNCLEAN_EXIT = "engine_unclean_exit";

/** The newest unclean-exit report nobody has looked at yet. */
export function crashToShow(items: IncidentSummary[]): IncidentSummary | null {
  const fresh = items.filter((i) => i.kind === UNCLEAN_EXIT && i.status === "new");
  fresh.sort((a, b) => b.last_seen - a.last_seen);
  return fresh[0] ?? null;
}

export function EngineBanner({ state }: { state: EngineState }) {
  const [crash, setCrash] = useState<IncidentSummary | null>(null);
  useEffect(() => {
    if (isMacApp()) return;
    const load = () =>
      api
        .incidents()
        .then((r) => setCrash(crashToShow(r.incidents)))
        .catch(() => {});
    void load();
    window.addEventListener(REFRESH_EVENT, load);
    return () => window.removeEventListener(REFRESH_EVENT, load);
  }, []);
  if (isMacApp()) return null;
  const seen = async (open: boolean) => {
    const id = crash?.id;
    setCrash(null);
    if (id) await api.incidentSeen(id).catch(() => {});
    if (open) window.location.hash = "/app-reports";
  };
  return (
    <>
      {state === "down" && (
        <div className="banner banner-error" role="alert">
          The engine isn't reachable. Start it with <code>agentline serve</code>; Studio reconnects by itself.
        </div>
      )}
      {state === "restarting" && (
        <div className="banner banner-info" role="status">
          Restarting the engine…
        </div>
      )}
      {crash && state === "up" && (
        <div className="banner banner-error row" role="alert">
          <span>agentline quit unexpectedly last time</span>
          <span className="spacer" />
          <button type="button" className="primary" onClick={() => void seen(true)}>
            View report
          </button>
          <button type="button" onClick={() => void seen(false)}>
            Dismiss
          </button>
        </div>
      )}
    </>
  );
}
