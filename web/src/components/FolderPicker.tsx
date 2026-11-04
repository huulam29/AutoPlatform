// Choose a folder on the engine's machine (spec 006 US3): the browser's stand-in for the Mac
// app's folder panel. Lists folders only, from GET /api/app/folders.
import { useEffect, useState } from "react";
import { api, type FolderListing } from "../api";

export function FolderPicker({
  start,
  title,
  onChoose,
  onCancel,
}: {
  start: string;
  title: string;
  onChoose: (path: string) => void;
  onCancel: () => void;
}) {
  const [listing, setListing] = useState<FolderListing | null>(null);
  const [typed, setTyped] = useState(start);
  const [error, setError] = useState<string | null>(null);
  const open = async (path: string) => {
    try {
      const r = await api.folders(path);
      setListing(r);
      setTyped(r.path);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  };
  useEffect(() => {
    void open(start);
  }, [start]);
  return (
    <div
      className="modal-backdrop"
      role="dialog"
      aria-modal="true"
      aria-labelledby="folder-picker-title"
      onKeyDown={(e) => {
        if (e.key === "Escape") onCancel();
      }}
    >
      <div className="modal">
        <h2 id="folder-picker-title">{title}</h2>
        <form
          className="row"
          onSubmit={(e) => {
            e.preventDefault();
            void open(typed);
          }}
        >
          <input className="mono grow" aria-label="folder path" value={typed} onChange={(e) => setTyped(e.target.value)} />
          <button type="submit">Go</button>
        </form>
        <div className="row">
          <button type="button" disabled={!listing?.parent} onClick={() => listing?.parent && void open(listing.parent)}>
            Up
          </button>
          <button type="button" onClick={() => listing && void open(listing.home)}>
            Home
          </button>
        </div>
        {error && <p className="issue small">{error}</p>}
        <ul className="folder-list" aria-label="folders">
          {listing?.folders.length === 0 && <li className="muted small">No folders here.</li>}
          {listing?.folders.map((f) => (
            <li key={f.path}>
              <button
                type="button"
                className="link"
                disabled={!f.readable}
                title={f.readable ? f.path : "Not readable"}
                onClick={() => void open(f.path)}
              >
                {f.name}
              </button>
            </li>
          ))}
        </ul>
        <div className="modal-foot">
          <button type="button" onClick={onCancel}>
            Cancel
          </button>
          <button type="button" className="primary" disabled={!listing} onClick={() => listing && onChoose(listing.path)}>
            Choose this folder
          </button>
        </div>
      </div>
    </div>
  );
}
