# Phase 14 (web): more room for the graph

Branch `phase-14-web` (off `phase-14`). Editor and run view chrome only: everything is in `web/`.
No new dependencies, no API changes.

## What changed

| Feature | Where |
|---|---|
| Round `+` on the canvas opens an **Add node** menu (the always-visible palette column is gone) | `components/Chrome.tsx` `AddNodeButton` |
| Pipelines list, Runs list, inspector and run details collapse to thin rails | `Chrome.tsx` `SidePanel`, `CollapseButton`, `PanelHeader` |
| Small translucent minimap that grows on hover / focus / tap, in the editor and the run view | `Chrome.tsx` `GrowingMiniMap` |
| The view keeps its center when a panel opens or closes, or the window resizes | `Chrome.tsx` `KeepCenterOnResize` |
| The pure state logic for all of the above, with vitest coverage | `lib/ui.ts`, `lib/ui.test.ts` |

### Add node

- The `+` button sits at the top-left of the canvas, clear of React Flow's controls (bottom-left)
  and the minimap (bottom-right). It has `aria-label="Add node"`, `aria-haspopup="menu"` and
  `aria-expanded`; the menu is `role="menu"` with `menuitem`s.
- Open: click, `ArrowDown` on the button, or `A` / `N` while the canvas (or nothing in particular)
  has focus and no text field does. Clicking the empty pane focuses the canvas, so the shortcut
  works right after clicking the graph.
- In the menu: arrows / Home / End move, Enter or Space picks, Escape closes and puts focus back
  on the button, Tab closes, and a click outside closes.
- Picking a type adds the node, selects it and pans to it (keeping the zoom). Layout is automatic
  (positions are never stored), so "drop at the viewport center" is really "center the view on
  where the layout put it". Items can still be dragged onto the canvas; the menu closes when the
  drag ends.
- Disabled while the YAML tab has unsaved edits, as the palette was.

### Panels

- Each panel has a chevron collapse button (`aria-label="Collapse Inspector"` etc.); collapsed, it
  leaves a 32px rail with an expand button (`aria-label="Expand Inspector"`) and a vertical label.
- State is per panel in `localStorage` (`agentline.panel.<name>`, values `"1"`/`"0"`), all access
  in try/catch; a broken or blocked storage just means nothing is remembered. Narrow viewports
  (< 768px) use separate keys (`agentline.panel.<name>.narrow`) and default to collapsed, so a panel opened on a
  desktop doesn't come back open on a phone.
- Selecting a node or edge while the inspector is collapsed does not open it: a pulsing dot
  appears on the rail until the panel is opened. Double-clicking a node opens the inspector.
  The run view does the same for its details panel.
- `\` (not while typing) or `Cmd/Ctrl + .` (anywhere) toggles the current view's two panels
  together: if either is open both collapse, else both open.
- React Flow tracks its pane size itself, so nothing breaks on resize, but by default the graph
  stays anchored to the top-left and slides sideways when a panel opens. `KeepCenterOnResize`
  shifts the viewport by half the size change so the point at the center stays at the center.

### Minimap

- Rendered at its full 200×150 and shown at `scale(0.6)` (120×90) with opacity 0.7. Scaling with
  a CSS transform (instead of changing the MiniMap's `style` size) keeps React Flow's pan math,
  which depends on the element size, right, and is what makes a 150ms transition possible.
  `prefers-reduced-motion` turns the transitions (and the rail pulse) off.
- Grows on mouse hover, keyboard focus (`:focus-visible`; it is `tabIndex=0`) or a pointerdown
  inside (a touch tap). A click or drag inside pans as before and never collapses it. It
  shrinks when the mouse leaves, on a pointerdown outside, on Escape, or on blur of a
  keyboard-focus expansion. Touch fires pointerenter/leave around every tap, so only mouse and
  pen count as hover.
- Dark mode now sets the minimap mask and node colors (React Flow's dark defaults only apply
  with its `.dark` class, which we don't use).

### Phone width

The breakpoint moved from 760px to 767px (< 768px). Columns stack as before; collapsed panels
become 36px horizontal bars (chevrons rotate to point up/down), so on a phone the canvas gets
almost the whole screen by default. The `+`, the controls and the minimap stay inside the canvas;
no horizontal page scroll at 375px.

## Fixes found on the way

- The run view passed fresh node objects on every render without `onNodesChange`, so React Flow
  never kept node measurements and the minimap (which only draws measured nodes) would have been
  empty. It now keeps `dimensions` changes and carries `measured` across the 2 s polls.
- `{...keyboardEvent}` copies none of `key`, `metaKey` and the rest (prototype getters); `keyLike()` reads
  them explicitly. Tested.

## Checks

- `npm test`: 67 tests (23 new in `lib/ui.test.ts`), `npx tsc --noEmit`, `npm run build`: green.
- Browser (dev server on a scratch copy of `examples/ticket-to-eod`, a submitted run for the run
  view): `+` menu by click and keyboard (arrows, Enter adds and pans to the node, Escape returns
  focus), `A` opens it, outside click closes; collapse buttons, rails, `\` and `Cmd+.` in both
  views, rail dot on select, double-click opens; minimap hover grows, drag inside pans and stays
  big, pointer out shrinks; 375px in light mode (panels collapsed by default, `scrollWidth` 375,
  minimap tap expands); desktop in dark and light. No console errors.

## Limits

- Touch was checked through the reducer tests and an emulated phone viewport whose clicks
  arrive as mouse events; not on a real touch device or inside the Mac app's WKWebView.
- The minimap is not keyboard-pannable (React Flow's isn't); focus only enlarges it.
- `Cmd+.` inside the Mac app may be claimed by a menu item before it reaches the page; `\` is the
  fallback.
- The built `index.html` can be served from browser cache after a rebuild; reload hard if the old
  UI shows up.
