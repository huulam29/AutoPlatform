// Costs (api/costs.py): spend per day, pipeline, model and node; the monthly budget.
import { useEffect, useState } from "react";
import { ApiError, api, type CostReport } from "../api";
import { formatCost } from "../lib/runs";

export function budgetPercent(month: number, budget: number | null | undefined): number | null {
  return budget ? Math.round((100 * month) / budget) : null;
}

export function Costs() {
  const [days, setDays] = useState(30);
  const [r, setR] = useState<CostReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [budget, setBudget] = useState("");
  useEffect(() => {
    api
      .costs(days)
      .then((x) => {
        setR(x);
        setBudget(x.budget_usd ? String(x.budget_usd) : "");
      })
      .catch((e) => setError(e instanceof ApiError ? e.message : String(e)));
  }, [days]);
  if (!r) return error ? <p className="issue pad">{error}</p> : <p className="muted pad">Loading…</p>;
  const max = Math.max(...r.by_day.map((d) => d.usd), 0.000001);
  const pct = budgetPercent(r.month_usd, r.budget_usd);
  return (
    <div className="costs">
      <div className="toolbar">
        <strong>Costs</strong>
        <div className="tabs">
          {[7, 30, 90].map((d) => (
            <button key={d} className={days === d ? "on" : ""} onClick={() => setDays(d)}>
              {d} days
            </button>
          ))}
        </div>
      </div>
      <div className="costs-body">
        <div className="cost-cards">
          <div className="cost-card">
            <div className="muted small">Last {r.days} days</div>
            <div className="cost-big">{formatCost(r.total_usd)}</div>
          </div>
          <div className={`cost-card ${pct !== null && pct >= 100 ? "over" : pct !== null && pct >= 80 ? "near" : ""}`}>
            <div className="muted small">This month ({r.month})</div>
            <div className="cost-big">{formatCost(r.month_usd)}</div>
            {pct !== null && (
              <>
                <div className="meter" role="meter" aria-label="share of the monthly budget spent" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
                  <span style={{ width: `${Math.min(pct, 100)}%` }} />
                </div>
                <div className="small">
                  {pct}% of {formatCost(r.budget_usd)}
                </div>
              </>
            )}
          </div>
          <form
            className="cost-card"
            onSubmit={async (e) => {
              e.preventDefault();
              const v = budget.trim() ? Number(budget) : null;
              const res = await api.setBudget(v && v > 0 ? v : null);
              setR({ ...r, budget_usd: res.budget_usd });
            }}
          >
            <label className="muted small" htmlFor="budget">
              Monthly budget (USD)
            </label>
            <div className="row">
              <input id="budget" type="number" min={0} step="any" value={budget} placeholder="none" onChange={(e) => setBudget(e.target.value)} />
              <button type="submit">Save</button>
            </div>
            <div className="small muted">Alerts at 50%, 80% and 100% go to your linked chat.</div>
          </form>
        </div>
        <h3>Per day</h3>
        <div className="bars" role="img" aria-label={`spend per day over the last ${r.days} days`}>
          {r.by_day.map((d) => (
            <div key={d.day} className="bar" title={`${d.day}: ${formatCost(d.usd)}`}>
              <span style={{ height: `${(100 * d.usd) / max}%` }} />
            </div>
          ))}
        </div>
        <div className="bars-axis small muted">
          <span>{r.by_day[0]?.day}</span>
          <span>{r.by_day[r.by_day.length - 1]?.day}</span>
        </div>
        <div className="cost-tables">
          <CostTable title="By pipeline" rows={r.by_pipeline.map((x) => [x.pipeline, `${x.runs} runs`, x.usd])} />
          <CostTable title="By model" rows={r.by_model.map((x) => [x.model, "", x.usd])} />
          <CostTable title="Most expensive nodes" rows={r.top_nodes.map((x) => [x.node, "", x.usd])} />
        </div>
      </div>
    </div>
  );
}

function CostTable({ title, rows }: { title: string; rows: [string, string, number][] }) {
  return (
    <section>
      <h3>{title}</h3>
      {rows.length === 0 ? (
        <p className="muted small">No spend recorded.</p>
      ) : (
        <table className="table">
          <tbody>
            {rows.map(([a, b, usd]) => (
              <tr key={a}>
                <td className="mono">{a}</td>
                <td className="muted small">{b}</td>
                <td className="num">{formatCost(usd)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
