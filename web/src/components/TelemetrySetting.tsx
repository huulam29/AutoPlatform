// Settings > General > Telemetry (phase 58; agentline/telemetry.py): optional OpenTelemetry
// export of finished runs to the user's own collector. Off by default; header values live in
// the secrets file under a name and are never shown here.
import { useEffect, useState } from "react";
import { ApiError } from "../api";
import { type TelemetryStatus, feedbackApi } from "../lib/feedback";

export function TelemetrySetting() {
  const [t, setT] = useState<TelemetryStatus | null>(null);
  const [endpoint, setEndpoint] = useState("");
  const [secret, setSecret] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const show = (x: TelemetryStatus) => {
    setT(x);
    setEndpoint(x.endpoint);
    setSecret(x.headers_secret);
  };
  useEffect(() => {
    feedbackApi
      .telemetry()
      .then(show)
      .catch((e) => setMsg(e instanceof ApiError ? e.message : String(e)));
  }, []);
  const save = async (body: Parameters<typeof feedbackApi.setTelemetry>[0]) => {
    setBusy(true);
    setMsg(null);
    try {
      show(await feedbackApi.setTelemetry(body));
    } catch (e) {
      setMsg(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  if (!t) return msg ? <p className="issue small">{msg}</p> : null;
  return (
    <section className="telemetry" aria-labelledby="telemetry-title">
      <h3 id="telemetry-title">Telemetry</h3>
      <p className="help">
        Everything about runs stays on this machine (Run view metrics, <a href="#/insights">Insights</a>). If you run an
        OpenTelemetry collector, agentline can also send it each finished run as a trace and a few metrics: ids,
        statuses, times, costs, tokens and models. Never prompts, inputs, outputs, comments or error text.
      </p>
      {t.killed && <p className="small warn">AGENTLINE_TELEMETRY=off is set for the engine: nothing is sent.</p>}
      <div className="field">
        <label htmlFor="otlp-endpoint">Collector address (OTLP over HTTP)</label>
        <input
          id="otlp-endpoint"
          className="mono"
          placeholder="http://localhost:4318"
          value={endpoint}
          onChange={(e) => setEndpoint(e.target.value)}
        />
      </div>
      <div className="field">
        <label htmlFor="otlp-headers">Headers secret (optional)</label>
        <input
          id="otlp-headers"
          className="mono"
          placeholder="OTEL_HEADERS"
          value={secret}
          onChange={(e) => setSecret(e.target.value.toUpperCase())}
        />
        <p className="small muted">
          The name of a key in General → Other keys whose value is <code>name=value,name2=value2</code>
          {t.headers_secret && (t.headers_set ? " (set)." : " (not set yet).")}
        </p>
      </div>
      <div className="row">
        <button type="button" disabled={busy} onClick={() => void save({ endpoint: endpoint.trim(), headers_secret: secret.trim() })}>
          Save
        </button>
        <button
          type="button"
          className={t.enabled ? "" : "primary"}
          disabled={busy || (!t.enabled && !endpoint.trim())}
          aria-pressed={t.enabled}
          onClick={() => void save({ enabled: !t.enabled, endpoint: endpoint.trim(), headers_secret: secret.trim() })}
        >
          {t.enabled ? "Turn off" : "Turn on"}
        </button>
        <button
          type="button"
          disabled={busy || !t.endpoint}
          onClick={async () => {
            setBusy(true);
            try {
              const r = await feedbackApi.testTelemetry();
              setMsg(r.ok ? "The collector answered." : `Test failed: ${r.error ?? "no answer"}`);
            } finally {
              setBusy(false);
            }
          }}
        >
          Test
        </button>
      </div>
      <p className="small muted">
        {t.enabled ? `On: sending to ${t.endpoint}.` : "Off."}
        {t.last && ` Last export ${new Date(t.last.at * 1000).toLocaleString()}: ${t.last.ok ? `${t.last.sent} run(s) sent` : `failed (${t.last.error ?? "unknown"})`}.`}
      </p>
      {msg && <p className="small">{msg}</p>}
    </section>
  );
}
