# Phase 35: Agents → Harnesses, `llm` → `builtin`

Why: the thing a node (or the Chat) runs on is a *harness*: the loop, tools and context that
drive a model (Claude Code, Codex, Aider). VS Code uses the same word ("Copilot / Local /
Claude / Codex" are harnesses; "agent" is the role). `llm` was renamed `builtin`: agentline's own
harness, which needs only an API key. Only the harness meaning was renamed. Node
`type: agent`, "agent node", agent skills and runner names (`runner: llm`, router `mode: llm`)
stay as they were.

## Naming map (the old name is still read everywhere)
| Where | Old | New |
|---|---|---|
| agentline.toml | `[agents.<name>]` | `[harnesses.<name>]` |
| harness kind / built-in profile | `llm` | `builtin` |
| agent nodes, `defaults:` | `agent:` | `harness:` |
| Settings API | `/settings/agents/x`, `/agents/x/detect`, `agents` in GET | `/settings/harnesses/x`, `/harnesses/x/detect`, `harnesses` |
| node types API | `agents`, field `agent` (source `agents`) | `harnesses`, field `harness` (source `harnesses`) |
| setup choice / Chat API | `{agent, model}` | `{harness, model}` |
| Chat actions | `setting` section `agents`, `chat_uses.agent` | section `harnesses`, `chat_uses.harness` |
| execution notes, triage records | `agent` | `harness` |
| bundles | `requires.agents`, `settings.agents` | `harnesses` |
| UI | Settings → Agents, "Agent" pickers | Settings → Harnesses, "Harness" |

## How the old names are handled
- `config.py`: `[agents]` and `[harnesses]` are merged; one name in both is an error.
  `kind = "llm"` → `builtin` (`HARNESS_KIND_ALIASES`).
- `agents.py`: `harness_name()` maps the old name `llm` to `builtin` unless a profile is
  really called `llm`. `kind_name()` for kinds.
- `pipeline/spec.py`: `agent:` is read as `harness:` (`OLD_FIELDS`, never offered by the
  editor); both on one node / in `defaults` is an issue.
- `pipeline/roundtrip.py`: `to_model` shows `harness`; `apply_model` keeps the file's `agent:`
  while its value is unchanged (byte-identical saves) and renames the key in place, comment
  kept, once the value changes (`_respell`).
- `tomlwrite.migrate_section`: a write to `[harnesses.*]` first renames `[agents.X]` headers in
  place (comments kept, checked by parsing back).
- `setup.read_state` converts a saved `{"agent"}` choice; the web reads the old localStorage
  key; `assistant._renamed` converts old Chat actions; `bundle` reads old manifests.

Tests: `tests/ai/test_harness_names.py` (every alias above), plus the existing suites on the
new names. Templates and examples use the new keys; `docs/agents-and-models.md` moved to
`docs/harnesses-and-models.md` (the old file points there).
