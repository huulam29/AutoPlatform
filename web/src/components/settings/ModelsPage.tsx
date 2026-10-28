// Settings > Models (phase 46): only what is usable, like VS Code's Language Models. A group per
// connected model source with its models (eye: show / hide in pickers), "Needs attention" for
// sources you set up that stopped working, and one "Add models" flow (AddModelsDialog) that
// connects a source and picks its models. Sources nobody connected appear only in that flow.
import { useEffect, useState } from "react";
import { api } from "../../api";
import { isFree, modelGroups, needsAttention, sourceTitle, type ModelGroup } from "../../lib/models";
import type { ModelInfo, Settings as SettingsData } from "../../types";
import { Icon } from "../Icon";
import { AddModelsDialog, type AddStart } from "./AddModelsDialog";
import { ClaudeLoginPanel, DeleteSourceDialog, SecretInput, errText } from "./parts";

export function ModelsPage({
  data,
  onApplied,
  onReload,
}: {
  data: SettingsData;
  /** a save returned the new settings */
  onApplied: (r: SettingsData) => void;
  /** something changed outside a save (a sign-in): fetch the settings again */
  onReload: () => void;
}) {
  const [query, setQuery] = useState("");
  const [adding, setAdding] = useState<AddStart | null>(null);
  const [removing, setRemoving] = useState<string | null>(null); // a source to confirm
  const [err, setErr] = useState<string | null>(null);
  const groups = modelGroups(data, query);
  const attention = needsAttention(data);
  const empty = !groups.length && !attention.length && !query.trim();

  const setHidden = (m: ModelInfo, hidden: boolean) => {
    setErr(null);
    api
      .modelVisibility(m.name, hidden)
      .then(onApplied)
      .catch((e) => setErr(errText(e)));
  };
  const removeModel = (m: ModelInfo) => {
    if (!window.confirm(`Remove ${m.name}? Pipeline steps that name it will need another model.`)) return;
    api
      .deleteSetting("models", m.name)
      .then(onApplied)
      .catch((e) => setErr(errText(e)));
  };

  return (
    <div className="models-page">
      <div className="models-head">
        <div>
          <h2>Models</h2>
          <p className="muted small">The models your pipelines and the Chat can use, grouped by where they come from.</p>
        </div>
        <button type="button" className="primary" onClick={() => setAdding({})}>
          <Icon name="plus" /> Add models
        </button>
      </div>
      {!empty && (
        <label className="models-search">
          <Icon name="search" />
          <span className="sr-only">Search models</span>
          <input
            type="search"
            placeholder="Search models, ids or sources (try: free)"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </label>
      )}
      {err && <p className="issue">{err}</p>}
      {attention.map((p) => (
        <div key={p.name} className="models-attention" role="status">
          <Icon name="alert" />
          <strong>Needs attention</strong>
          <span>
            {sourceTitle(p)}: {p.not_ready ?? "can't be used right now"}. Its models are hidden from pickers until
            it&apos;s fixed.
          </span>
          <button type="button" onClick={() => setAdding({ provider: p.name, step: "connect" })}>
            Reconnect
          </button>
          <button type="button" className="link" onClick={() => setRemoving(p.name)}>
            Remove
          </button>
        </div>
      ))}
      {empty && (
        <div className="models-empty">
          <h3>Connect your first model</h3>
          <p className="muted">
            Add an API key (OpenRouter has free models), your Claude login, or a model server on this computer. The
            built-in harness needs one.
          </p>
          <button type="button" className="primary" onClick={() => setAdding({})}>
            Add models
          </button>
        </div>
      )}
      {groups.map((g) => (
        <SourceGroup
          key={g.provider.name}
          group={g}
          onAddMore={() => setAdding({ provider: g.provider.name, step: "models" })}
          onEdit={() => setAdding({ provider: g.provider.name, step: "connect" })}
          onRemove={() => setRemoving(g.provider.name)}
          onHidden={setHidden}
          onRemoveModel={removeModel}
          onKeySaved={onReload}
          onLoginChanged={onReload}
        />
      ))}
      {!empty && !groups.length && query.trim() && <p className="muted">No models match “{query.trim()}”.</p>}
      {!empty && (
        <p className="muted small">Sources you haven&apos;t connected don&apos;t appear here: Add models connects one.</p>
      )}
      {adding && (
        <AddModelsDialog
          data={data}
          start={adding}
          onApplied={onApplied}
          onReload={onReload}
          onClose={() => setAdding(null)}
        />
      )}
      {removing && (
        <DeleteSourceDialog
          source={removing}
          data={data}
          onCancel={() => setRemoving(null)}
          onDelete={async (withModels) => {
            onApplied(await api.deleteSetting("providers", removing, withModels));
            setRemoving(null);
          }}
        />
      )}
    </div>
  );
}

function SourceGroup({
  group: { provider: p, models },
  onAddMore,
  onEdit,
  onRemove,
  onHidden,
  onRemoveModel,
  onKeySaved,
  onLoginChanged,
}: {
  group: ModelGroup;
  onAddMore: () => void;
  onEdit: () => void;
  onRemove: () => void;
  onHidden: (m: ModelInfo, hidden: boolean) => void;
  onRemoveModel: (m: ModelInfo) => void;
  onKeySaved: () => void;
  onLoginChanged: () => void;
}) {
  const [open, setOpen] = useState(true);
  const [menu, setMenu] = useState(false);
  const [panel, setPanel] = useState<"key" | "login" | null>(null);
  useEffect(() => {
    if (!menu) return;
    const close = (e: KeyboardEvent) => e.key === "Escape" && setMenu(false);
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [menu]);
  const subscription = p.auth === "subscription";
  const custom = !p.builtin; // a source you named: its URL can change
  const pick = (f: () => void) => () => {
    setMenu(false);
    f();
  };
  return (
    <section className="source-group" aria-label={sourceTitle(p)}>
      <div className="source-head">
        <button
          type="button"
          className="source-toggle"
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
        >
          <span className={open ? "" : "rot"}>
            <Icon name="chevronDown" />
          </span>
          <strong>{sourceTitle(p)}</strong>
        </button>
        <span className="source-ok">
          <span className="dot" aria-hidden="true" />
          {subscription ? "Signed in" : "Connected"}
        </span>
        <span className="muted small">
          {models.length} model{models.length === 1 ? "" : "s"}
          {subscription ? " · Claude Code harness only" : ""}
        </span>
        <div className="source-menu">
          <button
            type="button"
            className="icon-btn"
            aria-haspopup="menu"
            aria-expanded={menu}
            aria-label={`${sourceTitle(p)} options`}
            onClick={() => setMenu((v) => !v)}
          >
            <Icon name="more" />
          </button>
          {menu && (
            <div className="menu" role="menu">
              <button type="button" role="menuitem" onClick={pick(onAddMore)}>
                Add more models
              </button>
              {p.api_key_env && (
                <button type="button" role="menuitem" onClick={pick(() => setPanel("key"))}>
                  Change key
                </button>
              )}
              {subscription && (
                <button type="button" role="menuitem" onClick={pick(() => setPanel("login"))}>
                  Claude login
                </button>
              )}
              {custom && !subscription && (
                <button type="button" role="menuitem" onClick={pick(onEdit)}>
                  Change URL
                </button>
              )}
              <button type="button" role="menuitem" className="danger" onClick={pick(onRemove)}>
                Disconnect
              </button>
            </div>
          )}
        </div>
      </div>
      {panel === "key" && p.api_key_env && (
        <div className="source-panel">
          <span className="small">
            New value for <code>{p.api_key_env}</code>
          </span>
          <SecretInput
            name={p.api_key_env}
            onDone={(r) => {
              setPanel(null);
              if (r) onKeySaved();
            }}
          />
        </div>
      )}
      {panel === "login" && (
        <div className="source-panel">
          <ClaudeLoginPanel provider={p.name} ready={p.connected ?? p.ready} onChanged={onLoginChanged} />
          <button type="button" onClick={() => setPanel(null)}>
            Close
          </button>
        </div>
      )}
      {open && (
        <ul className="model-rows">
          {models.map((m) => (
            <ModelRow key={m.name} m={m} onHidden={onHidden} onRemove={onRemoveModel} />
          ))}
          {!models.length && (
            <li className="model-row muted">
              No models from this source yet.{" "}
              <button type="button" className="link" onClick={onAddMore}>
                Add models
              </button>
            </li>
          )}
        </ul>
      )}
    </section>
  );
}

function ModelRow({
  m,
  onHidden,
  onRemove,
}: {
  m: ModelInfo;
  onHidden: (m: ModelInfo, hidden: boolean) => void;
  onRemove: (m: ModelInfo) => void;
}) {
  const hidden = !!m.hidden;
  return (
    <li className={`model-row${hidden ? " hidden" : ""}`}>
      <span className="model-name">{m.name}</span>
      <code className="model-id">{m.model}</code>
      {isFree(m) && <span className="badge badge-free">free</span>}
      {hidden && <span className="small muted">hidden from pickers</span>}
      <span className="spacer" />
      <button
        type="button"
        className="icon-btn"
        aria-pressed={hidden}
        aria-label={hidden ? `Show ${m.name} in pickers` : `Hide ${m.name} from pickers`}
        title={hidden ? "Show in pickers" : "Hide from pickers"}
        onClick={() => onHidden(m, !hidden)}
      >
        <Icon name={hidden ? "eyeOff" : "eye"} />
      </button>
      {!m.implicit && (
        <button
          type="button"
          className="icon-btn"
          aria-label={`Remove ${m.name}`}
          title="Remove"
          onClick={() => onRemove(m)}
        >
          <Icon name="x" />
        </button>
      )}
    </li>
  );
}
