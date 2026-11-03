// Engine output: the Mac app's own log (app + engine) when inside it, else GET /api/logs.
// With Follow off the view is a frozen snapshot: the buffer trims old lines from the top on
// every poll, so re-rendering it would push what you're reading upward.
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { CopyPath } from "./CopyPath";
import { useAppInfo } from "../lib/appinfo";
import { LINE_COUNTS, filterLines, newSince, splitMatches, type LevelFilter } from "../lib/logs";
import { isMacApp, nativeCall } from "../lib/native";

const COUNT_KEY = "agentline.logs.count";

function savedCount(): number {
  try {
    const n = Number(window.localStorage.getItem(COUNT_KEY));
    return (LINE_COUNTS as readonly number[]).includes(n) ? n : 500;
  } catch {
    return 500;
  }
}

export function Logs() {
  const [latest, setLatest] = useState<string[]>([]);
  const [shown, setShown] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [follow, setFollow] = useState(true);
  const [count, setCount] = useState(savedCount);
  const [level, setLevel] = useState<LevelFilter>("all");
  const [query, setQuery] = useState("");
  const box = useRef<HTMLPreElement>(null);
  const info = useAppInfo();

  useEffect(() => {
    let live = true;
    const load = async () => {
      try {
        const r = isMacApp()
          ? await nativeCall<{ lines: string[] }>("logs.get", { limit: count })
          : await api.logs(count);
        if (live) {
          setLatest(r.lines);
          setError(null);
        }
      } catch (e) {
        if (live) setError(String((e as Error).message));
      }
    };
    void load();
    const t = setInterval(() => void load(), 2000);
    return () => {
      live = false;
      clearInterval(t);
    };
  }, [count]);

  // following: show every poll and stay at the bottom; not following: keep the snapshot
  useEffect(() => {
    if (follow) setShown(latest);
  }, [latest, follow]);
  useEffect(() => {
    if (!follow) return;
    const el = box.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [shown, follow, level, query]);

  const visible = useMemo(() => filterLines(shown, level, query), [shown, level, query]);
  const pending = follow ? 0 : newSince(shown, latest);
  const filtered = level !== "all" || query.trim() !== "";

  return (
    <div className="page">
      <div className="toolbar">
        <strong>Logs</strong>
        <a className="small" href="#/app-reports" title="Crash reports and other problems in agentline itself">
          App reports
        </a>
        <span className="muted small">
          {isMacApp() ? "app and engine output" : "engine log"}
          {filtered ? `, ${visible.length} of ${shown.length} lines` : `, ${shown.length} lines`}
        </span>
        <span className="spacer" />
        <input
          type="search"
          className="logs-search"
          placeholder="Search the log"
          aria-label="search the log"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <select value={level} aria-label="minimum level" onChange={(e) => setLevel(e.target.value as LevelFilter)}>
          <option value="all">All levels</option>
          <option value="warning">Warnings and errors</option>
          <option value="error">Errors only</option>
        </select>
        <select
          value={count}
          aria-label="lines to keep"
          onChange={(e) => {
            const n = Number(e.target.value);
            setCount(n);
            try {
              window.localStorage.setItem(COUNT_KEY, String(n));
            } catch {
              /* per-viewer convenience only */
            }
          }}
        >
          {LINE_COUNTS.map((n) => (
            <option key={n} value={n}>
              Last {n} lines
            </option>
          ))}
        </select>
        <label className="check small">
          <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} /> Follow
        </label>
        <button type="button" onClick={() => void navigator.clipboard?.writeText(visible.map((l) => l.text).join("\n"))}>
          Copy
        </button>
        {isMacApp() ? (
          <button type="button" onClick={() => void nativeCall("finder.dataFolder")}>
            Show Data Folder
          </button>
        ) : (
          info && <CopyPath path={info.data_folder} canReveal={info.can_reveal} target="data" label="Data folder" />
        )}
      </div>
      {pending > 0 && (
        <div className="banner banner-info logs-paused" role="status">
          Paused. {pending} new line{pending === 1 ? "" : "s"} since.{" "}
          <button type="button" onClick={() => setFollow(true)}>
            Show latest
          </button>
        </div>
      )}
      {error && <p className="issue pad">{error}</p>}
      <pre
        ref={box}
        className="logs mono small"
        tabIndex={0}
        aria-label="log lines"
        onScroll={(e) => {
          // scrolling up to read stops following, so new lines don't pull the view away
          const el = e.currentTarget;
          if (follow && el.scrollHeight - el.scrollTop - el.clientHeight > 48) setFollow(false);
        }}
      >
        {visible.length
          ? visible.map((l) => (
              <span key={l.n} className={`log-line${l.level === "error" ? " log-error" : l.level === "warning" ? " log-warning" : ""}`}>
                {splitMatches(l.text, query).map((p, i) => (p.hit ? <mark key={i}>{p.text}</mark> : <span key={i}>{p.text}</span>))}
                {"\n"}
              </span>
            ))
          : shown.length
            ? "No lines match."
            : "No output yet."}
      </pre>
    </div>
  );
}
