// Pipeline history (api/history.py): every save is a version; look at one, restore it.
import { useEffect, useState } from "react";
import { ApiError, api } from "../api";
import { formatTime } from "../lib/runs";
import { DiffView } from "./DiffView";

export function HistoryButton({ name, current, onRestored }: { name: string; current: string; onRestored: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" title="Earlier versions of this pipeline" onClick={() => setOpen(true)}>
        History
      </button>
      {open && <HistoryDialog name={name} current={current} onClose={() => setOpen(false)} onRestored={onRestored} />}
    </>
  );
}

function HistoryDialog({
  name,
  current,
  onClose,
  onRestored,
}: {
  name: string;
  current: string;
  onClose: () => void;
  onRestored: () => void;
}) {
  const [commits, setCommits] = useState<{ commit: string; at: number; message: string }[] | null>(null);
  const [available, setAvailable] = useState(true);
  const [picked, setPicked] = useState<string | null>(null);
  const [source, setSource] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api
      .history(name)
      .then((r) => {
        setCommits(r.commits);
        setAvailable(r.available);
      })
      .catch((e) => setError(e instanceof ApiError ? e.message : String(e)));
  }, [name]);
  useEffect(() => {
    if (!picked) return;
    setSource(null);
    api
      .historyShow(name, picked)
      .then((r) => setSource(r.source))
      .catch((e) => setError(e instanceof ApiError ? e.message : String(e)));
  }, [name, picked]);
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="hist-title">
      <div className="modal wide">
        <h2 id="hist-title">History: {name}</h2>
        {!available && <p className="issue">History needs git on this machine.</p>}
        {error && <p className="issue">{error}</p>}
        <div className="history">
          <ol className="history-list">
            {commits?.map((c) => (
              <li key={c.commit}>
                <button type="button" className={c.commit === picked ? "on" : ""} onClick={() => setPicked(c.commit)}>
                  <span className="small muted">{formatTime(c.at)}</span> {c.message}
                </button>
              </li>
            ))}
            {commits?.length === 0 && <li className="muted">No earlier versions yet.</li>}
          </ol>
          <div className="history-diff">
            {picked && source !== null ? (
              <>
                <p className="small muted">From this version to the current file:</p>
                <DiffView before={source} after={current} />
              </>
            ) : (
              <p className="muted small">Pick a version to compare it with the current file.</p>
            )}
          </div>
        </div>
        <div className="modal-foot row">
          <button type="button" onClick={onClose}>
            Close
          </button>
          <button
            type="button"
            className="primary"
            disabled={!picked || source === null}
            onClick={async () => {
              if (!picked) return;
              try {
                await api.historyRestore(name, picked);
                onRestored();
                onClose();
              } catch (e) {
                setError(e instanceof ApiError ? e.message : String(e));
              }
            }}
          >
            Restore this version
          </button>
        </div>
      </div>
    </div>
  );
}
