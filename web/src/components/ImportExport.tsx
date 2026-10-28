// Pipeline export (a .zip with its files and what it needs) and import (api/bundles.py).
import { useRef, useState } from "react";
import { ApiError, api, type ImportResult } from "../api";
import { saveBlob } from "../lib/download";

export function ExportButton({ name }: { name: string }) {
  const [busy, setBusy] = useState(false);
  return (
    <button
      type="button"
      disabled={busy}
      title="Download this pipeline and its prompts, schemas and scripts as a .zip"
      onClick={async () => {
        setBusy(true);
        try {
          const { blob, filename } = await api.exportPipeline(name);
          await saveBlob(blob, filename);
        } catch (e) {
          window.alert(`Export failed: ${e instanceof ApiError ? e.message : String(e)}`);
        } finally {
          setBusy(false);
        }
      }}
    >
      {busy ? "Exporting…" : "Export"}
    </button>
  );
}

export function ImportButton({ onImported }: { onImported: (name: string) => void }) {
  const input = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  return (
    <>
      <button
        type="button"
        className="link"
        title="Import a pipeline .zip exported from agentline"
        onClick={() => input.current?.click()}
      >
        Import
      </button>
      <input
        ref={input}
        type="file"
        accept=".zip,application/zip"
        hidden
        aria-label="pipeline bundle to import"
        onChange={(e) => {
          setFile(e.target.files?.[0] ?? null);
          e.target.value = "";
        }}
      />
      {file && (
        <ImportDialog
          file={file}
          onClose={() => setFile(null)}
          onImported={(n) => {
            setFile(null);
            onImported(n);
          }}
        />
      )}
    </>
  );
}

function ImportDialog({
  file,
  onClose,
  onImported,
}: {
  file: File;
  onClose: () => void;
  onImported: (name: string) => void;
}) {
  const [folder, setFolder] = useState("");
  const [applySettings, setApplySettings] = useState(true);
  const [overwrite, setOverwrite] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ImportResult | null>(null);

  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      setResult(await api.importPipeline(file, { folder, overwrite, applySettings }));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const missing = result
    ? Object.entries(result.missing).filter(([, names]) => names.length > 0)
    : [];
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="import-title">
      <div className="modal">
        <h2 id="import-title">Import pipeline</h2>
        <p className="muted small">{file.name}</p>
        {!result ? (
          <>
            <label className="field">
              <span className="field-name">Folder</span>
              <input
                value={folder}
                placeholder="the bundle's folder name"
                onChange={(e) => setFolder(e.target.value)}
              />
            </label>
            <label>
              <input
                type="checkbox"
                checked={applySettings}
                onChange={(e) => setApplySettings(e.target.checked)}
              />{" "}
              Add the harnesses, models, model sources and MCP servers it brings (if missing here)
            </label>
            <label>
              <input
                type="checkbox"
                checked={overwrite}
                onChange={(e) => setOverwrite(e.target.checked)}
              />{" "}
              Replace the folder if it exists
            </label>
            {error && <p className="issue">{error}</p>}
            <div className="modal-foot">
              <button type="button" onClick={onClose}>
                Cancel
              </button>
              <button type="button" className="primary" disabled={busy} onClick={() => void run()}>
                {busy ? "Importing…" : "Import"}
              </button>
            </div>
          </>
        ) : (
          <>
            <p>
              Imported <strong>{result.pipeline ?? result.name}</strong> as{" "}
              <code>{result.name}.yaml</code>.
            </p>
            {result.applied.length > 0 && (
              <p className="small">Added settings: {result.applied.join(", ")}</p>
            )}
            {missing.length > 0 && (
              <div className="small">
                Still needed here:
                <ul>
                  {missing.map(([kind, names]) => (
                    <li key={kind}>
                      {kind}: {names.join(", ")}
                    </li>
                  ))}
                </ul>
                Set model keys under Settings → Models and others under Settings → General → Other keys, or ask the Chat.
              </div>
            )}
            {result.issues.length > 0 && (
              <p className="issue small">{result.issues.length} validation issue(s): see the editor.</p>
            )}
            <div className="modal-foot">
              <button type="button" className="primary" onClick={() => onImported(result.name)}>
                Open it
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
