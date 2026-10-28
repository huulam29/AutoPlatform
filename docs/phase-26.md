# Phase 26: Settings pages without the side panel

Agents, Model sources, Models, MCP and Skills no longer use a list rail with an edit panel
next to it. Each page now has the same layout (`web/src/components/SettingsRows.tsx`):

- a page header with the title, one line on what the page is for, and the primary action
  (+ New agent, + Custom source, + New model, + Add server, + New skill);
- an optional toolbar: "Chat uses" on Agents, the source presets on Model sources, and search
  on Skills;
- a full-width list of hairline rows: the name in mono, badges (built-in, subscription, free,
  stdio, the skill's library), then a one-line summary.

A row opens in place into its editor: the fields sit in a two-column grid and Save, Close and
Delete are at the bottom. A new entry opens as a row at the top. Copying a library skill
opens the editor under that skill. MCP rows keep their own buttons (On, Test, Edit, Delete)
and detail lines; add and edit still use the guided popup.

Field labels are now sentence case. The CSS for the old rail and panel (`.settings-list`,
`.settings-col`, `.mcp-list`) is gone, along with its size tokens. Keys, Chat apps and
General are unchanged.

Checked:
- each page in the dark and light themes;
- at 375 px (no horizontal scroll; a row's buttons wrap onto their own line);
- opening rows, a new row, and the Skills Copy flow.

Gates: `lint_hardcodes`, `check_no_emoji`, tsc, vitest and the build all pass.
