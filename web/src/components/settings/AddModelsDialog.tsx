// "Add models" (Settings > Models, phase 46): 1. pick a model source, 2. connect it (a key, the
// Claude login, or a server URL; tested right away), 3. tick its models. Also opened at step 2
// to reconnect a source and at step 3 to add more models from a connected one. The steps are
// AddModelsFlow, which first-run setup (SetupDialog step 1) shows inline.
import { useEffect, useState } from "react";
import { api } from "../../api";
import { NAME_RE, PROVIDER_PRESETS, submitSecret } from "../../lib/ai";
import { sourceTitle } from "../../lib/models";
import type { ProviderInfo, ProviderTest, Settings as SettingsData } from "../../types";
import { ClaudeLoginPanel, ModelCheckList, errText } from "./parts";

type Step = "provider" | "connect" | "models";
export interface AddStart {
  provider?: string;
  step?: Step;
}

interface Choice {
  id: string;
  title: string;
  hint: string;
  /** the source it connects (built in); none for a custom endpoint */
  provider?: string;
  /** the preset that puts a removed built-in source back */
  preset?: string;
  kind: "key" | "login" | "url" | "custom";
}

const CHOICES: Choice[] = [
  { id: "openrouter", title: "OpenRouter", hint: "One key, hundreds of models; some are free.", provider: "openrouter", preset: "OpenRouter", kind: "key" },
  { id: "anthropic", title: "Anthropic API", hint: "Claude models, billed per token.", provider: "anthropic", preset: "Anthropic API key", kind: "key" },
  { id: "openai", title: "OpenAI", hint: "GPT models, billed per token.", provider: "openai", preset: "OpenAI", kind: "key" },
  { id: "claude", title: "Claude subscription", hint: "Your Pro/Max login, for the Claude Code harness.", provider: "claude", preset: "Claude subscription", kind: "login" },
  { id: "ollama", title: "Local server", hint: "Ollama or another server on this computer. No key.", provider: "ollama", preset: "Ollama", kind: "url" },
  { id: "custom", title: "Custom endpoint", hint: "Any OpenAI-compatible URL: a private server, a gateway.", kind: "custom" },
];

function choiceFor(provider: string | undefined, data: SettingsData): Choice | null {
  if (!provider) return null;
  const known = CHOICES.find((c) => c.provider === provider);
  if (known) return known;
  const p = data.providers.find((x) => x.name === provider);
  if (p?.auth === "subscription") return { ...CHOICES[3], provider };
  return { ...CHOICES[5], provider };
}

interface FlowProps {
  data: SettingsData;
  start: AddStart;
  onApplied: (r: SettingsData) => void;
  onReload: () => void;
  onClose: () => void;
}

export function AddModelsDialog(props: FlowProps) {
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="add-models-title">
      <div className="modal wide add-models">
        <AddModelsFlow {...props} />
      </div>
    </div>
  );
}

/** The three steps. `embedded`: inside another dialog (first-run setup): smaller headings, no
 * Escape handler, and no Cancel on the first step (the host has its own buttons). */
export function AddModelsFlow({
  data,
  start,
  onApplied,
  onReload,
  onClose,
  embedded = false,
}: FlowProps & { embedded?: boolean }) {
  const H = embedded ? "h3" : "h2";
  const initial = choiceFor(start.provider, data);
  const [choice, setChoice] = useState<Choice | null>(initial);
  const [step, setStep] = useState<Step>(initial ? (start.step ?? "connect") : "provider");
  const [tested, setTested] = useState<ProviderTest | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const provider = choice?.provider ? data.providers.find((p) => p.name === choice.provider) : undefined;
  const claudeInstalled = [...data.harnesses, ...(data.presets ?? [])].some(
    (h) => h.kind === "claude-code" && h.installed,
  );

  useEffect(() => {
    if (embedded) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && !busy && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose, embedded]);
  // opened to add more models from a connected source: list them right away
  useEffect(() => {
    if (step === "models" && !tested && choice?.provider) void test(choice.provider);
  }, []); // once

  async function test(name: string): Promise<boolean> {
    setBusy(true);
    setErr(null);
    try {
      const r = await api.testProvider(name);
      if (!r.ok) {
        setErr(r.error ?? "The source didn't answer.");
        return false;
      }
      setTested(r);
      setChoice((c) => (c ? { ...c, provider: name } : c)); // a custom endpoint is named in the connect step
      setStep("models");
      onReload(); // connected now: the page shows its group
      return true;
    } catch (e) {
      setErr(errText(e));
      return false;
    } finally {
      setBusy(false);
    }
  }

  /** A removed built-in source comes back from its preset before it can be connected. */
  async function ensureSource(c: Choice): Promise<string | null> {
    if (!c.provider) return null;
    if (data.providers.some((p) => p.name === c.provider)) return c.provider;
    const preset = PROVIDER_PRESETS.find((p) => p.label === c.preset);
    if (!preset) return c.provider;
    onApplied(await api.putSetting("providers", c.provider, preset.body));
    return c.provider;
  }

  const pickChoice = async (c: Choice) => {
    setErr(null);
    setChoice(c);
    setTested(null);
    try {
      await ensureSource(c);
      setStep("connect");
    } catch (e) {
      setErr(errText(e));
    }
  };

  const added = choice?.provider
    ? data.models.filter((m) => m.provider === choice.provider).map((m) => m.model)
    : [];

  return (
    <>
        {!embedded && (
        <ol className="setup-steps small" aria-label="Steps">
          <li className={step === "provider" ? "on" : undefined}>1. Source</li>
          <li aria-hidden="true">·</li>
          <li className={step === "connect" ? "on" : undefined}>2. Connect</li>
          <li aria-hidden="true">·</li>
          <li className={step === "models" ? "on" : undefined}>3. Choose models</li>
        </ol>
        )}

        {step === "provider" && (
          <>
            <H id="add-models-title">Where do the models come from?</H>
            <div className="choice-grid">
              {CHOICES.filter((c) => c.kind !== "login" || claudeInstalled).map((c) => {
                const p = c.provider ? data.providers.find((x) => x.name === c.provider) : undefined;
                const connected = !!(p?.connected ?? p?.ready) && !!p;
                return (
                  <button key={c.id} type="button" className="choice" onClick={() => void pickChoice(c)}>
                    <strong>{c.title}</strong>
                    <span className="small muted">{connected ? "Connected: add more models" : c.hint}</span>
                  </button>
                );
              })}
            </div>
            {!claudeInstalled && (
              <p className="help">
                Have a Claude Pro/Max plan? Install Claude Code (<code>npm install -g @anthropic-ai/claude-code</code>) to
                use your login here.
              </p>
            )}
          </>
        )}

        {step === "connect" && choice && (
          <ConnectStep
            choice={choice}
            provider={provider}
            data={data}
            busy={busy}
            heading={H}
            setBusy={setBusy}
            setErr={setErr}
            onApplied={onApplied}
            onTest={test}
            onReload={onReload}
          />
        )}

        {step === "models" && choice?.provider && (
          <>
            <H id="add-models-title">Choose models from {provider ? sourceTitle(provider) : choice.title}</H>
            {tested ? (
              <>
                <p className="help ok">
                  Connected: {tested.count ?? tested.models.length} models
                  {tested.models.some((m) => m.free) ? `, ${tested.models.filter((m) => m.free).length} free` : ""}.
                </p>
                <ModelCheckList
                  models={tested.models}
                  added={added}
                  onAdd={async (ids) => {
                    const r = await api.chooseModels(choice.provider ?? "", ids, true);
                    onApplied(r);
                    onClose();
                    return r.added;
                  }}
                />
              </>
            ) : (
              <p className="muted">{busy ? "Listing its models…" : ""}</p>
            )}
          </>
        )}

        {err && <p className="issue">{err}</p>}
        <div className="modal-foot">
          {step !== "provider" && !start.provider && (
            <button type="button" disabled={busy} onClick={() => setStep(step === "models" ? "connect" : "provider")}>
              Back
            </button>
          )}
          <span className="grow" />
          {!(embedded && step === "provider") && (
            <button type="button" disabled={busy} onClick={onClose}>
              {step === "models" ? "Done" : "Cancel"}
            </button>
          )}
        </div>
    </>
  );
}

function ConnectStep({
  choice,
  provider,
  data,
  busy,
  heading: H,
  setBusy,
  setErr,
  onApplied,
  onTest,
  onReload,
}: {
  choice: Choice;
  provider: ProviderInfo | undefined;
  data: SettingsData;
  busy: boolean;
  heading: "h2" | "h3";
  setBusy: (b: boolean) => void;
  setErr: (e: string | null) => void;
  onApplied: (r: SettingsData) => void;
  onTest: (name: string) => Promise<boolean>;
  onReload: () => void;
}) {
  const [key, setKey] = useState("");
  const [replace, setReplace] = useState(false);
  const [url, setUrl] = useState(provider?.base_url ?? (choice.kind === "url" ? "http://localhost:11434/v1" : ""));
  const [name, setName] = useState(choice.provider ?? "");
  const [keyEnv, setKeyEnv] = useState(provider?.api_key_env ?? "");
  // a custom endpoint saved by this step whose test failed: trying again updates it
  const [created, setCreated] = useState<string | null>(null);
  const connected = !!(provider?.connected ?? provider?.ready);
  const title = provider ? sourceTitle(provider) : choice.title;

  const run = async (f: () => Promise<string | null>) => {
    setBusy(true);
    setErr(null);
    let target: string | null = null;
    try {
      target = await f();
    } catch (e) {
      setErr(errText(e));
    } finally {
      setBusy(false);
    }
    if (target) await onTest(target);
  };

  if (choice.kind === "key" && provider?.api_key_env) {
    const env = provider.api_key_env;
    return (
      <form
        className="connect"
        onSubmit={(e) => {
          e.preventDefault();
          if (connected && !replace) return void onTest(provider.name);
          void run(async () => {
            const r = await submitSecret(env, key, api.putSecret);
            setKey(r.draft);
            if (r.error) throw new Error(r.error);
            return provider.name;
          });
        }}
      >
        <H id="add-models-title">Connect {title}</H>
        {connected && !replace ? (
          <p className="help ok">
            Connected: <code>{env}</code> is set.{" "}
            <button type="button" className="link" onClick={() => setReplace(true)}>
              Use another key
            </button>
          </p>
        ) : (
          <label className="field">
            <span className="field-name">{title} API key</span>
            <input
              type="password"
              autoComplete="new-password"
              spellCheck={false}
              autoFocus
              value={key}
              placeholder={env}
              onChange={(e) => setKey(e.target.value)}
            />
            <span className="help">Saved on this computer only ({env}); never shown again. Tested before the next step.</span>
          </label>
        )}
        <button type="submit" className="primary" disabled={busy || (!(connected && !replace) && !key)}>
          {busy ? "Checking…" : "Next: choose models"}
        </button>
      </form>
    );
  }

  if (choice.kind === "login" && provider) {
    return (
      <div className="connect">
        <H id="add-models-title">Connect your Claude subscription</H>
        <p className="help">
          It uses Claude Code&apos;s own login. Sign in with <code>claude auth login</code> in Terminal, then check again.
        </p>
        <ClaudeLoginPanel provider={provider.name} ready={connected} onChanged={onReload} />
        <button type="button" className="primary" disabled={busy} onClick={() => void onTest(provider.name)}>
          {busy ? "Checking…" : "Next: choose models"}
        </button>
      </div>
    );
  }

  // a server URL: the built-in local server, or a custom endpoint (new or one you named)
  const isNew = choice.kind === "custom" && !provider;
  return (
    <form
      className="connect"
      onSubmit={(e) => {
        e.preventDefault();
        void run(async () => {
          const target = isNew ? name.trim() : (provider?.name ?? choice.provider ?? "");
          if (isNew && (!NAME_RE.test(target) || (target !== created && data.providers.some((p) => p.name === target))))
            throw new Error("Pick a new name: letters, digits, _ . -");
          if (!/^https?:\/\//.test(url.trim())) throw new Error("The URL starts with http:// or https://");
          const changed = isNew || url.trim() !== (provider?.base_url ?? "") || keyEnv.trim() !== (provider?.api_key_env ?? "");
          if (changed) {
            const body: Record<string, unknown> = { type: "openai-compatible", base_url: url.trim() };
            if (keyEnv.trim()) body.api_key_env = keyEnv.trim();
            onApplied(await api.putSetting("providers", target, body));
            if (isNew) setCreated(target);
          }
          if (keyEnv.trim() && key) {
            const r = await submitSecret(keyEnv.trim(), key, api.putSecret);
            if (r.error) throw new Error(r.error);
          }
          return target;
        });
      }}
    >
      <H id="add-models-title">{isNew ? "Add a custom endpoint" : `Connect ${title}`}</H>
      {isNew && (
        <label className="field">
          <span className="field-name">Name</span>
          <input type="text" value={name} placeholder="homelab" autoFocus onChange={(e) => setName(e.target.value)} />
        </label>
      )}
      <label className="field">
        <span className="field-name">Server URL (OpenAI-compatible)</span>
        <input type="url" value={url} placeholder="http://localhost:11434/v1" onChange={(e) => setUrl(e.target.value)} />
      </label>
      {choice.kind === "custom" && (
        <>
          <label className="field">
            <span className="field-name">Key name (empty: no key)</span>
            <input type="text" value={keyEnv} placeholder="HOMELAB_API_KEY" onChange={(e) => setKeyEnv(e.target.value)} />
          </label>
          {keyEnv.trim() && (
            <label className="field">
              <span className="field-name">Key value</span>
              <input
                type="password"
                autoComplete="new-password"
                value={key}
                placeholder="leave empty to keep the saved one"
                onChange={(e) => setKey(e.target.value)}
              />
            </label>
          )}
        </>
      )}
      <button type="submit" className="primary" disabled={busy || !url.trim() || (isNew && !name.trim())}>
        {busy ? "Checking…" : "Next: choose models"}
      </button>
    </form>
  );
}
