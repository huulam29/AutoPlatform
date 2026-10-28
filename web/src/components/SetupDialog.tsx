// First-run setup (api/setup.py), in two steps: 1. connect a model API: the same steps as
// Settings > Models > Add models (AddModelsFlow), 2. pick the harness the Chat uses and its model. Shown on the first run and
// whenever no harness can run, in the browser and in the Mac app; each step can be skipped,
// and Settings > General opens it again (OPEN_SETUP_EVENT).
import { useEffect, useRef, useState } from "react";
import { ApiError, api } from "../api";
import { modelGroups, sourceTitle } from "../lib/models";
import {
  OPEN_SETUP_EVENT,
  firstStep,
  harnessModelOptions,
  sourcesNextLabel,
  suggestedHarness,
  type SetupStep,
} from "../lib/setup";
import type { Settings as SettingsData, SetupHarness, SetupStatus } from "../types";
import { AddModelsFlow } from "./settings/AddModelsDialog";

export function SetupDialog({ onDone }: { onDone: () => void }) {
  const [status, setStatus] = useState<SetupStatus | null>(null);
  const [step, setStep] = useState<SetupStep>("sources");
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [settings, setSettings] = useState<SettingsData | null>(null);
  const [flow, setFlow] = useState(0); // bumped to start Add models over
  const modal = useRef<HTMLDivElement>(null);
  useEffect(() => {
    modal.current?.scrollTo({ top: 0 });
  }, [step]);

  const load = (force: boolean) =>
    api
      .setup()
      .then((s) => {
        setStatus(s);
        if (force || s.needs_setup) {
          setStep(force ? "sources" : firstStep(s)); // asked for again: from the start
          setOpen(true);
        }
      })
      .catch(() => setStatus(null)); // an older engine: no popup
  useEffect(() => {
    void load(false);
    const again = () => void load(true);
    window.addEventListener(OPEN_SETUP_EVENT, again);
    return () => window.removeEventListener(OPEN_SETUP_EVENT, again);
  }, []);

  // step 1 shows the Add models steps, which work on the settings
  const reloadSettings = () =>
    api
      .settings()
      .then(setSettings)
      .catch(() => setSettings(null));
  useEffect(() => {
    if (open && step === "sources") void reloadSettings();
  }, [open, step]);

  if (!open || !status) return null;

  const run = async (id: string, op: () => Promise<SetupStatus>): Promise<SetupStatus | null> => {
    setBusy(id);
    setError(null);
    try {
      const s = await op();
      setStatus(s);
      return s;
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
      return null;
    } finally {
      setBusy(null);
    }
  };

  /** A source connected or its models added: refresh what step 1 lists and what step 2 offers. */
  const changed = () => {
    void reloadSettings();
    void run("sources", () => api.setup());
  };

  const use = async (h: SetupHarness, model: string) => {
    const s = await run(h.id, () => api.setupChoice(h.id, model || undefined));
    if (s) {
      onDone();
      setOpen(false);
    }
  };

  const skipAll = async () => {
    await run("dismiss", () => api.setupDismiss());
    setOpen(false);
  };

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="setup-title">
      <div className="modal" ref={modal}>
        <p className="setup-steps small muted" aria-label="setup steps">
          <span className={step === "sources" ? "on" : undefined}>1. Model API</span>
          <span aria-hidden="true"> · </span>
          <span className={step === "harness" ? "on" : undefined}>2. Harness</span>
        </p>
        {step === "sources" ? (
          <>
            <h2 id="setup-title">Connect a model API?</h2>
            <p className="muted">
              Connect a model source (one is enough): an API key, your Claude login or a model running on this
              computer, then pick its models. Keys go to this computer&apos;s secrets file and are never shown again.
              You can skip this and add them later in Settings → Models.
            </p>
            {settings ? (
              <>
                <ConnectedSources data={settings} />
                <div className="add-models setup-add-models">
                  <AddModelsFlow
                    key={flow}
                    embedded
                    data={settings}
                    start={{}}
                    onApplied={(r) => {
                      setSettings(r);
                      changed();
                    }}
                    onReload={changed}
                    onClose={() => setFlow((n) => n + 1)}
                  />
                </div>
              </>
            ) : (
              <p className="muted">Loading…</p>
            )}
          </>
        ) : (
          <>
            <h2 id="setup-title">Which harness should the Chat use?</h2>
            <p className="muted">
              A harness drives the model: its loop, tools and context. <strong>Built-in</strong> needs only an API key
              from step 1. Claude Code, Codex and Aider are programs you install. Agent nodes can pick their own
              harness later.
            </p>
            {status.harnesses.map((h) => (
              <HarnessCard
                key={h.id}
                harness={h}
                suggested={h.id === suggestedHarness(status)}
                saved={status.choice?.harness === h.id ? status.choice.model : undefined}
                busy={busy === h.id}
                onUse={(m) => void use(h, m)}
                onCheck={() => void run(h.id, () => api.setup())}
                onBack={() => setStep("sources")}
              />
            ))}
          </>
        )}
        {error && <p className="issue">{error}</p>}
        <div className="modal-foot">
          <button type="button" disabled={busy !== null} onClick={() => void skipAll()}>
            {status.ready && !status.needs_setup ? "Close" : "Skip setup for now"}
          </button>
          <span className="grow" />
          {step === "sources" ? (
            <button type="button" className="primary" onClick={() => setStep("harness")}>
              {sourcesNextLabel(status)}
            </button>
          ) : (
            <button type="button" onClick={() => setStep("sources")}>
              Back
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

/** Step 1: the sources already connected, with their model counts. */
function ConnectedSources({ data }: { data: SettingsData }) {
  const groups = modelGroups(data);
  if (!groups.length) return null;
  return (
    <ul className="setup-connected small" aria-label="connected sources">
      {groups.map((g) => (
        <li key={g.provider.name}>
          <span className="source-ok">
            <span className="dot" aria-hidden="true" />
            {sourceTitle(g.provider)}
          </span>{" "}
          <span className="muted">
            {g.models.length} model{g.models.length === 1 ? "" : "s"}
          </span>
        </li>
      ))}
    </ul>
  );
}

function HarnessCard({
  harness: h,
  suggested,
  saved,
  busy,
  onUse,
  onCheck,
  onBack,
}: {
  harness: SetupHarness;
  suggested: boolean;
  /** the model saved for this harness, when it is the Chat's current choice */
  saved?: string | null;
  busy: boolean;
  onUse: (model: string) => void;
  onCheck: () => void;
  onBack: () => void;
}) {
  const options = harnessModelOptions(h);
  const [model, setModel] = useState(saved ?? h.model ?? options[0]?.value ?? "");
  return (
    <div className={`setup-card ${h.available ? "ok" : ""}`}>
      <div className="setup-head">
        <strong>{h.title}</strong>
        {h.available && <span className="badge">{suggested ? "suggested" : "ready"}</span>}
      </div>
      <p className="small muted">{h.description}</p>
      <p className="small">{h.hint}</p>
      {h.available ? (
        <div className="setup-key">
          {options.length > 0 && (
            <select value={model} aria-label={`model for ${h.title}`} onChange={(e) => setModel(e.target.value)}>
              {options.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          )}
          <button type="button" className={suggested ? "primary" : undefined} disabled={busy} onClick={() => onUse(model)}>
            {h.configured === false ? "Add and use for the Chat" : "Use for the Chat"}
          </button>
        </div>
      ) : h.cli ? (
        <button type="button" disabled={busy} onClick={onCheck}>
          Check again
        </button>
      ) : (
        <button type="button" onClick={onBack}>
          Back to step 1
        </button>
      )}
    </div>
  );
}
