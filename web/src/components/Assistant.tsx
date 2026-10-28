// The Chat: it can do everything the GUI does (settings, pipelines, runs, problems, costs) by
// proposing cards the user applies; each applies through the same API as its GUI page
// (lib/assistant.ts applyAction). Key values are typed into a password field here and go
// straight to /api/secrets, never to the model.
import { Icon } from "./Icon";
import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { ApiError, api } from "../api";
import { ADD_HARNESS, chatDefaultModelLabel, chatModelOptions, modelAfterHarnessChange } from "../lib/ai";
import { AddHarnessDialog, AddHarnessOption } from "./AddHarness";
import { saveBlob } from "../lib/download";
import { isMacApp, nativeCall } from "../lib/native";
import {
  actionDetails,
  actionTitle,
  applyAction,
  assistantInbox,
  cardInput,
  needsOwnClick,
  type ApplyDeps,
  type Applied,
  autoApplicable,
  editorLink,
  loadChatModel,
  loadHarness,
  lookedAt,
  loadChat,
  pipelineName,
  saveChatModel,
  saveHarness,
  clearOldChat,
  forStorage,
  toHistory,
  transcriptByline,
  type ActionState,
  type AssistantAction,
  type ChatMessage,
  type ChatReply,
} from "../lib/assistant";
import { historyGroups, loadCurrent, newConversationId, saveCurrent } from "../lib/conversations";
import { reviewReply } from "../lib/review";
import { triageReply } from "../lib/triage";
import type { NodeTypes } from "../types";
import { ChatHistory } from "./ChatHistory";
import { PanelHeader } from "./Chrome";
import { DiffView } from "./DiffView";

function storage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

const STARTERS = [
  "Set up what I need to run pipelines",
  "Build a full dev cycle pipeline: plan, implement, review, deploy",
  "Import the MCP servers I already use",
  "What needs my attention?",
];

/** The API calls the cards use (the same ones the GUI pages use; triage cards elsewhere too). */
export const deps: ApplyDeps = {
  putSetting: api.putSetting,
  deleteSetting: api.deleteSetting,
  claudeLogout: api.claudeLogout,
  chooseModels: api.chooseModels,
  modelVisibility: api.modelVisibility,
  putSecret: api.putSecret,
  deleteSecret: api.deleteSecret,
  setupChoice: api.setupChoice,
  setVision: api.setVision,
  putChatInstructions: api.putChatInstructions,
  fetchSkills: api.fetchSkills,
  installSkills: api.installSkills,
  putSkill: api.putSkill,
  deleteSkill: api.deleteSkill,
  importMcp: api.importMcp,
  chatPair: (p) => api.chatPair(p) as Promise<{ code?: string; link?: string | null }>,
  chatTest: api.chatTest,
  chatUnlink: api.chatUnlink,
  chatNotify: api.chatNotify,
  native: (action) => nativeCall<{ path?: string }>(action),
  assistantApply: api.assistantApply,
  exportPipeline: async (name) => {
    const { blob, filename } = await api.exportPipeline(name);
    await saveBlob(blob, filename);
    return { filename };
  },
  importPipeline: api.importPipeline,
  dryRun: api.dryRun,
  historyRestore: api.historyRestore,
  deletePipeline: api.deletePipeline,
  startRun: api.startRun,
  sendRunMessage: api.sendRunMessage,
  retryRun: api.retryRun,
  approve: api.approve,
  reject: api.reject,
  updateIssue: api.updateIssue,
  deleteIncident: api.deleteIncident,
  clearIncidents: api.clearIncidents,
  setBudget: api.setBudget,
  addLesson: api.addLesson,
  triageRun: api.triageRun,
  triageGithub: api.triageGithub,
  reviewRun: api.reviewRun,
  reviewProbe: api.reviewProbe,
  lessonAdd: api.lessonAdd,
  githubSave: api.githubSave,
  githubLogin: api.githubLogin,
  githubTest: api.githubTest,
  archiveConversation: api.archiveConversation,
};

export function Assistant({
  types,
  onSettingsChanged,
  onPipelinesChanged,
}: {
  types: NodeTypes | null;
  onSettingsChanged: () => void;
  onPipelinesChanged: (open?: string) => void;
}) {
  // the open conversation (kept on the server: agentline.conversations); its id is made here
  // and the first save creates it
  const [convId, setConvId] = useState(() => loadCurrent(storage) || newConversationId());
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [loaded, setLoaded] = useState(false); // nothing is saved before the open one is read
  const [archived, setArchived] = useState(false); // the open one sits under Archived
  const [view, setView] = useState<"chat" | "history">("chat");
  const [historyTick, setHistoryTick] = useState(0);
  const [harness, setHarness] = useState(() => loadHarness(storage));
  const [model, setModel] = useState(() => loadChatModel(storage));
  const [draft, setDraft] = useState("");
  // the default (Settings → Harnesses → "Chat uses"), used when the pickers say "default"
  const [saved, setSaved] = useState<{ harness: string; model: string | null } | null>(null);
  useEffect(() => {
    api
      .setup()
      .then((st) => setSaved(st.choice ?? st.assistant ?? { harness: "claude-code", model: null }))
      .catch(() => setSaved({ harness: "claude-code", model: null }));
  }, [types]);
  const [pending, setPending] = useState<string[]>([]); // attached image ids for the next message
  const [uploading, setUploading] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const attach = async (files: Iterable<File>) => {
    const images = [...files].filter((f) => f.type.startsWith("image/"));
    if (!images.length) return;
    setUploading(true);
    setError(null);
    try {
      for (const f of images) {
        const r = await api.assistantAttach(f);
        setPending((p) => (p.includes(r.id) ? p : [...p, r.id]));
      }
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setUploading(false);
    }
  };
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const open = useSyncExternalStore(editorLink.subscribe, () =>
    editorLink.current(),
  );
  const end = useRef<HTMLDivElement>(null);
  const abort = useRef<AbortController | null>(null);
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    if (!busy) return;
    setElapsed(0);
    const t = setInterval(() => setElapsed((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, [busy]);

  // the messages as read from the server: not saved back
  const lastSaved = useRef<ChatMessage[] | null>(null);
  const show = (ms: ChatMessage[]) => {
    lastSaved.current = ms;
    setMessages(ms);
  };

  // Once: the chat kept in the browser before phase 48 becomes a conversation; else the
  // remembered one ("": a new chat), else the newest recent one.
  useEffect(() => {
    let stop = false;
    const old = loadChat(storage);
    clearOldChat(storage); // before any await: a second run of this effect must not copy it again
    const remembered = loadCurrent(storage);
    const open = async () => {
      if (old.length) {
        setMessages(old); // the save effect writes it under convId
        return;
      }
      if (remembered === "") return;
      if (remembered) {
        try {
          const c = await api.conversation(remembered);
          if (!stop) {
            show(c.messages);
            setArchived(c.archived);
          }
        } catch (e) {
          // not there: a new chat that was never saved. Any other failure: don't overwrite it
          if (!(e instanceof ApiError && e.status === 404) && !stop) setConvId(newConversationId());
        }
        return;
      }
      const { conversations } = await api.conversations();
      const newest = historyGroups(conversations, Date.now() / 1000).recent[0];
      if (newest && !stop) {
        const c = await api.conversation(newest.id);
        if (!stop) {
          setConvId(c.id);
          show(c.messages);
        }
      }
    };
    open()
      .catch(() => undefined) // the engine isn't answering: start empty
      .finally(() => !stop && setLoaded(true));
    return () => {
      stop = true;
    };
  }, []);

  // every change is written to the server, one save after the other
  const saving = useRef<Promise<unknown>>(Promise.resolve());
  useEffect(() => {
    if (!loaded || !messages.length || messages === lastSaved.current) return;
    lastSaved.current = messages;
    const id = convId;
    const body = forStorage(messages);
    saving.current = saving.current
      .then(() => api.saveConversation(id, body))
      .then((r) => setArchived(r.archived))
      .catch((e) => setError(`This chat couldn't be kept: ${e instanceof ApiError ? e.message : String(e)}`));
  }, [messages, convId, loaded]);
  const started = messages.length > 0;
  useEffect(() => {
    if (loaded) saveCurrent(storage, started ? convId : ""); // "": reopen as a new chat
  }, [convId, loaded, started]);

  const newChat = () => {
    setConvId(newConversationId());
    setMessages([]);
    setArchived(false);
    setError(null);
    setPending([]);
    setView("chat");
  };
  const openConversation = async (id: string) => {
    if (id === convId) {
      setView("chat");
      return;
    }
    try {
      const c = await api.conversation(id);
      setConvId(c.id);
      show(c.messages);
      setArchived(c.archived);
      setError(null);
      setPending([]);
      setView("chat");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
      setView("chat");
    }
  };
  const unarchive = async () => {
    try {
      const r = await api.archiveConversation(convId, false);
      setArchived(r.archived);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  };

  // block bodies: an effect must return nothing or a cleanup (newer browsers' scrollIntoView
  // returns a Promise, which React would call as the cleanup on unmount)
  useEffect(() => {
    saveHarness(storage, harness);
  }, [harness]);
  useEffect(() => {
    saveChatModel(storage, model);
  }, [model]);
  useEffect(() => {
    end.current?.scrollIntoView({ block: "end" });
  }, [messages.length, busy]);

  const harnesses = types?.ai?.harnesses.map((a) => a.name) ?? ["claude-code"];
  // the models listed are the ones this harness can use with a source that is ready
  const compatible = types?.ai?.compatible_models ?? {};
  const effective = harness || saved?.harness || "claude-code";
  const [adding, setAdding] = useState(false); // "+ Add a harness…" popup
  const pickHarness = (h: string) => {
    setHarness(h);
    setModel((m) => modelAfterHarnessChange(compatible, h || saved?.harness || "claude-code", m));
  };

  // "Fix this" and friends: send what they hand over (once the panel is idle)
  const sendRef = useRef<(text: string) => Promise<void>>(async () => {});
  useEffect(() => {
    const drain = () => {
      if (busy || !loaded) return; // after the open conversation is read, or it would replace them
      const next = assistantInbox.take();
      if (typeof next === "string") void sendRef.current(next);
      else if (next) {
        const reply = next;
        setMessages((ms) => [...ms, { role: "assistant", content: reply.message, reply, states: {} }]);
        drain();
      }
    };
    drain();
    return assistantInbox.subscribe(drain);
  }, [busy, loaded]);

  const send = async (text: string) => {
    const content = text.trim();
    if ((!content && !pending.length) || busy) return;
    const attachments = pending;
    const next: ChatMessage[] = [
      ...messages,
      {
        role: "user",
        content: content || "Build the pipeline shown in this image.",
        ...(attachments.length ? { attachments } : {}),
      },
    ];
    setPending([]);
    setMessages(next);
    setDraft("");
    setBusy(true);
    setError(null);
    try {
      const ctrl = new AbortController();
      abort.current = ctrl;
      const reply = await api.assistantChat(
        {
          harness: harness || undefined,
          model: model || undefined,
          messages: toHistory(next),
          pipeline: open?.name,
          source: open?.source,
        },
        ctrl.signal,
      );
      setMessages([
        ...next,
        { role: "assistant", content: reply.message, reply, states: {} },
      ]);
    } catch (e) {
      if ((e as Error).name === "AbortError") {
        setError("Cancelled. The harness was stopped; send another message when ready.");
      } else {
        setError(e instanceof ApiError ? e.message : String(e));
      }
    } finally {
      abort.current = null;
      setBusy(false);
    }
  };

  sendRef.current = send;

  const setState = (mi: number, ai: number, state: ActionState, err?: string, done?: Applied) =>
    setMessages((ms) =>
      ms.map((m, k) =>
        k === mi
          ? { ...m, states: { ...m.states, [ai]: { state, error: err, result: done?.result, run: done?.run } } }
          : m,
      ),
    );

  /** Apply one proposal; `input` is a key's value (typed here, never stored) or a file. */
  const apply = async (mi: number, ai: number, input: { value?: string; file?: Blob } = {}) => {
    const a = messages[mi]?.reply?.actions[ai];
    if (!a) return;
    setState(mi, ai, "applying");
    try {
      const r = await applyAction(a, deps, input);
      if (r.changed === "settings") onSettingsChanged();
      if (r.changed === "pipelines" && r.pipeline) {
        editorLink.notifyChanged(r.pipeline);
        onPipelinesChanged(r.pipeline);
      }
      setState(mi, ai, "applied", undefined, r);
      if (r.conversations && a.type === "conversation") {
        setHistoryTick((n) => n + 1);
        if (a.id === convId) setArchived(a.op === "archive");
      }
      if (r.triage) assistantInbox.push(triageReply(r.triage)); // its proposals as a new card
      if (r.review) assistantInbox.push(reviewReply(r.review));
    } catch (e) {
      setState(mi, ai, "failed", e instanceof ApiError || e instanceof Error ? e.message : String(e));
    }
  };

  const applyAll = async (mi: number) => {
    for (const ai of autoApplicable(messages[mi])) await apply(mi, ai);
  };

  return (
    <div className="assistant">
      <PanelHeader name="assistant" side="right" label="Chat" />
      {adding && (
        <AddHarnessDialog
          presets={types?.ai?.presets ?? []}
          onAdded={(name) => {
            setAdding(false);
            pickHarness(name);
          }}
          onCancel={() => setAdding(false)} // the previous choice stays
        />
      )}
      <div className="assistant-bar">
        <div className="assistant-pickers">
          <label className="small muted">
            Harness{" "}
            <select
              value={harness}
              onChange={(e) => (e.target.value === ADD_HARNESS ? setAdding(true) : pickHarness(e.target.value))}
              aria-label="Chat harness"
            >
              <option value="">default: {saved?.harness ?? "…"}</option>
              {harness && !harnesses.includes(harness) && <option value={harness}>{harness} (not set up)</option>}
              {harnesses.map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
              <AddHarnessOption presets={types?.ai?.presets} />
            </select>
          </label>
          <label className="small muted">
            Model{" "}
            <select value={model} onChange={(e) => setModel(e.target.value)} aria-label="Chat model">
              <option value="">{chatDefaultModelLabel(types?.ai, effective, harness ? null : saved)}</option>
              {chatModelOptions(compatible, effective, model).map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
        </div>
        <div className="assistant-bar-actions">
          <button
            type="button"
            className={`link ${view === "history" ? "active" : ""}`}
            disabled={busy}
            aria-pressed={view === "history"}
            title="Past chats, and the archived ones"
            onClick={() => setView((v) => (v === "history" ? "chat" : "history"))}
          >
            <Icon name="history" /> History
          </button>
          <button type="button" className="link" disabled={busy || (!messages.length && view === "chat")} onClick={newChat}>
            New chat
          </button>
        </div>
      </div>
      {view === "history" ? (
        <ChatHistory
          current={convId}
          refresh={historyTick}
          onOpen={(id) => void openConversation(id)}
          onNew={newChat}
          onArchived={(id, a) => id === convId && setArchived(a)}
        />
      ) : (
        <>
          {archived && messages.length > 0 && (
            <div className="assistant-archived small">
              <span className="muted">Archived: no activity for more than 7 days. Sending a message brings it back.</span>
              <button type="button" className="link" onClick={() => void unarchive()}>
                Unarchive
              </button>
            </div>
          )}
          <div className="assistant-log" aria-live="polite">
            {!messages.length && (
              <div className="assistant-empty">
                <p className="muted">
                  I can do what the app does: set up harnesses, models, keys, MCP
                  servers, skills and chat apps; build, change, dry run, import or
                  restore pipelines; start runs, retry them and answer gates; update
                  problems and the budget. Changes show up as cards for you to apply.
                  {open ? (
                    <>
                      {" "}
                      The open pipeline is <code>{open.name}</code>.
                    </>
                  ) : null}
                </p>
                {STARTERS.map((s) => (
                  <button
                    key={s}
                    type="button"
                    className="say-chip"
                    onClick={() => void send(s)}
                  >
                    {s}
                  </button>
                ))}
              </div>
            )}
            {messages.map((m, mi) => (
              <div key={mi} className={`msg msg-${m.role}`}>
                <div className="msg-text">{m.content || (m.reply ? "" : "…")}</div>
                {m.attachments && m.attachments.length > 0 && (
                  <div className="msg-images">
                    {m.attachments.map((id) => (
                      <img key={id} src={api.attachmentUrl(id)} alt="attached image" />
                    ))}
                  </div>
                )}
                {m.reply?.transcripts?.map((t) => (
                  <TranscriptCard
                    key={t.id}
                    t={t}
                    onChange={(next) =>
                      setMessages((ms) =>
                        ms.map((x, xi) =>
                          xi === mi && x.reply
                            ? { ...x, reply: { ...x.reply, transcripts: x.reply.transcripts?.map((y) => (y.id === next.id ? next : y)) } }
                            : x,
                        ),
                      )
                    }
                  />
                ))}
                {m.reply && m.reply.actions.length > 0 && (
                  <div className="actions">
                    {m.reply.actions.map((a, ai) => (
                      <ActionCard
                        key={ai}
                        action={a}
                        state={m.states?.[ai]?.state ?? "pending"}
                        error={m.states?.[ai]?.error}
                        result={m.states?.[ai]?.result}
                        run={m.states?.[ai]?.run}
                        onApply={(input) => void apply(mi, ai, input)}
                      />
                    ))}
                    {autoApplicable(m).length > 1 && (
                      <button type="button" onClick={() => void applyAll(mi)}>
                        Apply all ({autoApplicable(m).length})
                      </button>
                    )}
                  </div>
                )}
                {m.reply &&
                  mi === messages.length - 1 &&
                  m.reply.choices.length > 0 && (
                    <div className="choices">
                      {m.reply.choices.map((c) => (
                        <button
                          key={c}
                          type="button"
                          className="say-chip"
                          onClick={() => void send(c)}
                        >
                          {c}
                        </button>
                      ))}
                    </div>
                  )}
                {m.reply && (m.reply.cost_usd > 0 || m.reply.repaired) && (
                  <div className="small muted">
                    {m.reply.harness}
                    {m.reply.model ? ` (${m.reply.model})` : ""}
                    {m.reply.cost_usd > 0 && ` · $${m.reply.cost_usd.toFixed(3)}`}
                    {m.reply.repaired && " · fixed after validation"}
                  </div>
                )}
                {lookedAt(m.reply) && <div className="small muted">{lookedAt(m.reply)}</div>}
                {m.reply?.report && (
                  <div className="small muted">
                    Reported for agentline&apos;s maintainers: {m.reply.report.title}.{" "}
                    <a href="#/app-reports">App reports</a>
                  </div>
                )}
              </div>
            ))}
            {busy && (
              <div className="msg msg-assistant muted thinking">
                <span>Thinking… {elapsed}s</span>
                <button type="button" className="link" onClick={() => abort.current?.abort()}>
                  Cancel
                </button>
              </div>
            )}
            {error && (
              <div className="issue" role="alert">
                {error}
              </div>
            )}
            <div ref={end} />
          </div>
          {pending.length > 0 && (
            <div className="pending-images">
              {pending.map((id) => (
                <span key={id} className="pending-image">
                  <img src={api.attachmentUrl(id)} alt="image to send" />
                  <button
                    type="button"
                    aria-label="remove image"
                    onClick={() => setPending((p) => p.filter((x) => x !== id))}
                  >
                    ×
                  </button>
                </span>
              ))}
            </div>
          )}
          <form
            className="assistant-input"
            onSubmit={(e) => {
              e.preventDefault();
              void send(draft);
            }}
            onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => {
              e.preventDefault();
              void attach(e.dataTransfer.files);
            }}
          >
            <button
              type="button"
              className="attach"
              title="Attach an image (a pipeline diagram, a screenshot); or paste / drop one"
              aria-label="attach an image"
              disabled={busy || uploading}
              onClick={() => fileInput.current?.click()}
            >
              {uploading ? "…" : <Icon name="paperclip" />}
            </button>
            <input
              ref={fileInput}
              type="file"
              accept="image/png,image/jpeg,image/gif,image/webp"
              multiple
              hidden
              onChange={(e) => {
                if (e.target.files) void attach(e.target.files);
                e.target.value = "";
              }}
            />
            <textarea
              onPaste={(e) => {
                const files = [...e.clipboardData.files];
                if (files.some((f) => f.type.startsWith("image/"))) {
                  e.preventDefault();
                  void attach(files);
                }
              }}
              value={draft}
              placeholder="Ask, or describe the pipeline you want…"
              aria-label="message to the Chat"
              rows={3}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (
                  e.key === "Enter" &&
                  !e.shiftKey &&
                  !e.nativeEvent.isComposing
                ) {
                  e.preventDefault();
                  void send(draft);
                }
              }}
            />
            <button
              type="submit"
              className="primary"
              disabled={busy || uploading || (!draft.trim() && !pending.length)}
            >
              Send
            </button>
          </form>
        </>
      )}
    </div>
  );
}

export function ActionCard({
  action: a,
  state,
  error,
  result,
  run,
  onApply,
}: {
  action: AssistantAction;
  state: ActionState;
  error?: string;
  result?: string;
  run?: string;
  onApply: (input: { value?: string; file?: Blob }) => void;
}) {
  const [value, setValue] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [showYaml, setShowYaml] = useState(false);
  const done = state === "applied";
  const invalid = a.issues.length > 0;
  const input = cardInput(a);
  const details = actionDetails(a);
  const native = a.type === "engine" && !isMacApp();
  return (
    <div className={`action-card ${done ? "done" : ""}`}>
      <div className="action-head">
        <strong>{actionTitle(a)}</strong>
        {done && <span className="badge">applied</span>}
        {!done && needsOwnClick(a) && <span className="badge" title="Not part of Apply all">own click</span>}
      </div>
      {"why" in a && a.why && <p className="small muted">{a.why}</p>}
      {a.type === "skill" && <pre className="mono small action-yaml">{a.body}</pre>}
      {details.length > 0 && <pre className="mono small action-yaml">{details.join("\n")}</pre>}
      {a.type === "pipeline" && (
        <>
          {a.summary && <p className="small">{a.summary}</p>}
          <p className="small muted">
            {a.exists ? "Replaces the file" : "New file"}
            {a.files &&
              Object.keys(a.files).length > 0 &&
              ` + ${Object.keys(a.files).join(", ")}`}
          </p>
          <button
            type="button"
            className="link"
            onClick={() => setShowYaml((v) => !v)}
          >
            {showYaml ? "Hide" : a.exists ? "Show changes" : "Show YAML"}
          </button>
          {showYaml && (a.exists ? <ProposalDiff action={a} /> : <pre className="mono small action-yaml">{a.yaml}</pre>)}
        </>
      )}
      {done && a.type === "pipeline" && (
        <a className="small" href={`#/pipelines/${pipelineName(a.path)}`}>
          Open it
        </a>
      )}
      {done && (a.type === "restore" || a.type === "dry_run" || a.type === "export") && (
        <a className="small" href={`#/pipelines/${a.pipeline}`}>
          Open {a.pipeline}
        </a>
      )}
      {done && run && (
        <a className="small" href={`#/runs/${encodeURIComponent(run)}`}>
          Open the run
        </a>
      )}
      {result && <p className="small">{result}</p>}
      {a.issues.map((i, k) => (
        <p key={k} className="issue small">
          {i}
        </p>
      ))}
      {native && <p className="small muted">This one needs the Mac app.</p>}
      {error && <p className="issue small">{error}</p>}
      {!done && input === "secret" && (
        <input
          type="password"
          autoComplete="off"
          value={value}
          placeholder="paste the key"
          aria-label={`value for ${"name" in a ? a.name : "the key"}`}
          onChange={(e) => setValue(e.target.value)}
        />
      )}
      {!done && input === "file" && (
        <input
          type="file"
          accept=".zip,application/zip"
          aria-label="pipeline bundle to import"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
        />
      )}
      {!done && (
        <button
          type="button"
          disabled={
            state === "applying" ||
            native ||
            (input === "secret" && !value) ||
            (input === "file" && !file) ||
            (invalid && a.type !== "pipeline")
          }
          title={
            invalid && a.type === "pipeline"
              ? "It has issues; you can fix them in the editor"
              : undefined
          }
          onClick={() => {
            onApply(input === "secret" ? { value } : input === "file" && file ? { file } : {});
            if (input === "secret") setValue("");
          }}
        >
          {state === "applying"
            ? "Applying…"
            : invalid && a.type === "pipeline"
              ? "Apply anyway"
              : "Apply"}
        </button>
      )}
    </div>
  );
}

/** An update to an existing pipeline: the change against the file on disk. */
function ProposalDiff({ action }: { action: Extract<AssistantAction, { type: "pipeline" }> }) {
  const [current, setCurrent] = useState<string | null>(null);
  useEffect(() => {
    api
      .pipeline(pipelineName(action.path))
      .then((p) => setCurrent(p.source))
      .catch(() => setCurrent(""));
  }, [action.path]);
  if (current === null) return <p className="muted small">Loading…</p>;
  return (
    <div className="action-yaml">
      <DiffView before={current} after={action.yaml} />
    </div>
  );
}

type Transcript = NonNullable<ChatReply["transcripts"]>[number];

/** What a vision model read in an image the Chat's harness can't see: check it, correct it, or
 * have it read again. A correction is used from the next message on. */
function TranscriptCard({ t, onChange }: { t: Transcript; onChange: (t: Transcript) => void }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(t.text);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const run = async (f: () => Promise<Transcript>) => {
    setBusy(true);
    setErr(null);
    try {
      const next = await f();
      onChange(next);
      setText(next.text);
      setEditing(false);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="action-card transcript-card">
      <div className="action-head">
        <img className="transcript-thumb" src={api.attachmentUrl(t.id)} alt="the attached image" />
        Image transcript
      </div>
      <span className="help">{transcriptByline(t)}. The Chat builds from this text, so fix anything it misread.</span>
      {editing ? (
        <textarea className="transcript-edit" aria-label="image transcript" value={text} rows={10} onChange={(e) => setText(e.target.value)} />
      ) : (
        <details>
          <summary>Show what it read</summary>
          <pre>{t.text}</pre>
        </details>
      )}
      <div className="row">
        {editing ? (
          <>
            <button type="button" className="primary" disabled={busy || !text.trim()} onClick={() => void run(() => api.putTranscript(t.id, text))}>
              Save correction
            </button>
            <button type="button" disabled={busy} onClick={() => { setEditing(false); setText(t.text); }}>
              Cancel
            </button>
          </>
        ) : (
          <>
            <button type="button" disabled={busy} onClick={() => setEditing(true)}>
              Correct it
            </button>
            <button type="button" disabled={busy} title="Have the vision model read the image again" onClick={() => void run(() => api.redoTranscript(t.id))}>
              {busy ? "Reading…" : "Read again"}
            </button>
          </>
        )}
      </div>
      {err && <p className="issue small">{err}</p>}
    </div>
  );
}
