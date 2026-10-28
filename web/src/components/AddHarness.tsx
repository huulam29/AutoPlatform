// "Add harness" (Settings > Harnesses) and "+ Add a harness…" (every Harness picker): only what
// can run is offered. Installed programs (Claude Code, Codex, Aider, OpenCode, Hermes Agent) can
// be added; the others sit in a collapsed "Not installed" list with the command that installs
// them and Check again.
// Save selects the new harness; Cancel keeps what was selected before.
import { useState } from "react";
import { ApiError, api } from "../api";
import { ADD_HARNESS, SETTINGS_CHANGED_EVENT } from "../lib/ai";
import { INSTALL_COMMANDS } from "../lib/models";
import type { HarnessPreset } from "../types";

/** The picker's last entry. */
export function AddHarnessOption({ presets }: { presets: HarnessPreset[] | undefined }) {
  if (!presets?.length) return null;
  return <option value={ADD_HARNESS}>+ Add a harness…</option>;
}

const TITLES: Record<string, string> = {
  "claude-code": "Claude Code",
  codex: "Codex",
  aider: "Aider",
  opencode: "OpenCode",
  "hermes-agent": "Hermes Agent",
};

export function AddHarnessDialog({
  presets,
  initial,
  onAdded,
  onCancel,
  onCustom,
}: {
  presets: HarnessPreset[];
  /** the preset to start on (e.g. the harness a pipeline names but isn't set up) */
  initial?: string;
  onAdded: (name: string) => void;
  onCancel: () => void;
  /** Settings > Harnesses: "Custom harness…" opens the full form */
  onCustom?: () => void;
}) {
  const installed = presets.filter((p) => p.installed);
  const missing = presets.filter((p) => !p.installed);
  const [pick, setPick] = useState(installed.find((p) => p.name === initial)?.name ?? installed[0]?.name);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const preset = installed.find((p) => p.name === pick);
  const save = async () => {
    if (!preset) return;
    setBusy(true);
    setErr(null);
    try {
      const body: Record<string, unknown> = { kind: preset.kind };
      if (preset.model) body.model = preset.model;
      await api.putSetting("harnesses", preset.name, body);
      window.dispatchEvent(new Event(SETTINGS_CHANGED_EVENT));
      onAdded(preset.name);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  // a program installed meanwhile: reload what the pickers and pages list
  const checkAgain = () => window.dispatchEvent(new Event(SETTINGS_CHANGED_EVENT));
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="add-harness-title">
      <div className="modal">
        <h2 id="add-harness-title">Add harness</h2>
        <p className="muted">A harness is a program that drives the model: its loop, tools and context.</p>
        {installed.length > 0 ? (
          <div className="add-harness-list" role="radiogroup" aria-label="installed harnesses">
            {installed.map((p) => (
              <label key={p.name} className={`setup-card ${pick === p.name ? "ok" : ""}`}>
                <span className="setup-head">
                  <input type="radio" name="add-harness" checked={pick === p.name} onChange={() => setPick(p.name)} />
                  <strong>{TITLES[p.name] ?? p.name}</strong>
                  <span className="badge">installed</span>
                </span>
                <span className="small muted">
                  {p.description || `the ${p.kind} program`}
                  {p.model ? ` · default model ${p.model}` : ""}
                </span>
              </label>
            ))}
          </div>
        ) : (
          <p className="help">None of these programs is installed on this computer yet.</p>
        )}
        {missing.length > 0 && (
          <details className="not-installed" open={installed.length === 0}>
            <summary>Not installed ({missing.length})</summary>
            {missing.map((p) => (
              <div key={p.name} className="not-installed-item">
                <strong>{TITLES[p.name] ?? p.name}</strong>
                {INSTALL_COMMANDS[p.name] && <code className="mono small">{INSTALL_COMMANDS[p.name]}</code>}
              </div>
            ))}
            <button type="button" onClick={checkAgain}>
              Check again
            </button>
          </details>
        )}
        {onCustom && (
          <button type="button" className="link" onClick={onCustom}>
            Custom harness…
          </button>
        )}
        {err && <p className="issue">{err}</p>}
        <div className="modal-foot">
          <button type="button" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button type="button" className="primary" onClick={() => void save()} disabled={busy || !preset}>
            {busy ? "Saving…" : "Add and use"}
          </button>
        </div>
      </div>
    </div>
  );
}
