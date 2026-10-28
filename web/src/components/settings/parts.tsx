// Pieces shared by the Settings pages (Models, General > Other keys) and the Add models
// dialog: the remove-a-source confirm, the Claude login panel, the model checklist, keys.
import { useEffect, useState } from "react";
import { api } from "../../api";
import { filterTested, selectShown, submitSecret, toggled } from "../../lib/ai";
import type { ClaudeLogin, KeyStatus, Settings as SettingsData, TestedModel } from "../../types";
import { Status } from "../Icon";

export const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));

/** Asks before deleting a model source, listing the models that go with it. */
export function DeleteSourceDialog({
  source,
  data,
  onCancel,
  onDelete,
}: {
  source: string;
  data: SettingsData;
  onCancel: () => void;
  onDelete: (withModels: boolean) => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // its models in agentline.toml are deleted; a built-in source's built-in models go with it
  const models = data.models.filter((m) => m.provider === source).map((m) => m.name);
  const uses = data.harnesses.filter((h) => h.model && models.includes(h.model));
  const defaults = uses.filter((h) => !h.implicit); // these block: pick another default first
  const losing = uses.filter((h) => h.implicit); // built-in harnesses just lose their default
  const builtin = data.providers.find((p) => p.name === source)?.builtin;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onCancel]);
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="delete-source-title">
      <div className="modal">
        <h2 id="delete-source-title">Remove model source {source}?</h2>
        {models.length > 0 ? (
          <>
            <p>
              Source <strong className="mono">{source}</strong> is used by {models.length === 1 ? "this model" : `these ${models.length} models`}:
            </p>
            <ul className="mono small">
              {models.map((m) => (
                <li key={m}>{m}</li>
              ))}
            </ul>
            <p>
              Removing the source removes {models.length === 1 ? "it" : "them"} too. Pipeline steps that name{" "}
              {models.length === 1 ? "it" : "one of them"} will need another model.
            </p>
          </>
        ) : (
          <p>
            No models use it.
          </p>
        )}
        {losing.length > 0 && (
          <p className="help">
            {losing.map((h) => `${h.name} (default ${h.model})`).join(", ")}: no default model afterwards.
          </p>
        )}
        {builtin && <p className="help">It is built in: Add a preset brings it back.</p>}
        {defaults.length > 0 && (
          <p className="issue">
            First pick another default model for{" "}
            {defaults.map((h) => `${h.name} (uses ${h.model})`).join(", ")} under Harnesses.
          </p>
        )}
        {err && <p className="issue">{err}</p>}
        <div className="modal-foot">
          <button type="button" onClick={onCancel} disabled={busy} autoFocus>
            Cancel
          </button>
          <button
            type="button"
            className="danger"
            disabled={busy || defaults.length > 0}
            onClick={() => {
              setBusy(true);
              setErr(null);
              onDelete(models.length > 0)
                .catch((e) => setErr(errText(e)))
                .finally(() => setBusy(false));
            }}
          >
            {busy ? "Removing…" : models.length ? `Remove source and ${models.length} model${models.length === 1 ? "" : "s"}` : "Remove source"}
          </button>
        </div>
      </div>
    </div>
  );
}


/** A subscription source's Claude Code login: who is signed in, and a way to sign out. */
export function ClaudeLoginPanel({
  provider,
  ready,
  onChanged,
}: {
  provider: string;
  ready?: boolean;
  onChanged: () => void; // signed in or out: the page's sources and models change
}) {
  const [login, setLogin] = useState<ClaudeLogin | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [asking, setAsking] = useState(false);
  const refresh = () => {
    setErr(null);
    api
      .claudeLogin(provider)
      .then(setLogin)
      .catch((e) => setErr(errText(e)));
  };
  useEffect(refresh, [provider]);
  useEffect(() => {
    if (login && ready !== undefined && login.logged_in !== ready) onChanged();
  }, [login]); // only when a new status arrives
  const signOut = () => {
    setBusy(true);
    setErr(null);
    api
      .claudeLogout(provider)
      .then(setLogin)
      .catch((e) => setErr(errText(e)))
      .finally(() => {
        setBusy(false);
        setAsking(false);
      });
  };
  return (
    <div className="field">
      <span className="field-name">Claude login</span>
      <div className="tester">
        {!login && !err && <Status s="running">checking</Status>}
        {login && !login.cli && <p className="issue">The Claude Code CLI isn't installed (see Test below).</p>}
        {login?.cli && login.logged_in && (
          <p className="help ok">
            Signed in{login.email ? ` as ${login.email}` : ""}
            {login.subscription ? ` (${login.subscription})` : ""}.
          </p>
        )}
        {login?.cli && !login.logged_in && (
          <p className="help">
            Not signed in{login.error ? `: ${login.error}` : "."} To sign in, run <code>claude auth login</code> in
            Terminal, then refresh.
          </p>
        )}
        {err && <p className="issue">{err}</p>}
        {asking ? (
          <div className="inline">
            <span className="small">
              Sign Claude Code out on this machine? Claude Code in Terminal is signed out too.
            </span>
            <button type="button" className="danger" disabled={busy} onClick={signOut}>
              {busy ? "Signing out…" : "Sign out"}
            </button>
            <button type="button" disabled={busy} onClick={() => setAsking(false)}>
              Cancel
            </button>
          </div>
        ) : (
          <div className="inline">
            {login?.logged_in && (
              <button type="button" onClick={() => setAsking(true)}>
                Sign out…
              </button>
            )}
            <button type="button" onClick={refresh}>
              Refresh
            </button>
          </div>
        )}
      </div>
    </div>
  );
}


/** A model source's models as a checklist: check several, Add puts them all in the Models tab
 * (names derived from the ids). Models already added are checked and greyed out. */
export function ModelCheckList({
  models,
  added,
  onAdd,
}: {
  models: TestedModel[];
  added: string[];
  /** adds the ids; returns the new model names */
  onAdd: (ids: string[]) => Promise<string[]>;
}) {
  const [query, setQuery] = useState("");
  const [freeOnly, setFreeOnly] = useState(false);
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const have = new Set(added);
  const shown = filterTested(models, query, freeOnly);
  const picked = [...sel].filter((id) => !have.has(id));
  const add = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const names = await onAdd(picked);
      setSel(new Set());
      setMsg({ ok: true, text: names.length ? `Added to Models: ${names.join(", ")}` : "Already in Models." });
    } catch (e) {
      setMsg({ ok: false, text: errText(e) });
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="pick-list">
      <div className="inline">
        <input type="search" placeholder="search models" value={query} onChange={(e) => setQuery(e.target.value)} />
        <label className="check">
          <input type="checkbox" checked={freeOnly} onChange={(e) => setFreeOnly(e.target.checked)} /> free only
        </label>
        <button type="button" onClick={() => setSel(selectShown(sel, shown.slice(0, 200), have))}>
          Select shown
        </button>
        {picked.length > 0 && (
          <button type="button" onClick={() => setSel(new Set())}>
            Clear
          </button>
        )}
      </div>
      <ul aria-label="models of this source">
        {shown.slice(0, 200).map((m) => {
          const isAdded = have.has(m.id);
          return (
            <li key={m.id} className={isAdded ? "added" : undefined}>
              {/* the whole row toggles: the label holds the badges too */}
              <label className="check">
                <input
                  type="checkbox"
                  aria-label={m.id}
                  checked={isAdded || sel.has(m.id)}
                  disabled={isAdded || busy}
                  onChange={() => setSel(toggled(sel, m.id))}
                />
                <code>{m.id}</code>
                {m.free && <span className="badge badge-free">free</span>}
                {isAdded && <span className="badge">added</span>}
                {m.context_length ? <span className="muted small">{Math.round(m.context_length / 1000)}k</span> : null}
              </label>
            </li>
          );
        })}
      </ul>
      {shown.length > 200 && <p className="help">{shown.length - 200} more: narrow the search</p>}
      {!shown.length && <p className="help">No models match.</p>}
      <div className="inline">
        <button type="button" className="primary" disabled={busy || picked.length === 0} onClick={() => void add()}>
          {busy ? "Adding…" : picked.length ? `Add ${picked.length} model${picked.length === 1 ? "" : "s"}` : "Add models"}
        </button>
        {msg && <span className={msg.ok ? "help ok" : "issue"}>{msg.text}</span>}
      </div>
    </div>
  );
}


export function KeysSection({
  keys,
  onKeys,
}: {
  keys: KeyStatus[];
  onKeys: (keys: KeyStatus[], note?: string) => void;
}) {
  const [editing, setEditing] = useState<string | null>(null);
  const [other, setOther] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const names = editing && !keys.some((k) => k.name === editing) ? [...keys, { name: editing, set: false, source: null }] : keys;
  return (
    <div className="settings-form wide">
      {names.length > 0 && (
      <table className="table keys">
        <thead>
          <tr>
            <th>Key</th>
            <th>Status</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {names.map((k) => (
            <tr key={k.name}>
              <td>
                <code>{k.name}</code>
              </td>
              <td>
                {k.set ? (
                  <span className="badge badge-set">set</span>
                ) : (
                  <span className="badge">not set</span>
                )}{" "}
                {k.source && <span className="muted small">from {k.source}</span>}
              </td>
              <td className="actions">
                {editing === k.name ? (
                  <SecretInput
                    name={k.name}
                    onDone={(r) => {
                      setEditing(null);
                      if (r) onKeys(r.keys, r.note);
                    }}
                  />
                ) : (
                  <>
                    <button type="button" onClick={() => setEditing(k.name)}>
                      Set key
                    </button>
                    {k.source === "secrets file" && (
                      <button
                        type="button"
                        className="danger"
                        onClick={() => {
                          if (!window.confirm(`Remove ${k.name} from the secrets file?`)) return;
                          api
                            .deleteSecret(k.name)
                            .then((r) => onKeys(r.keys))
                            .catch((e) => setErr(errText(e)));
                        }}
                      >
                        Remove
                      </button>
                    )}
                  </>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      )}
      {err && <p className="issue">{err}</p>}
      <form
        className="inline other-key"
        onSubmit={(e) => {
          e.preventDefault();
          const n = other.trim();
          if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(n)) return setErr("a key name is an env var name, e.g. MY_SERVER_KEY");
          setErr(null);
          setEditing(n);
          setOther("");
        }}
      >
        <input type="text" placeholder="a key name, e.g. JIRA_API_TOKEN" value={other} onChange={(e) => setOther(e.target.value)} />
        <button type="submit" disabled={!other.trim()}>
          Add
        </button>
      </form>
    </div>
  );
}

/** Password input for one key. The value is sent once and cleared; it is never kept or logged. */
export function SecretInput({
  name,
  onDone,
}: {
  name: string;
  onDone: (r: { keys: KeyStatus[]; note?: string } | null) => void;
}) {
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  return (
    <form
      className="inline secret"
      onSubmit={(e) => {
        e.preventDefault();
        setBusy(true);
        void submitSecret(name, draft, api.putSecret).then((r) => {
          setDraft(r.draft);
          setBusy(false);
          setErr(r.error ?? null);
          if (r.keys) onDone({ keys: r.keys }); // saved: the field clearing is the feedback
        });
      }}
    >
      <input
        type="password"
        value={draft}
        aria-label={`value of ${name}`}
        autoComplete="new-password"
        spellCheck={false}
        autoFocus
        onChange={(e) => setDraft(e.target.value)}
      />
      <button className="primary" type="submit" disabled={busy || !draft}>
        Save
      </button>
      <button
        type="button"
        onClick={() => {
          setDraft("");
          onDone(null);
        }}
      >
        Cancel
      </button>
      {err && <span className="issue">{err}</span>}
    </form>
  );
}


/** General > Other keys: keys that no page sets (Jira, your own names). Model source keys are set
 * under Models, MCP secrets in the MCP form, chat app tokens under Chat apps. */
export function OtherKeys({ keys, onKeys }: { keys: KeyStatus[]; onKeys: (keys: KeyStatus[]) => void }) {
  return (
    <section className="other-keys">
      <h3>Other keys</h3>
      <p className="help">
        Keys your pipelines or integrations use that no other page sets, like Jira&apos;s. Saved on this computer
        only; values are never shown again.
      </p>
      <KeysSection keys={keys.filter((k) => !k.owner)} onKeys={(all) => onKeys(all)} />
    </section>
  );
}
