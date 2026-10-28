// Dry run (api/dryrun.py): run the open pipeline on the real engine with every agent and action
// stubbed. Choose what each router answers; see which nodes ran, how often, and how it ended.
import { useState } from "react";
import { ApiError, api, type DryRunResult } from "../api";
import { colorGroup } from "../lib/runs";
import type { Model } from "../types";

/** Router id -> its labels, in edge order (what the user can script). */
export function routerLabels(model: Model): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const n of model.nodes) if (n.type === "router") out[n.id] = [];
  for (const e of model.edges) {
    if (e.src in out && e.when && !out[e.src].includes(e.when)) out[e.src].push(e.when);
  }
  return out;
}

/** "CHANGES, PASS" -> ["CHANGES", "PASS"] */
export function parseRoutes(text: Record<string, string>): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const [k, v] of Object.entries(text)) {
    const labels = v.split(",").map((x) => x.trim()).filter(Boolean);
    if (labels.length) out[k] = labels;
  }
  return out;
}

export function DryRunButton({ name, model, source }: { name: string; model: Model; source: string }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" title="Run it with every agent and action stubbed: no cost, no side effects" onClick={() => setOpen(true)}>
        Dry run
      </button>
      {open && <DryRunDialog name={name} model={model} source={source} onClose={() => setOpen(false)} />}
    </>
  );
}

function DryRunDialog({
  name,
  model,
  source,
  onClose,
}: {
  name: string;
  model: Model;
  source: string;
  onClose: () => void;
}) {
  const labels = routerLabels(model);
  const [routes, setRoutes] = useState<Record<string, string>>({});
  const [payload, setPayload] = useState("{}");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<DryRunResult | null>(null);

  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      const p = payload.trim() ? (JSON.parse(payload) as unknown) : {};
      setResult(await api.dryRun({ name, source, payload: p, routes: parseRoutes(routes) }));
    } catch (e) {
      if (e instanceof ApiError) {
        const issues = (e.body as { detail?: { issues?: string[] } } | null)?.detail?.issues;
        setError(issues?.length ? `${e.message}\n${issues.join("\n")}` : e.message);
      } else setError(e instanceof SyntaxError ? `payload isn't JSON: ${e.message}` : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="dry-title">
      <div className="modal wide">
        <h2 id="dry-title">Dry run: {model.name ?? name}</h2>
        <p className="muted small">
          The real engine (joins, loops, max_loops, for_each, gates) with stubbed agents and actions: nothing is
          called, nothing costs money. Gates are approved automatically.
        </p>
        {Object.keys(labels).length > 0 && (
          <div className="dry-routes">
            <strong className="small">What each router answers, one label per visit (empty: move forward)</strong>
            {Object.entries(labels).map(([id, labs]) => (
              <label key={id} className="field">
                <span className="field-name">
                  {id} <span className="muted small">({labs.join(" | ") || "no labels"})</span>
                </span>
                <input
                  value={routes[id] ?? ""}
                  placeholder={labs.length > 1 ? `e.g. ${labs.slice(0, 2).join(", ")}` : labs[0] ?? ""}
                  onChange={(e) => setRoutes({ ...routes, [id]: e.target.value })}
                />
              </label>
            ))}
          </div>
        )}
        <label className="field">
          <span className="field-name">Payload (JSON; missing fields are filled in)</span>
          <textarea className="mono" rows={2} value={payload} onChange={(e) => setPayload(e.target.value)} />
        </label>
        {error && <pre className="issue small">{error}</pre>}
        {result && (
          <div className="dry-result">
            <p>
              Run ended <span className={`badge status-${result.status}`}>{result.status}</span>
              {result.error && <span className="issue small"> {result.error}</span>}
            </p>
            <table className="table">
              <thead>
                <tr>
                  <th>node</th>
                  <th>status</th>
                  <th>runs</th>
                </tr>
              </thead>
              <tbody>
                {result.nodes.map((n) => (
                  <tr key={n.node} className={`run-${colorGroup(n.status)}`}>
                    <td className="mono">{n.node}</td>
                    <td>
                      <span className={`dot status-${n.status}`} /> {n.status}
                      {n.error && <div className="issue small">{n.error}</div>}
                    </td>
                    <td>{n.runs}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {result.routes.length > 0 && (
              <p className="small">
                Routes: {result.routes.map((r) => `${r.router} → ${r.label}`).join(", ")}
              </p>
            )}
            <details>
              <summary className="small">Journal ({result.journal.length})</summary>
              <ol className="journal">
                {result.journal.map((j, i) => (
                  <li key={i}>
                    <span className="mono">{j.node ?? "run"}</span>
                    {j.iteration > 1 ? ` #${j.iteration}` : ""}
                    {j.task != null ? ` task ${j.task}` : ""}: {j.message}
                  </li>
                ))}
              </ol>
            </details>
          </div>
        )}
        <div className="modal-foot row">
          <button type="button" onClick={onClose}>
            Close
          </button>
          <button type="button" className="primary" disabled={busy} onClick={() => void run()}>
            {busy ? "Running…" : result ? "Run again" : "Run"}
          </button>
        </div>
      </div>
    </div>
  );
}
