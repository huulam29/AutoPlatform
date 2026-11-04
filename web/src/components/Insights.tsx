// Insights (phase 58; GET /api/insights): trends of finished runs per pipeline, per day and, for
// one pipeline, per node; the days edits from run reviews were adopted are marked.
import { useEffect, useState } from "react";
import { ApiError } from "../api";
import {
  type Insights as InsightsData,
  adoptionDays,
  barHeights,
  feedbackApi,
  formatDuration,
  percent,
} from "../lib/feedback";
import { formatCost } from "../lib/runs";

type Measure = "runs" | "failed" | "cost_usd" | "median_s" | "down";
const MEASURES: { id: Measure; label: string; fmt: (v: number) => string }[] = [
  { id: "runs", label: "Runs", fmt: (v) => String(v) },
  { id: "failed", label: "Failed", fmt: (v) => String(v) },
  { id: "median_s", label: "Median time", fmt: formatDuration },
  { id: "cost_usd", label: "Cost", fmt: formatCost },
  { id: "down", label: "👎", fmt: (v) => String(v) },
];

export function Insights() {
  const [days, setDays] = useState(30);
  const [pipeline, setPipeline] = useState<string | null>(null);
  const [measure, setMeasure] = useState<Measure>("runs");
  const [r, setR] = useState<InsightsData | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    feedbackApi
      .insights(days, pipeline)
      .then((x) => live && (setR(x), setError(null)))
      .catch((e) => live && setError(e instanceof ApiError ? e.message : String(e)));
    return () => {
      live = false;
    };
  }, [days, pipeline]);
  if (!r) return error ? <p className="issue pad">{error}</p> : <p className="muted pad">Loading…</p>;
  const m = MEASURES.find((x) => x.id === measure)!;
  const heights = barHeights(r.by_day.map((d) => d[measure]));
  const adopted = adoptionDays(r);
  return (
    <div className="costs insights">
      <div className="toolbar">
        <strong>Insights</strong>
        <div className="tabs">
          {[7, 30, 90].map((d) => (
            <button key={d} className={days === d ? "on" : ""} onClick={() => setDays(d)}>
              {d} days
            </button>
          ))}
        </div>
        {pipeline && (
          <button type="button" onClick={() => setPipeline(null)}>
            All pipelines
          </button>
        )}
      </div>
      <div className="costs-body">
        <p className="small muted">
          Finished runs (done, failed, stopped for review) started in the last {r.days} days. Rate runs in the Run view; a
          👎 or a comment gets the run reviewed, and reviews propose the fixes on the <a href="#/lessons">Lessons</a> page.
        </p>
        {r.pipelines.length === 0 ? (
          <p className="muted">No finished runs in this window.</p>
        ) : (
          <table className="table insights-table">
            <thead>
              <tr>
                <th>Pipeline</th>
                <th className="num">Runs</th>
                <th className="num">Succeeded</th>
                <th className="num">Median</th>
                <th className="num">Slowest 10%</th>
                <th className="num">Cost / run</th>
                <th className="num">Retries</th>
                <th className="num">Loops</th>
                <th className="num">Rejected</th>
                <th className="num">👍</th>
                <th className="num">👎</th>
              </tr>
            </thead>
            <tbody>
              {r.pipelines.map((p) => (
                <tr key={p.pipeline} className={pipeline === p.pipeline ? "on" : ""}>
                  <td>
                    <button type="button" className="link mono" onClick={() => setPipeline(p.pipeline)} title="Show its nodes">
                      {p.pipeline}
                    </button>
                  </td>
                  <td className="num">{p.runs}</td>
                  <td className="num">{percent(p.success_rate)}</td>
                  <td className="num">{formatDuration(p.median_s)}</td>
                  <td className="num">{formatDuration(p.p90_s)}</td>
                  <td className="num">{formatCost(p.cost_per_run)}</td>
                  <td className="num">{p.retries}</td>
                  <td className="num">{p.loops}</td>
                  <td className="num">{p.rejections}</td>
                  <td className="num">{p.up}</td>
                  <td className="num">{p.down}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <div className="toolbar">
          <h3>Per day{pipeline ? `: ${pipeline}` : ""}</h3>
          <div className="tabs" role="group" aria-label="what the bars show">
            {MEASURES.map((x) => (
              <button key={x.id} type="button" className={measure === x.id ? "on" : ""} aria-pressed={measure === x.id} onClick={() => setMeasure(x.id)}>
                {x.label}
              </button>
            ))}
          </div>
        </div>
        <div className="bars" role="img" aria-label={`${m.label} per day over the last ${r.days} days`}>
          {r.by_day.map((d, i) => (
            <div
              key={d.day}
              className={`bar ${adopted.has(d.day) ? "adopted" : ""}`}
              title={`${d.day}: ${m.fmt(d[measure])}${adopted.has(d.day) ? " (an edit was adopted)" : ""}`}
            >
              <span style={{ height: `${heights[i]}%` }} />
            </div>
          ))}
        </div>
        <div className="bars-axis small muted">
          <span>{r.by_day[0]?.day}</span>
          {adopted.size > 0 && <span>▲ marks a day an edit was adopted</span>}
          <span>{r.by_day[r.by_day.length - 1]?.day}</span>
        </div>
        {pipeline && (
          <>
            <h3>Nodes of {pipeline}</h3>
            {r.nodes.length === 0 ? (
              <p className="muted small">No node ran in this window.</p>
            ) : (
              <table className="table insights-table">
                <thead>
                  <tr>
                    <th>Node</th>
                    <th className="num">Runs</th>
                    <th className="num">Failed</th>
                    <th className="num">Median</th>
                    <th className="num">Cost</th>
                    <th className="num">Retries</th>
                    <th className="num">👍</th>
                    <th className="num">👎</th>
                  </tr>
                </thead>
                <tbody>
                  {r.nodes.map((n) => (
                    <tr key={n.node}>
                      <td className="mono">
                        {n.node} <span className="muted small">{n.kind}</span>
                      </td>
                      <td className="num">{n.executions}</td>
                      <td className="num">{percent(n.failure_rate)}</td>
                      <td className="num">{formatDuration(n.median_s)}</td>
                      <td className="num">{formatCost(n.cost_usd)}</td>
                      <td className="num">{n.retries}</td>
                      <td className="num">{n.up}</td>
                      <td className="num">{n.down}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </>
        )}
      </div>
    </div>
  );
}
