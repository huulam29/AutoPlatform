// A path from the engine's machine, with Copy and (on that machine) Show in folder: what a
// browser offers where the Mac app has "Show in Finder" (spec 006 FR-012).
import { useState } from "react";
import { api } from "../api";
import { copyText } from "../lib/clipboard";

export function CopyPath({
  path,
  canReveal,
  target,
  label,
}: {
  path: string;
  canReveal?: boolean;
  /** what Show in folder opens (else `path`) */
  target?: "data" | "pipelines" | "config" | "incidents";
  label?: string;
}) {
  const [note, setNote] = useState<string | null>(null);
  return (
    <span className="copy-path">
      <span className="mono small">{label ?? path}</span>
      <button
        type="button"
        className="link"
        onClick={async () => setNote((await copyText(path)) ? "Copied" : "Couldn't copy")}
      >
        Copy
      </button>
      {canReveal && (
        <button
          type="button"
          className="link"
          onClick={async () => {
            try {
              await api.reveal(target ? { target } : { path });
              setNote(null);
            } catch (e) {
              setNote((e as Error).message);
            }
          }}
        >
          Show in folder
        </button>
      )}
      {note && <span className="small muted">{note}</span>}
    </span>
  );
}
