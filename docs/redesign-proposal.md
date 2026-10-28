# Agentline Studio redesign: audit and proposal

Status: **approved 2026-10-20: Flight Deck.** The status-color rule (decision 49) was released by the user; the system sets status colors (decision 59). Final mock: https://claude.ai/artifact/A3DsUeYHQWNPQe4ni73Rbz. Implementation: phase 21.

## 1. Audit

### UI architecture
- One UI: React 18 + TypeScript + React Flow (Vite), plain CSS in `web/src/styles.css`
  (1849 lines, one file). The Mac app hosts it in a WKWebView (`StudioWindow.swift`), so a
  redesign of `web/` restyles the Mac app too. No component library, no CSS framework.
- Screens: Pipelines (list + canvas + inspector + Chat), Runs (list + run graph + details),
  Approvals, Problems, Costs, Logs, Settings (8 tabs), plus dialogs (setup, start run,
  import/export, dry run, history, diff).
- Theme: about 30 CSS variables on `:root` with a dark `prefers-color-scheme` override. Node
  kinds and run statuses already have their own variables, which is a good base.

### Measured (kit gates, current code)
- `lint_hardcodes.py src`: **FAIL, 391 hardcoded values** (px sizes, radii, colors outside the
  variables).
- `check_no_emoji.py src`: 3 dash tells in UI copy (`SettingsExtra.tsx:765, 771`,
  `Editor.tsx:616`).
- Type: 7 ad-hoc sizes (11, 12, 13, 14, 15, 18, 22, 28 px). 25 of 35 declarations are 11 or 12 px.
- Radius: 10 different values (0, 2, 3, 4, 6, 8, 9, 10 px, 50%).
- 5 `:focus-visible` rules for the whole app. Reduced motion is handled (1 block).
- Icons: almost none (2 inline SVGs). Arrows and glyphs are text characters.

### Findings, most severe first
| # | Severity | Finding | Where |
|---|---|---|---|
| 1 | Critical | No real hierarchy. The top nav, the page toolbars and the panel headers all have the same weight, so nothing says where you are or what to do next | every screen |
| 2 | Critical | Focus states are missing on most controls (5 rules for about 40 control styles). Keyboard use is hard to follow | buttons, tabs, list rows |
| 3 | Major | 391 hardcoded values and 10 radii: each page drifted on its own, so the screens don't read as one product | `styles.css` |
| 4 | Major | Text is too small for long sessions: most labels are 11 or 12 px with grey on grey | inspector, run details, lists |
| 5 | Major | The canvas is a flat black field, and the nodes are small low-contrast boxes. At fit-to-view, the 13-node feature-factory graph can't be read | Pipelines, Runs |
| 6 | Major | Status relies on color: the left border plus a colored word. One-sided accent bars are also a banned tell | run nodes, chat cards |
| 7 | Major | Empty areas waste the first screen: "Pick a run." sits in 80% of the window, and Costs has a 1-bar chart across the full width | Runs, Problems, Costs |
| 8 | Minor | The Chat panel mixes raw run journals and proposal cards with no separation, so cards and messages look the same | Chat |
| 9 | Minor | Buttons have four looks (filled blue, outline, text link, small pill) with no clear rule | toolbars |
| 10 | Minor | No icons: "Import", "<", ">" and "+" are text glyphs | headers, panels |

What works and must stay: every route, the keyboard shortcuts, the panel collapse, the
status color meanings (decision 49: green done, red failed, yellow running, grey pending,
greyed skipped), the reduced-motion block, `role=group` inspector fields, and the native
bridge.

## 2. Brief
- Domain: a developer control plane for long-running agent pipelines. Expert users with
  long sessions.
- Mood: **precise, calm, instrumented.** Like a flight deck, not a game HUD.
- Motion: subtle feedback, plus one meaningful live signal (running nodes).
- Layout family: a docked console, with rails, a main canvas and instrument panels. This is
  not a card dashboard.

## 3. Three directions

### A. Flight Deck (recommended)
Obsidian base, midnight-navy instrument panels, hairline rules and small corner ticks on
panels. Cyan is the one action color, ice-blue marks selection and focus, and amber is only
for warnings and gates. SF Pro for UI, SF Mono for ids, numbers and uppercase micro-labels
with wide tracking. The canvas gets a faint measured grid with coordinates at the edges.
Running nodes get a slow cyan sweep along their outline (stopped under reduced motion).
- Pros: readable for hours (solid surfaces, high text contrast), the technical details stay
  at the edges and out of the content, and it is cheap to render in WKWebView. It also
  maps cleanly onto the current layout.
- Cons: the most restrained of the three. Its identity comes from detail and typography, so
  sloppy spacing anywhere would show.

### B. Orbital Glass
Layered translucent panels over a deep navy gradient, with backdrop blur, soft cyan glow on
focus and depth from stacking.
- Pros: the most "premium" at first glance.
- Cons: blur behind a live React Flow canvas is expensive in WKWebView. Contrast changes with
  whatever sits behind a panel. Glow tires the eyes over long sessions. This direction fights
  the "comfortable for extended use" goal.

### C. Telemetry Terminal
Near-black, mono-first, dense grid lines, phosphor cyan, bracketed labels like `[RUN 01A1]`
and tabular everything.
- Pros: the most distinctive, and very dense.
- Cons: mono-heavy prose (Chat, prompts, issue notes) is tiring to read. It drifts toward a
  costume, and forms and settings look harsh.

**Recommendation: A, Flight Deck.** It is the only one that meets "readable and comfortable
for extended use" without weakening the sci-fi identity. B's glass and C's mono identity
can each appear once, where they help: a glass sheet for modal dialogs, and mono for ids,
numbers and logs.

## 4. Token system
Contrast checked with the kit's `contrast.py` (on `surface.panel` #141D2B unless noted).

**Primitives**
| Token | Value |
|---|---|
| `obsidian-950` | #0A0E14 |
| `navy-900` | #0F1622 |
| `navy-850` | #141D2B |
| `navy-800` | #1A2536 |
| `navy-700` | #1F2A3A |
| `navy-600` | #2B3A50 |
| `ice-100` | #DCE6F0 |
| `steel-400` | #8796A8 |
| `steel-600` | #5E6B7D |
| `cyan-400` | #4FD1E8 |
| `ice-300` | #A8E6F5 |
| `amber-400` | #F2B33D |
| `red-400` | #F2687A |
| `green-400` | #5BD6A0 |

**Semantic** (what the components use; nothing else may reach a primitive)
| Token | Maps to | Use | Contrast |
|---|---|---|---|
| `surface.page` | obsidian-950 | window, canvas | |
| `surface.rail` | navy-900 | nav rail, lists | |
| `surface.panel` | navy-850 | inspector, cards | |
| `surface.raised` | navy-800 | hover, menus, dialogs | |
| `border.hairline` | navy-700 | rules, panel edges | |
| `border.strong` | navy-600 | inputs, dividers that matter | |
| `text.primary` | ice-100 | content | 13.40:1 |
| `text.secondary` | steel-400 | labels, meta | 5.61:1 |
| `text.disabled` | steel-600 | disabled and decorative only | 3.12:1 (not body text) |
| `accent.action` | cyan-400 | primary buttons, links, active nav | 9.37:1 |
| `accent.on-action` | #06222A | text on cyan buttons | 9.15:1 |
| `accent.focus` | ice-300 | focus ring (2 px + 2 px offset), selection | |
| `status.ok` | green-400 | done | 9.33:1 |
| `status.running` | cyan-400 with the sweep | running (the cyan replaces yellow, see below) | |
| `status.warn` | amber-400 | warnings, waiting gates, budget | 9.10:1 |
| `status.fail` | red-400 | failed, dead, needs-review | 5.67:1 |
| `status.idle` | steel-400 | pending, ready, waiting | |

**Scales**
- Type: 11 (mono micro-label, uppercase, +0.08em tracking only), 12, 13 (base UI), 15, 18,
  24, 32. Body text uses 13 px, up from 12. Tabular numbers everywhere.
- Space: 2, 4, 8, 12, 16, 24, 32, 48.
- Radius: 2 (chips), 6 (controls, nodes), 10 (panels, dialogs). Three values, down from ten.
- Elevation: flat by default. Panels separate by surface step plus hairline. Only menus
  and dialogs get one shadow, and they get the glass sheet.
- Motion: 120 ms (hover, press), 200 ms (panels), 240 ms ease-out (dialogs). The running
  sweep loops at 2.4 s and is the only infinite animation, with an opt-out comment. It goes
  static under `prefers-reduced-motion`.

**One status decision for you.** Decision 49 says running is **yellow**. In this palette,
amber means "warning", so yellow for running would collide with warnings. My proposal: running
becomes cyan with a moving sweep, and amber stays for warnings and waiting gates. Every
status also gets an icon and a text label, so color is never the only signal. If you want
yellow for running kept, it stays and the warnings get a different treatment.

## 5. Component strategy
- Tokens live in one file, `web/src/theme.css` (CSS custom properties: primitives, then
  semantic). `styles.css` uses semantic variables only, and `lint_hardcodes.py` gates it. No
  new framework, and no Tailwind.
- Themes: the request is for a dark console, so dark is the default. A light
  "daylight" variant comes from the same semantic tokens (`[data-theme=light]`), so a theme
  switch never needs page edits.
- Icons: lucide as inline SVG (`currentColor`), added as a small local `Icon.tsx` with only the
  icons used. It's a devDependency, or vendored paths. No icon font.
- Primitives to standardize (8 states each: default, hover, focus-visible, active, disabled,
  loading, error, selected): Button (primary / secondary / ghost / danger), IconButton, Tab,
  ListRow, Field, Select, Badge/StatusChip (icon + label), Panel (header with mono
  micro-label and corner ticks), Dialog (glass sheet), Toast/Banner (full hairline border,
  icon, text; no one-sided bars).
- React Flow: nodes become instrument tiles with a kind glyph, mono id, title, and a status
  chip at the bottom. Edges use the hairline color with a cyan flow on the active path in a run.
- macOS: native window chrome stays (traffic lights, title bar). The web UI respects the
  safe area, and shortcuts stay the same. The `apple-design-skill` review comes after the
  first screen.

## 6. Plan after approval
1. Tokens + primitives, and the **Runs** screen first. It has the most kinds of state (list,
   graph, statuses, details, banner), so it proves the system.
2. Then Pipelines (canvas, inspector, Chat), Settings, Approvals, Problems, Costs, Logs and
   the dialogs, one shared theme.
3. Gates: `lint_hardcodes`, `check_no_emoji`, `validate_contrast`, axe, keyboard walk-through,
   reduced motion, the 84 vitest tests, `tsc` and the build, then the macOS review.
