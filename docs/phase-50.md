# Phase 50: install skills from skills.sh and GitHub

The user asked: "in the Skills settings, I should be able to install skill from market place.
If not possible, at least install skills like `npx skills add docker/skills`". Both work now,
natively (no Node).

## What `npx skills` does (researched from vercel-labs/skills)

- **Marketplace**: `npx skills find` calls `https://skills.sh/api/search?q=…&limit=…`
  (public, no key) → `{skills: [{id, source: "owner/repo", skillId, name, installs}]}`.
  There is no description in the results.
- **Sources**: `owner/repo`, `owner/repo/sub/path`, `owner/repo@skill`, GitHub URLs
  (`/tree/<ref>/<path>`), `github:owner/repo`, plus GitLab / other git hosts (not supported
  here).
- **Discovery** in a fetched repo: a `SKILL.md` at the pointed-to folder is the only skill;
  otherwise the root's children, `skills/` (+ `.curated`, `.experimental`, `.system`) and the
  agents' folders (`.claude/skills`, `.agents/skills`, …) up to 3 levels, and only if nothing
  is found, the whole tree 5 levels deep. A SKILL.md needs `name` and `description` in its
  front matter.

## Engine: `src/agentline/skill_sources.py`

- `parse_source(text)` reads every spelling above, and a pasted `npx skills add owner/repo
  --skill x` command. `Source.label` is the canonical spelling.
- `download()` gets `codeload.github.com/<owner>/<repo>/tar.gz/<ref>` (`HEAD` = default
  branch), 50 MB cap, cached 10 minutes so a preview's download is reused by the install.
  `unpack()` keeps regular files only (no links, nothing outside, 5 MB per file) and reads the
  commit id GitHub puts in the tarball's pax header.
- `discover()` mirrors the CLI's rules (above). Names are the front matter `name`, lowercased
  with spaces as dashes (or the folder name when that isn't a valid skill name).
- `install(source, names)` copies the chosen skill folders (all their files) into
  `<skills>/installed/<name>/` and records `{source, repo, ref, path, sha, installed_at}` in
  `<skills>/installed.json`. `update(names?)` downloads each repo once and reinstalls from the
  recorded path (`missing` when a skill left its repo); `remove(name)`.
- `search(q)` → skills.sh, in skills.sh's own ranking.
- `AGENTLINE_SKILLS_API` / `AGENTLINE_GITHUB_CODELOAD` point both at fakes in tests.

`SkillLibrary.list()` merges `installed/` with source `installed`. Priority on a name clash:
your own > installed > the fetched library > built-in. Everything that uses skills (nodes'
`skills:`, the editor's suggestions, the Chat's `.claude/skills` workspace and prompt
sections) goes through `list()`, so installed skills work everywhere.

## API (`api/skills.py`)

| | |
|---|---|
| `GET /api/skills/search?q=` | skills.sh results + `installed` (this skill from this repo is here) and `exists` |
| `POST /api/skills/preview {source}` | `{source, repo, url, ref, sha, skills: [{name, description, path, exists, installed_from}], skipped}` |
| `POST /api/skills/install {source, names?}` | installs; returns the skills view + `result` |
| `POST /api/skills/update {names?}` | re-downloads installed skills |
| `DELETE /api/skills/{name}` | your own skill, or now also an installed one |

`GET /api/skills` rows of installed skills carry `installed_from`. Network errors are 502
(404 → "is the repo public?"), bad sources 422.

## CLI

`agentline skills search WORDS | add SOURCE [--skill x]… | update [NAME] | remove NAME`.

## Web: Settings → Skills

- **+ Add skills** (primary) opens `components/settings/AddSkillsDialog.tsx`: one input.
  Words search skills.sh as you type (rows: name, repo, installs, **Install** = that one
  skill via `owner/repo@skill`, **All from repo** = preview the repo). A source
  (`lib/skills.ts looksLikeSource`: `owner/repo`, URL, `npx …`) is looked up on Enter /
  **Find skills** and shown as a checklist (description, clash note such as "your own skill
  of this name wins", skipped SKILL.md files) with **Install N skills**. A note says skills are
  instructions agents follow: install only from sources you trust.
- Installed rows show their repo as the badge (tooltip: source, commit, date) and
  **Update / Copy / Remove**; **Update installed** in the header when any are installed.

## Chat parity

`install_skills {source, names?}` (checked with `parse_source`; names must be skill names),
applied through `api.installSkills`; own click (not "Apply all") since it downloads
third-party instructions. `delete_skill` now covers installed skills; LIVE STATE lists "Your
own and installed skills". Row added to docs/phase-25.md's table.

## Tests

`tests/api/test_skill_sources.py`: a fake codeload + skills.sh server (`_Server`), source
parsing, discovery layouts (skills/, root SKILL.md, deep tree, `.claude/skills`, skipped
SKILL.md, symlink dropped), install/update/remove + lock, shadowing by your own skill, API,
CLI. Parity cases in `tests/ai/test_assistant_parity.py`; web: `lib/skills.test.ts`,
`SettingsExtra.test.ts`, `assistantApply.test.ts`.

Checked by hand in the browser against the real skills.sh and `docker/skills` (12 skills
found, 2 installed, Update, rows) on a throwaway data folder.

## Not done

- GitLab / other git hosts and private repos (codeload only serves public GitHub repos
  without a token).
- skills.sh results have no descriptions; the dialog shows name, repo and installs.
