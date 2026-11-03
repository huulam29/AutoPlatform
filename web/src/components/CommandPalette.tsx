// Cmd/Ctrl+K: every Mac app menu action from the keyboard (spec 006 US6).
import { useEffect, useMemo, useRef, useState } from "react";
import { filterCommands, type Command } from "../lib/commands";

export function CommandPalette({ commands, onClose }: { commands: Command[]; onClose: () => void }) {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const shown = useMemo(() => filterCommands(commands, query), [commands, query]);
  useEffect(() => {
    input.current?.focus();
  }, []);
  useEffect(() => setActive(0), [query]);
  const run = (c: Command | undefined) => {
    if (!c) return;
    onClose();
    c.run();
  };
  return (
    <div
      className="modal-backdrop palette-backdrop"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="palette" role="dialog" aria-modal="true" aria-label="Commands">
        <input
          ref={input}
          className="palette-input"
          role="combobox"
          aria-expanded="true"
          aria-controls="palette-list"
          aria-activedescendant={shown[active] ? `cmd-${shown[active].id}` : undefined}
          placeholder="Type a command"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") {
              e.preventDefault();
              setActive((i) => Math.min(i + 1, shown.length - 1));
            } else if (e.key === "ArrowUp") {
              e.preventDefault();
              setActive((i) => Math.max(i - 1, 0));
            } else if (e.key === "Enter") {
              e.preventDefault();
              run(shown[active]);
            } else if (e.key === "Escape") {
              e.preventDefault();
              onClose();
            }
          }}
        />
        <ul id="palette-list" className="palette-list" role="listbox" aria-label="Commands">
          {shown.length === 0 && <li className="muted small palette-empty">No matching command</li>}
          {shown.map((c, i) => (
            <li
              key={c.id}
              id={`cmd-${c.id}`}
              role="option"
              aria-selected={i === active}
              className={i === active ? "on" : ""}
              onMouseEnter={() => setActive(i)}
              onMouseDown={(e) => {
                e.preventDefault();
                run(c);
              }}
            >
              <span>{c.title}</span>
              <span className="palette-meta">
                <span className="muted small">{c.section}</span>
                {c.shortcut && <kbd>{c.shortcut}</kbd>}
              </span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
