# Phase 21: Flight Deck redesign

Approved direction: Flight Deck (docs/redesign-proposal.md; final mock
https://claude.ai/artifact/A3DsUeYHQWNPQe4ni73Rbz). Decisions 58-59.

## What changed
- `web/src/theme.css`: the one token source. Primitives (obsidian/navy/ice/cyan/amber/red/mint),
  semantic tokens (surface, border, text, accent, status), type/space/radius/motion scales and
  component sizes. Dark by default; `[data-theme="light"]` swaps the semantic layer.
- `web/src/styles.css`: rewritten on the tokens with every class name kept. It has no
  hardcoded values: `lint_hardcodes.py` went from 391 findings to 0.
- Status tones (decision 59, `lib/runs.ts colorGroup(status, nodeType?)`): pass (mint), run
  (cyan + edge sweep), attn (amber: needs-review, waiting gates), fail (coral), wait (steel),
  skip (dimmed). `components/Icon.tsx`: inline SVG icons plus `<Status>` / `<StatusIcon>`
  (icon and word, never color alone).
- Canvas nodes are instrument tiles: a kind icon with a label (no kind colors), the id, a
  status chip, and a task bar for `for_each` nodes. Layout rows are taller (`nodeHeight` 96).
  The canvas uses a line grid.
- Copy: the emoji and the dash tells are gone. Two a11y fixes: the pipeline budget input
  has a label, and the costs meter has a name.
- Mac: Studio window uses dark appearance and an obsidian under-page color (no white flash).

## Checks (measured)
- `lint_hardcodes.py web/src`: 0. `check_no_emoji.py web/src`: OK.
- axe-core (WCAG 2.2 A/AA) on 7 live routes in headless Chrome: 0 violations.
- Keyboard: 12 Tab stops per route all show a focus ring.
- 390 px wide: 0 px horizontal overflow on every route.
- Reduced motion: transitions and the running sweep become instant and single-pass.
- Token contrast via `contrast.py`: dark text 13.4:1, secondary 5.6:1, status colors 5.2 to
  10.7:1 on all surfaces; light variant adjusted until every pair passes AA.
- `tsc`, vitest 84/84, `npm run build`, `swift build`, `swift test` 59/59.
- Not run: the kit's file-based render gates (they need a static HTML file and Playwright's
  own browser, which isn't installed). `apple-design-skill` isn't available in this session.
