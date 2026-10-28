// Start a run: pick a pipeline, give its input (JSON from a template, text, or a first chat
// message: the pipeline's start point, src/agentline/pipeline/start.py), go.
import { useEffect, useState } from "react";
import { ApiError, api } from "../api";
import { emptyFields, initialText, startBody } from "../lib/start";
import type { StartInfo, StartInput } from "../types";

export function StartRunButton({ onStarted }: { onStarted: (runId: string) => void }) {
  const [open, setOpen] = useState(false);
  // #/runs/new (the Mac app's File > Start Run…) opens the dialog
  useEffect(() => {
    const on = () => {
      if (window.location.hash !== "#/runs/new") return;
      setOpen(true);
      window.location.replace("#/runs");
    };
    on();
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return (
    <>
      <button type="button" className="primary" title="Start a run of a pipeline" onClick={() => setOpen(true)}>
        Start run
      </button>
      {open && (
        <StartRunDialog
          onClose={() => setOpen(false)}
          onStarted={(id) => {
            setOpen(false);
            onStarted(id);
          }}
        />
      )}
    </>
  );
}

const INPUT_HELP: Record<StartInput, string> = {
  json: "Fill in the JSON. The fields are the ones the pipeline reads ({{payload.…}}).",
  text: "Write what the run should do, in your own words. The start node's harness gets it.",
  chat: "Say what you need. The gatekeeper asks follow-ups here before the pipeline starts, then stays in the run's Conversation.",
};

export function StartRunDialog({ onClose, onStarted }: { onClose: () => void; onStarted: (id: string) => void }) {
  const [pipelines, setPipelines] = useState<string[]>([]);
  const [pipeline, setPipeline] = useState("");
  const [info, setInfo] = useState<StartInfo | null>(null);
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    api
      .status()
      .then((s) => {
        setPipelines(s.pipelines);
        setPipeline((p) => p || s.pipelines[0] || "");
      })
      .catch((e) => setError(String((e as Error).message)));
  }, []);
  useEffect(() => {
    if (!pipeline) return;
    let live = true;
    setInfo(null);
    api
      .startInfo(pipeline)
      .then((i) => {
        if (!live) return;
        setInfo(i);
        setText(initialText(i));
      })
      .catch(() => {
        if (!live) return;
        const fallback: StartInfo = { node: null, input: "json", template: {}, fields: [] };
        setInfo(fallback);
        setText(initialText(fallback));
      });
    return () => {
      live = false;
    };
  }, [pipeline]);
  const input: StartInput = info?.input ?? "json";
  const empty = input === "json" ? emptyFieldsOf(text) : [];
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="start-title">
      <form
        className="modal"
        onSubmit={async (e) => {
          e.preventDefault();
          const body = startBody(input, text);
          if ("error" in body) {
            setError(body.error);
            return;
          }
          setBusy(true);
          setError(null);
          try {
            const r = "text" in body ? await api.startRun(pipeline, null, body.text) : await api.startRun(pipeline, body.payload);
            onStarted(r.run_id);
          } catch (x) {
            setError(x instanceof ApiError ? x.message : String(x));
          } finally {
            setBusy(false);
          }
        }}
      >
        <h2 id="start-title">Start a run</h2>
        <label className="field">
          <span className="field-name">Pipeline</span>
          <select value={pipeline} onChange={(e) => setPipeline(e.target.value)}>
            {pipelines.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </label>
        {pipelines.length === 0 && (
          <p className="small muted">No pipelines are loaded by the engine yet. Save one in the editor: the engine picks it up right away.</p>
        )}
        {info && (
          <div className="field">
            <span className="field-name">
              {input === "chat" ? "Message" : input === "text" ? "Input (text)" : "Payload (JSON)"}
              {info.node && <span className="muted"> · taken by {info.node}</span>}
            </span>
            <textarea
              className={input === "json" ? "mono" : undefined}
              rows={input === "json" ? Math.min(14, Math.max(4, text.split("\n").length + 1)) : 5}
              value={text}
              aria-label={input === "json" ? "payload" : "message"}
              placeholder={input === "chat" ? "What do you need?" : input === "text" ? "What should the run do?" : "{}"}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                if (input !== "json" && e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                  e.preventDefault();
                  e.currentTarget.form?.requestSubmit();
                }
              }}
            />
            <span className="help">{INPUT_HELP[input]}</span>
            {empty.length > 0 && <span className="help">Still empty: {empty.join(", ")}</span>}
          </div>
        )}
        {error && <p className="issue">{error}</p>}
        <div className="modal-foot row">
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="primary" disabled={busy || !pipeline || !info}>
            {busy ? "Starting…" : input === "chat" ? "Send" : "Start"}
          </button>
        </div>
      </form>
    </div>
  );
}

function emptyFieldsOf(text: string): string[] {
  try {
    return emptyFields(JSON.parse(text) as unknown);
  } catch {
    return [];
  }
}
