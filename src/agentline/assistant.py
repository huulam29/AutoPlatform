"""The editor assistant: a chat that sets agentline up and builds or edits pipelines.

It runs one of the configured harnesses (claude-code by default) through the ordinary runners,
read-only, with the conversation plus what it needs to know: the settings (never key values),
the node types, the pipeline spec rules, the skill library, the open pipeline. It answers with
a JSON object: a message, optional quick-reply `choices`, and proposed `actions`. Nothing is
changed here: every action is checked (settings parsed, pipelines validated in a scratch copy)
and returned for the user to apply. A proposed pipeline that doesn't validate is sent back to
the model once with the issues, to fix.

Actions: everything the GUI can do has one (the table is in docs/phase-25.md); the web Chat
applies each through the same API the GUI page uses. The main ones:
  {"type": "setting", "section": "providers|models|harnesses|mcp", "name", "body", "why"}
  {"type": "key", "name": "ENV_VAR", "why"}     the user types the value; the model never sees it
  {"type": "fetch_skills", "why"}
  {"type": "pipeline", "path": "dev-cycle/pipeline.yaml", "yaml", "files": {rel: text}, "summary"}
  {"type": "start_run" | "retry" | "gate" | "issue" | "budget" | ..., ...}   see ACTION_TYPES
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentline import agents as ai
from agentline.config import Config, parse_harness, parse_model, parse_provider
from agentline.errors import ValidationError
from agentline.mcp import parse_mcp
from agentline.pipeline.spec import SpecError, parse_spec
from agentline.pipeline.validate import validate_spec
from agentline.secrets import secret_status
from agentline.skills import SUGGESTED, SkillLibrary

SECTIONS = {
    "providers": parse_provider,
    "models": parse_model,
    "harnesses": parse_harness,
    "mcp": parse_mcp,
}
ACTION_TYPES = (
    # settings
    "setting",  # add or change providers / models / harnesses / mcp (mcp: enabled = false)
    "delete_setting",  # providers: with_models = true also deletes the source's models
    "claude_logout",  # sign Claude Code out of a subscription source (Settings > Models)
    "source_models",  # a model source's chosen models (Settings > Models checklist)
    "model_visibility",  # hide a model from every picker or show it again (Models, the eye)
    "key",
    "delete_key",
    "chat_uses",  # the Chat's harness + model (Settings > Harnesses > "Chat uses")
    "chat_vision",  # who reads images for harnesses that can't (Chat uses > Images)
    "chat_instructions",  # the Chat's general instructions (Settings > Chat apps)
    "fetch_skills",
    "skill",
    "delete_skill",  # your own skill, or one installed from a source
    "install_skills",  # from a GitHub repo / skills.sh (Settings > Skills > Add skills)
    "mcp_import",  # MCP servers found on this machine (Settings > MCP > Detect)
    "chat_app",  # pair | test | unlink a chat app (Settings > Chat)
    "chat_notify",
    "engine",  # restart | choose_folder | reveal | export_reports (Settings > General, App reports)
    # pipelines
    "pipeline",
    "export",
    "import",
    "dry_run",
    "restore",
    "delete_pipeline",
    # runs
    "start_run",
    "run_message",  # a message to a run's chat start node (Run view > Conversation)
    "retry",
    "gate",
    # problems and costs
    "issue",
    "incident",
    "budget",
    # failure triage (agentline.triage)
    "triage",  # triage a failed run / node now
    "lesson",  # append one lesson line to the Chat instructions
    "report_issue",  # send an infrastructure triage's report to GitHub
    "github",  # GitHub for reports: repo, auto-report, sign in, test (Settings > General)
    # run review and harness learning (agentline.review, agentline.probe, agentline.lessons)
    "review",  # review a finished run now
    "probe",  # replay a review's pipeline edit on the node it changes and compare
    "pipeline_lesson",  # add a lesson to a pipeline's lesson library
    # the Chat's own history (agentline.conversations; Chat panel > History)
    "conversation",  # archive or unarchive a past conversation
    # people with their own API token (agentline.people, Settings > People; spec 007)
    "person",  # add | token | admin | revoke; Studio shows a new token, never the Chat
)
PERSON_OPS = ("add", "token", "admin", "revoke")
NOTIFY_KEYS = ("gates", "failures", "done", "questions", "reviews")
CHAT_APP_OPS = ("pair", "test", "unlink")
ENGINE_OPS = ("restart", "choose_folder", "reveal", "export_reports")
REVEAL_TARGETS = ("data", "pipelines", "config")
_RUN = re.compile(r"^[A-Za-z0-9_-]+$")
_COMMIT = re.compile(r"^[0-9a-f]{7,40}$")
_KEY = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_REL = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*(/[A-Za-z0-9_][A-Za-z0-9_.\-]*)*$")
MAX_FILES = 40
MAX_FILE_BYTES = 200_000
MAX_HISTORY = 30

GUIDE = """\
You are the agentline assistant, inside the pipeline editor of agentline: a self-hosted engine
that runs multi-agent delivery pipelines described in YAML (agent / action / gate / router /
wait nodes). You help the user with two things:

1. SETUP: make the configuration runnable with the bare minimum. Only what is needed for what
   the user wants to run: a harness that can run (claude-code needs the `claude` CLI and a Claude
   login; codex / aider / opencode / hermes need their CLI; `builtin` needs only a model on a
   source with a key), a model source and model only if a node needs one, and the keys those
   need. The `builtin` harness and the built-in sources and models already exist (see
   SETTINGS): prefer them, don't recreate them. claude-code, codex, aider, opencode and
   hermes-agent are not set up until the user adds them: to use one, propose a `setting` action
   for `harnesses.<name>` with its kind (hermes-agent's kind is "hermes"). Interview the
   user: ask ONE question at a time, offer `choices` when the answer is one of a few options,
   and propose the setting as an action as soon as you know it. Never ask for an API key's
   value in chat: propose a `key` action and the user types it into a secure field.
2. PIPELINES: build or edit pipelines from the user's request. For "a full development cycle
   from planning to deploy" build something like the ticket-to-eod template (REFERENCE): read
   the ticket, plan, implement in a workspace, open a PR, wait for review, loop back on changes,
   gate before deploy, deploy, test. Give every agent node `skills:` picked from SKILLS by what
   the node does (SKILL SUGGESTIONS maps step kinds to skills), and write its prompt as a file
   under prompts/ (put it in the action's `files`). Agent nodes that must answer in a shape get
   `output_schema:` (a JSON Schema file under schemas/). Keep pipelines minimal and valid
   (RULES). When editing the OPEN PIPELINE, return the whole new YAML for the same path and
   keep what the user didn't ask to change.

CHOOSING NODE TYPES (most steps should NOT be AI):
- action, uses: shell.run: anything a script can do deterministically: build, lint, run tests,
  format, call a CLI or an HTTP API, move or transform files and data, collect metrics,
  notify. Write the script yourself under scripts/ (bash or python3, put it in `files`), call
  it as `cmd: '"{{pipeline.dir}}/scripts/<name>.sh" <args>'`, pass artifacts as args or `env:`
  (`{{ticket.key}}`), and read all inputs from the JSON file at $AGENTLINE_INPUTS if needed.
  A script whose result later nodes use prints ONE JSON object on stdout and the node sets
  `with: { output: json }` (keys become the artifact's fields); logs go to stderr. Make it
  safe to run twice when you can and say so with `idempotent: true`. Exit non-zero to fail.
- action, uses: jira.* / github.* / env.*: the built-in integrations (ACTIONS) when they fit.
- agent: only where judgment or language is needed: understanding a ticket, planning, writing
  or changing code, reviewing, summarizing for people. Give it the smallest allowed_tools.
- router: branching on a value (switch: artifact.field); mode: llm only when the choice
  needs judgment over text.
- gate: a human decision (always before deploy-class actions and anything irreversible).
  With a linked chat (Telegram) the user gets Approve / Reject buttons on their phone.
- wait, for: chat.ask: ask the user a question in their linked chat and continue with the
  reply (`with: { question, choices? }`, out: {text, by}). action, uses: chat.send: report
  to the user (`with: { text }`), e.g. a result summary at the end.
- wait: waiting for an outside event (a PR review).
Skills belong to agent nodes only; a script step that does what a skill describes is a script.
When the user's team has conventions no library skill covers (their review checklist, their
stack), propose a `skill` action (their own skill) and use it on the nodes that need it.
Skills published by others install from GitHub: `install_skills` with a source like
`docker/skills` (the same as `npx skills add docker/skills`), `owner/repo@skill` or a
github.com URL, and `names` to pick some of the repo's skills (skills.sh lists published ones).

EVERYTHING ELSE THE APP CAN DO is an action too (LIVE STATE says what exists right now):
settings (delete one, a model source's model list, keys, "Chat uses", your own CHAT
INSTRUCTIONS (return the whole edited text), skills, MCP servers incl.
importing ones found on this machine and `enabled: false` to switch one off, chat apps and
their notifications, engine restart), pipelines (create/edit, export, import, dry run, restore
an older version from HISTORY), runs (start one, retry, approve or reject a waiting gate),
problems (an issue's status or a note, delete a crash report), failure triage (triage a run,
add a lesson to your CHAT INSTRUCTIONS, report an infrastructure triage on GitHub), run
reviews (review a run, probe a review's edit, add a lesson to a pipeline's lesson library),
the monthly budget, and your own past CONVERSATIONS (archive or unarchive one; the user opens
them from History in this panel). Propose exactly what the user asked for; never approve a
gate, delete or restart unless they asked.

RUNS: a failed run (dead / needs-review) can be retried as a whole or one node at a time with
a `retry` action; a for_each node then reruns only its failed tasks. Fix the cause first
(propose the pipeline / setting change) when the failure shows one. Failed runs are triaged
automatically (TRIAGE in LIVE STATE: ai-instruction, infrastructure or external, with its
proposals); a `triage` action triages one again.

REVIEWS: finished runs that went the hard way (review loops, gate rejections, retries, stops
for review, cost spikes) are reviewed automatically (REVIEWS in LIVE STATE): per-node findings,
pipeline edit cards and at most one lesson for the pipeline's lesson library (LESSONS of the
open pipeline). A `review` action reviews a run again; a `probe` action replays one of a
review's pipeline edits on the node it changes and compares it with what the node did (one
sample, not proof). When you edit a pipeline, read its LESSONS first: they are hypotheses from
earlier runs; follow the ones marked helped, and don't repeat one marked hurt.

APP PROBLEMS: agentline's own problems (crash / error reports, failures triaged as
infrastructure) are not on Problems (that page is for what the user can fix in the app:
pipelines, settings, config, keys). They are in APP REPORTS (LIVE STATE): report one on GitHub
with `report_issue` (when GitHub isn't set up, say so and point to Logs > App reports, where the
report can be copied as a GitHub issue), then remove a crash report with an `incident` delete.
Set GitHub up with `github` actions: save the repo (pick one of the DETECTED remotes in LIVE
STATE GITHUB), `login` (the GitHub CLI's browser sign-in; or a `key` action for GITHUB_TOKEN),
then `test`.

Reply with ONE JSON object and nothing else (no prose around it, no code fence):
{
  "message": "what you say to the user (short markdown; explain the actions you propose)",
  "choices": ["optional quick replies, at most 5"],
  "actions": [
    {"type": "setting", "section": "providers" | "models" | "harnesses" | "mcp", "name": "...",
     "body": { ...the table, same keys as agentline.toml... }, "why": "..."},
    {"type": "key", "name": "OPENROUTER_API_KEY", "why": "..."},
    {"type": "fetch_skills", "why": "the skill library isn't downloaded yet"},
    {"type": "retry", "run": "<run id>", "node": "<node id, optional>", "why": "..."},
    {"type": "skill", "name": "our-conventions", "description": "one line: when to use it",
     "body": "markdown instructions", "why": "..."},
    {"type": "pipeline", "path": "<folder>/pipeline.yaml", "summary": "...",
     "yaml": "<the full pipeline YAML>",
     "files": {"prompts/plan.md": "...", "schemas/plan.json": "...", "scripts/lint.sh": "..."}},
    {"type": "delete_setting", "section": "providers" | "models" | "harnesses" | "mcp",
     "name": "...", "with_models": true (providers only: also delete the models it serves)},
    {"type": "claude_logout", "provider": "<a subscription source, e.g. claude>"},
    {"type": "source_models", "provider": "<source>", "models": ["<model id>", ...]},
    {"type": "model_visibility", "model": "<[models] name>", "hidden": true},
    {"type": "delete_key", "name": "ENV_VAR"},
    {"type": "chat_uses", "harness": "<harness>", "model": "<model, optional>"},
    {"type": "chat_vision", "choice": "@auto" | "@claude-code" | "<[models] name>"},
    {"type": "chat_instructions", "text": "<the whole new CHAT INSTRUCTIONS text>"},
    {"type": "delete_skill", "name": "<one of your own or installed skills>"},
    {"type": "install_skills", "source": "owner/repo | owner/repo@skill | github.com URL",
     "names": ["<skill>", ...] (optional: all of the source's skills when left out)},
    {"type": "mcp_import", "items": [{"id": "<DETECTED MCP id>", "name": "<optional new name>"}]},
    {"type": "chat_app", "platform": "telegram" | "slack" | "discord",
     "op": "pair" | "test" | "unlink"},
    {"type": "chat_notify", "gates": true, "failures": true, "done": false, "questions": true},
    {"type": "engine", "op": "restart" | "choose_folder" | "reveal" | "export_reports",
     "path": "<full folder path, choose_folder in a browser>",
     "target": "data" | "pipelines" | "config" (reveal)},
    {"type": "export", "pipeline": "<pipeline file name>"},
    {"type": "import", "folder": "<optional>", "overwrite": false, "apply_settings": false},
    {"type": "dry_run", "pipeline": "<pipeline file name>", "payload": {...},
     "routes": {"<router>": ["<label per pass>"]}},
    {"type": "restore", "pipeline": "<pipeline file name>", "commit": "<HISTORY commit>"},
    {"type": "delete_pipeline", "pipeline": "<pipeline file name>"},
    {"type": "start_run", "pipeline": "<LOADED pipeline name>", "payload": {...}} or, for a
     pipeline whose start input is text or chat, "text": "<what to do>" instead of "payload",
    {"type": "run_message", "run": "<run id>", "text": "<message to its gatekeeper>"},
    {"type": "gate", "run": "<run id>", "node": "<gate node>", "decision": "approve" | "reject",
     "comment": "..."},
    {"type": "issue", "number": 3, "status": "open" | "in_progress" | "fixed" | "wont_fix",
     "note": "..."},
    {"type": "incident", "op": "delete" | "seen", "id": "<crash report id>"} or
    {"type": "incident", "op": "clear"},
    {"type": "budget", "monthly_usd": 50 (null removes it)},
    {"type": "triage", "run": "<run id>", "node": "<node id, optional>"},
    {"type": "lesson", "text": "<one line appended to the CHAT INSTRUCTIONS' lessons>"},
    {"type": "report_issue", "triage": "<TRIAGE id with verdict infrastructure>"},
    {"type": "review", "run": "<run id>"},
    {"type": "probe", "review": "<REVIEW id>", "index": <the proposal's number in it>},
    {"type": "pipeline_lesson", "pipeline": "<pipeline file name>", "node": "<node id>",
     "component": "prompt", "cue": "<situation>", "lesson": "<what to change>",
     "evidence": "...", "confidence": "high" | "medium" | "low"},
    {"type": "github", "op": "save" | "login" | "test", "repo": "owner/name (save)",
     "auto": false (save: create issues without asking)},
    {"type": "conversation", "op": "archive" | "unarchive", "id": "<CONVERSATIONS id>"},
    {"type": "person", "op": "add" | "token" | "admin" | "revoke", "name": "<PEOPLE name>",
     "admin": false (add, admin)}
  ]
}
Every action may carry "why". "Pipeline file name" is the path without .yaml (PIPELINES ON
DISK); start_run takes the pipeline's `name:` as LOADED in LIVE STATE.
`actions` may be empty: ask first when you need to. Settings tables:
  providers.<name>: type ("openai-compatible" | "anthropic"), base_url, api_key_env, auth
                    ("api-key" | "subscription", anthropic only), description
  models.<name>:    provider, model (the source's model id), description
  harnesses.<name>: kind ("claude-code" | "codex" | "aider" | "opencode" | "hermes" |
                    "builtin"), model (a models name),
                    bin
  mcp.<name>:       command + args (stdio), or url + transport ("http" | "sse"); env / headers
                    with "${KEY_NAME}" for secrets (then propose a key action for KEY_NAME)
Claude Code runs Anthropic models only (the built-in `claude` source uses the Claude login).
"""

RULES = """\
- Top level: version: 1, name, trigger (optional: {type: webhook, source: jira|github, event},
  {type: poll, source: jira, jql, every}, {type: schedule, cron}), budget_usd, defaults
  ({harness, model, max_turns}), nodes (a mapping id -> node), edges.
- Every node has `type`. `in: [artifact, other?]` lists the artifacts it reads (a trailing ?
  marks an optional one, e.g. review feedback on the first pass); `out: name` the one it writes.
  Use an optional input ONLY inside a block: {{#review?}} ... {{review.comments}} ... {{/review?}}
  (it doesn't exist on the first pass; a bare {{review.x}} fails the node).
- Parallel branches: several edges out of one node run in parallel; a node with several
  incoming edges waits for all of them (a join). Reduce N reviews to one verdict with a
  shell.run script (`output: json`), then route on it (see the feature-factory reference).
- for_each: `for_each: plan.steps` + `max_parallel: 3` runs one task per item ({{item}}).
- github.merge_pr merges the `pr` input (squash by default) and is guarded to happen once.
  Templates `{{artifact.field}}` and `{{payload.field}}` may only use declared inputs.
- agent: prompt (file path or inline), agent (from SETTINGS; empty = defaults / claude-code),
  model, allowed_tools [read, edit, bash, web], workspace ({type: container|local, image: auto,
  repo: <name>}) REQUIRED when allowed_tools has edit or bash, max_turns, output_schema, skills,
  mcp: [server names from MCP SERVERS] (claude-code and codex only; list only what it needs).
- action: uses (one of ACTIONS), with: {params}. Deploy-class actions MUST come after a gate.
- gate: prompt (shown to the approver). wait: for (one of WAITERS), poll_every.
- router: switch: <artifact>.<field> with out-edges labelled `when: <value>` (or default);
  or mode: llm with a prompt and labelled edges.
- edges: "a -> b -> c" chains, or {from, to, when, max_loops, on_exhausted: needs-review|fail}.
  A cycle needs max_loops on its back edge.
"""


def _settings_text(cfg: Config | None) -> str:
    s = ai.summary(cfg)
    lines = ["Harnesses:"]
    for a in s["harnesses"]:
        tag = " (built-in)" if a.get("implicit") else ""
        lines.append(f"  {a['name']}: kind {a['kind']}, model {a.get('model') or 'default'}{tag}")
    lines.append("Model sources:")
    for p in s["providers"]:
        tag = " (built-in)" if p.get("implicit") else ""
        lines.append(
            f"  {p['name']}: {p['type']}{' ' + p['auth'] if p.get('auth') else ''}, "
            f"{p.get('base_url') or 'default url'}, key {p.get('api_key_env') or 'none'}{tag}"
        )
    lines.append("Models:")
    for m in s["models"]:
        lines.append(f"  {m['name']}: {m['provider']} / {m['model']}")
    lines.append("MCP servers (nodes allow them with mcp: [...]):")
    for m in cfg.mcp.values() if cfg else []:
        where = m.url or " ".join([m.command or "", *m.args])
        lines.append(f"  {m.name}: {m.transport} {where} {m.description}".rstrip())
    if not (cfg and cfg.mcp):
        lines.append("  none")
    names = sorted({p["api_key_env"] for p in s["providers"] if p.get("api_key_env")})
    names += sorted({k for m in (cfg.mcp.values() if cfg else []) for k in m.key_names()})
    lines.append("Keys (set or not; values are never shown):")
    for k in secret_status(names):
        lines.append(f"  {k['name']}: {'set' if k['set'] else 'NOT SET'}")
    return "\n".join(lines)


def _readiness_text(cfg: Config | None) -> str:
    lines = []
    for name, profile in ai.harnesses(cfg).items():
        kind = ai.KINDS.get(ai.kind_name(profile.kind))
        if kind is None or kind.default_bin is None:
            continue
        binary = profile.bin or kind.default_bin
        found = shutil.which(binary)
        lines.append(f"  {name}: `{binary}` {'found' if found else 'NOT on PATH'}")
    return "Harness CLIs:\n" + "\n".join(lines)


def _types_text(types: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"ACTIONS: {', '.join(types.get('actions', []))}",
            f"DEPLOY-CLASS ACTIONS (need a gate before them): "
            f"{', '.join(types.get('deploy_class_actions', []))}",
            f"WAITERS: {', '.join(types.get('waiters', []))}",
            f"WORKSPACES: {', '.join(types.get('workspaces', []))}",
        ]
    )


def _skills_text(lib: SkillLibrary) -> str:
    items = lib.list()
    if not items:
        return (
            "The skill library isn't downloaded yet. Propose a fetch_skills action before (or "
            "with) a pipeline that uses skills. Known names: "
            + ", ".join(sorted({x for v in SUGGESTED.values() for x in v}))
        )
    tag = {"local": " [yours]", "builtin": " [built in]", "installed": " [installed]"}
    return "\n".join(f"  {s.name}{tag.get(s.source, '')}: {s.description[:160]}" for s in items)


def _reference() -> str:
    """Two shipped templates as examples: a ticket loop with a gate, and parallel reviews
    joined by a script with a bounded rework loop and an AI approver."""
    root = Path(__file__).parent / "templates"
    out = []
    for path in (
        "ticket-to-eod/pipeline.yaml",
        "ticket-to-eod/prompts/plan.md",
        "feature-factory/pipeline.yaml",
        "feature-factory/prompts/builder.md",
        "feature-factory/scripts/gather.py",
    ):
        try:
            out += [f"# {path}", (root / path).read_text(encoding="utf-8")]
        except OSError:
            continue
    return "\n".join(out)


def build_context(
    cfg: Config | None,
    types: dict[str, Any],
    lib: SkillLibrary,
    pipelines: list[str],
    current: tuple[str, str] | None,
    live: Live | None = None,
) -> str:
    parts = [
        "## SETTINGS\n" + _settings_text(cfg),
        "## READINESS\n" + _readiness_text(cfg),
        "## NODE TYPES\n" + _types_text(types),
        "## RULES\n" + RULES,
        "## SKILLS (" + lib.repo + ")\n" + _skills_text(lib),
        "## SKILL SUGGESTIONS\n"
        + "\n".join(f"  {k}: {', '.join(v)}" for k, v in SUGGESTED.items()),
        "## PIPELINES ON DISK\n  " + (", ".join(pipelines) or "none"),
        "## REFERENCE\n" + _reference(),
    ]
    if live is not None:
        parts.append("## LIVE STATE\n" + live_text(live))
    if current:
        parts.append(f"## OPEN PIPELINE ({current[0]})\n{current[1]}")
    return "\n\n".join(parts)


IMAGE_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
MAX_IMAGE = 10_000_000


def attachments_dir() -> Path:
    import os

    home = Path(os.environ.get("AGENTLINE_HOME") or Path.home() / ".agentline")
    return home / "assistant" / "attachments"


def save_attachment(data: bytes, content_type: str) -> dict[str, Any]:
    """Store an image the user attached; its id is its content hash."""
    import hashlib

    ext = IMAGE_TYPES.get(content_type.split(";")[0].strip().lower())
    if ext is None:
        raise ValidationError(f"only images can be attached ({', '.join(IMAGE_TYPES)})")
    if not data or len(data) > MAX_IMAGE:
        raise ValidationError("the image is empty or larger than 10 MB")
    digest = hashlib.sha256(data).hexdigest()[:32]
    d = attachments_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{digest}{ext}"
    if not path.exists():
        path.write_bytes(data)
    return {"id": f"{digest}{ext}", "path": str(path), "size": len(data)}


def attachment_path(att_id: str) -> Path | None:
    if not re.match(r"^[0-9a-f]{32}\.(png|jpg|gif|webp)$", att_id):
        return None
    p = attachments_dir() / att_id
    return p if p.is_file() else None


def instructions_text(instructions: str, skills_note: str) -> str:
    """The user's Chat instructions (they win over the guide where they differ) and how this
    agent invokes skills."""
    out = "## CHAT INSTRUCTIONS (set by the user; follow them)\n" + (
        instructions.strip() or "(none)"
    )
    return out + "\n\n## USING SKILLS\n" + skills_note


def stage_attachments(messages: list[dict[str, Any]], into: Path) -> dict[str, Path]:
    """Copy the attached images into the agent's working folder (`into/attachments/`): Claude
    Code reads only inside its folder (and folders given with --add-dir), and the saved images
    live outside it. Returns {attachment id: path the agent can read}."""
    staged: dict[str, Path] = {}
    for m in messages:
        for a in m.get("attachments") or []:
            src = attachment_path(str(a))
            if src is None or str(a) in staged:
                continue
            dst = into / "attachments" / src.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists():
                shutil.copyfile(src, dst)
            staged[str(a)] = dst
    return staged


def build_prompt(
    context: str,
    messages: list[dict[str, Any]],
    instructions: str = "",
    staged: dict[str, Path] | None = None,
    transcripts: dict[str, dict[str, Any]] | None = None,
) -> str:
    convo = []
    for m in messages[-MAX_HISTORY:]:
        role = "USER" if m.get("role") == "user" else "ASSISTANT"
        text = f"{role}: {str(m.get('content') or '')[:20_000]}"
        if transcripts:  # an agent that can't see images gets what a vision model read
            for a in m.get("attachments") or []:
                t = transcripts.get(str(a))
                if t:
                    who = "the user (edited)" if t.get("edited") else t.get("by")
                    text += (
                        f"\n[The user attached an image. You can't see it; this transcript"
                        f" of it is by {who}. Build from it; if a part is UNCLEAR, ask.]\n"
                        + str(t["text"])
                    )
            convo.append(text)
            continue
        paths = [
            str(p)
            for a in m.get("attachments") or []
            if (p := (staged or {}).get(str(a)) or attachment_path(str(a)))
        ]
        if paths:
            text += (
                "\n[The user attached image(s). Open each with your Read tool and use what it"
                " shows (a diagram of the pipeline they want, a screenshot, ...): "
                + ", ".join(paths)
                + "]"
            )
        convo.append(text)
    return (
        system_text(context, instructions)
        + "\n\n## CONVERSATION\n"
        + "\n\n".join(convo)
        + "\n\nReply to the last USER message with the JSON object."
    )


def system_text(context: str, instructions: str = "") -> str:
    """Everything but the conversation: the guide, the user's instructions, the context."""
    return GUIDE + "\n\n" + (instructions + "\n\n" if instructions else "") + context


# -- the agent's reply ------------------------------------------------------------------------
def _json_object(text: str) -> dict[str, Any] | None:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    for cand in ([fence.group(1)] if fence else []) + [text, text[text.find("{") :]]:
        try:
            v = json.loads(cand)
        except (ValueError, TypeError):
            # a trailing remark after the object: decode the first object only
            try:
                v, _ = json.JSONDecoder().raw_decode(cand)
            except (ValueError, TypeError):
                continue
        if isinstance(v, dict):
            return v
    return None


def parse_reply(output: Any) -> dict[str, Any]:
    """{message, choices, actions} from a runner's output (a dict, or text holding JSON)."""
    data = output if isinstance(output, dict) else _json_object(str(output or ""))
    if data is None:
        return {"message": str(output or "").strip() or "(no answer)", "choices": [], "actions": []}
    choices = [str(c)[:200] for c in (data.get("choices") or []) if str(c).strip()][:5]
    actions = [a for a in (data.get("actions") or []) if isinstance(a, dict)]
    return {"message": str(data.get("message") or ""), "choices": choices, "actions": actions}


# -- checking proposed actions ----------------------------------------------------------------
def _safe_rel(rel: str) -> bool:
    return bool(_REL.match(rel)) and ".." not in rel.split("/")


def check_setting(a: dict[str, Any], cfg: Config | None) -> list[str]:
    section, name, body = a.get("section"), str(a.get("name") or ""), a.get("body")
    if section not in SECTIONS:
        return [f"unknown settings section {section!r}"]
    if not re.match(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$", name):
        return [f"bad name {name!r}"]
    if not isinstance(body, dict):
        return ["body must be a table"]
    if any(k in body for k in ("api_key", "key", "token", "secret")):
        return ["keys never go in agentline.toml; use a key action"]
    try:
        SECTIONS[section](name, {k: v for k, v in body.items() if v not in (None, "", [], {})})
    except ValidationError as e:
        return [str(e)]
    if section == "models" and body.get("provider") not in ai.providers(cfg):
        return [f"unknown model source {body.get('provider')!r}"]
    if section == "harnesses" and ai.kind_name(body.get("kind", name)) not in ai.KINDS:
        return [f"unknown harness kind {body.get('kind')!r}"]
    return []


def check_pipeline(a: dict[str, Any], root: Path, registry: Any, cfg: Config | None) -> list[str]:
    """Validate a proposed pipeline in a scratch copy of its folder, with its files on top."""
    path = str(a.get("path") or "")
    if not path.endswith(".yaml") or not _safe_rel(path):
        return [f"bad pipeline path {path!r} (a relative path ending in .yaml)"]
    yaml_text = a.get("yaml")
    if not isinstance(yaml_text, str) or not yaml_text.strip():
        return ["the pipeline YAML is empty"]
    files = a.get("files") or {}
    if not isinstance(files, dict) or len(files) > MAX_FILES:
        return ["files must be a mapping of at most 40 relative paths to text"]
    for rel, text in files.items():
        if not _safe_rel(str(rel)) or not isinstance(text, str) or len(text) > MAX_FILE_BYTES:
            return [f"bad file {rel!r}"]
    target_dir = (root / path).parent
    with tempfile.TemporaryDirectory(prefix="agentline-assistant-") as tmp:
        scratch = Path(tmp)
        if target_dir != root and target_dir.is_dir():
            shutil.copytree(target_dir, scratch, dirs_exist_ok=True)
        else:  # a pipeline at the top: only its usual subfolders
            for sub in ("prompts", "schemas"):
                if (target_dir / sub).is_dir():
                    shutil.copytree(target_dir / sub, scratch / sub, dirs_exist_ok=True)
        for rel, text in files.items():
            (scratch / rel).parent.mkdir(parents=True, exist_ok=True)
            (scratch / rel).write_text(text, encoding="utf-8")
        p = scratch / Path(path).name
        p.write_text(yaml_text, encoding="utf-8")
        try:
            spec = parse_spec(yaml_text, path=p, base_dir=scratch)
        except SpecError as e:
            return [str(i) for i in e.issues]
        except ValidationError as e:
            return [str(e)]
        except Exception as e:  # a YAML syntax error and the like
            return [f"{type(e).__name__}: {e}"]
        return [
            f"{i.path}: {i.message}" + (f" ({i.hint})" if i.hint else "")
            for i in validate_spec(spec, registry, cfg)
        ]


@dataclass
class Live:
    """What exists right now beyond the config: shown to the agent (LIVE STATE) and used to
    check proposals. None = unknown (no engine behind the call): not checked."""

    flows: list[str] | None = None  # loaded pipelines (what start_run takes)
    starts: dict[str, str] = field(default_factory=dict)  # pipeline -> its start input
    runs: list[dict[str, Any]] | None = None  # recent runs {id, flow, status}
    gates: list[dict[str, Any]] | None = None  # waiting gates {run_id, node_id, prompt}
    issues: list[dict[str, Any]] | None = None
    incidents: list[str] | None = None  # crash report ids
    budget: float | None = None
    chat: dict[str, Any] | None = None  # platform -> {linked, keys_set}; "notify" -> prefs
    history: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # file -> commits
    detected: list[dict[str, Any]] | None = None  # MCP servers found on this machine
    own_skills: list[str] | None = None
    triage: list[dict[str, Any]] | None = None  # recent failure triages
    reviews: list[dict[str, Any]] | None = None  # recent run reviews (agentline.review)
    lessons: dict[str, str] = field(default_factory=dict)  # pipeline -> its lesson library
    github: dict[str, Any] | None = None  # GitHub reporting setup (github_setup.status)
    conversations: list[dict[str, Any]] | None = None  # the Chat's past conversations
    me: dict[str, Any] | None = None  # who is chatting: {kind, name, admin} (api.identity)
    people: list[dict[str, Any]] | None = None  # {name, admin}; only for an admin `me`


def gather_live(
    files: Any,
    app: Any = None,
    issues: Any = None,
    incidents: Any = None,
    pipeline: str | None = None,
    me: dict[str, Any] | None = None,
) -> Live:
    """LIVE STATE for a turn. Each part is best effort: one that fails is left unknown.
    `me` is the request's identity: people are listed only for an admin (never tokens)."""
    import contextlib
    import os

    from agentline import costs, history, mcp_detect
    from agentline.secrets import read_secrets

    live = Live()
    if me is not None:
        live.me = me
        if me.get("admin"):
            with contextlib.suppress(Exception):
                from agentline.people import PeopleStore

                live.people = [{"name": p.name, "admin": p.admin} for p in PeopleStore().list()]
    if app is not None:
        with contextlib.suppress(Exception):
            live.flows = list(app.flow_names())
        with contextlib.suppress(Exception):
            from agentline.pipeline.start import start_info

            for name in live.flows or []:
                spec = app.graph(name).meta.get("spec")
                if spec is not None:
                    info = start_info(spec)
                    fields = f": {', '.join(info['fields'])}" if info["fields"] else ""
                    live.starts[name] = info["input"] + (fields if info["input"] == "json" else "")
        with contextlib.suppress(Exception), app.ledger.tx() as tx:
            runs = tx.list_runs(None, 15)
            live.runs = [{"id": r.id, "flow": r.flow, "status": r.status} for r in runs]
            gates = []
            for run in tx.list_runs("waiting", 200):
                for x in tx.list_executions(run.id):
                    if x.status == "waiting" and x.detail.get("kind") == "gate":
                        gates.append(
                            {"run_id": run.id, "node_id": x.node_id,
                             "prompt": x.detail.get("prompt", "")}
                        )  # fmt: skip
            live.gates = gates
    if issues is not None:
        with contextlib.suppress(Exception):
            live.issues = [
                {"number": i["number"], "status": i.get("status"), "title": i.get("title")}
                for i in issues.list()[:40]
            ]
    if incidents is not None:
        with contextlib.suppress(Exception):
            live.incidents = [str(r.get("id")) for r in incidents.list(limit=40)]
    with contextlib.suppress(Exception):
        live.budget = costs.read_budget().get("monthly_usd")
    with contextlib.suppress(Exception):
        from agentline.chatlink import state
        from agentline.chatlink.platforms import PLATFORMS

        chat: dict[str, Any] = {
            name: {"linked": bool(state.ChatState(name).linked()), "keys_set": p.keys_set()}
            for name, p in PLATFORMS.items()
        }
        chat["notify"] = state.notify_prefs()
        live.chat = chat
    if pipeline:
        with contextlib.suppress(Exception):
            name = files.name_of(files.resolve(pipeline))
            live.history[name] = history.log(files.root, f"{name}.yaml", 12)
    with contextlib.suppress(Exception):
        found = mcp_detect.detect(
            project_dirs=mcp_detect.project_dirs(files.root),
            current_keys={**os.environ, **read_secrets()},
        )
        live.detected = mcp_detect.public(found, (files.config or Config()).mcp)
    with contextlib.suppress(Exception):
        live.own_skills = [
            x.name for x in SkillLibrary().list() if x.source in ("local", "installed")
        ]
    with contextlib.suppress(Exception):
        from agentline.triage import TriageStore, triage_incidents

        if incidents is not None:  # crash reports are app reports (incidents are redacted)
            triage_incidents(incidents, TriageStore(), files.config)
        live.triage = [
            {k: t.get(k) for k in ("id", "run", "node", "verdict", "status", "summary", "incident")}
            | {"github": bool(t.get("github"))}
            for t in TriageStore().list(limit=20)
        ]
    with contextlib.suppress(Exception):
        from agentline.review import ReviewStore

        live.reviews = [_review_live(r) for r in ReviewStore().list(limit=10)]
    if pipeline:
        with contextlib.suppress(Exception):
            from agentline import lessons

            path = files.resolve(pipeline)
            if text := lessons.text_for_prompt(path, 4000):
                live.lessons[files.name_of(path)] = text
    with contextlib.suppress(Exception):
        from agentline import github_setup

        live.github = github_setup.status(files.config, files)
    with contextlib.suppress(Exception):
        from agentline import conversations

        live.conversations = [
            {k: c[k] for k in ("id", "title", "updated_at", "archived")}
            for c in conversations.list_all()[:30]
        ]
    return live


def _review_live(r: dict[str, Any]) -> dict[str, Any]:
    props = [
        {"index": i, "type": a.get("type"), "title": title(a),
         "probe": (a.get("probe") or {}).get("verdict")}
        for i, a in enumerate(r.get("actions") or [])
    ]  # fmt: skip
    return {k: r.get(k) for k in ("id", "run", "flow", "status", "summary")} | {"proposals": props}


def live_text(live: Live) -> str:
    lines = []
    if live.me is not None:
        role = "admin" if live.me.get("admin") else "not an admin: can't manage people"
        lines.append(f"You are talking with {live.me.get('name')} ({role}).")
    if live.people is not None:
        lines.append(
            "PEOPLE (Settings > People; each signs in with their own token): "
            + (", ".join(p["name"] + (" (admin)" if p["admin"] else "") for p in live.people)
               or "none")
        )  # fmt: skip
    if live.flows is not None:
        shown = [f"{f} ({live.starts[f]})" if f in live.starts else f for f in live.flows]
        lines.append("LOADED pipelines (start_run, with their start input): "
                     + (", ".join(shown) or "none"))  # fmt: skip
    if live.runs is not None:
        lines.append("Recent runs:")
        lines += [f"  {r['id']}: {r['flow']} {r['status']}" for r in live.runs] or ["  none"]
    if live.gates is not None:
        lines.append("Gates waiting for a decision:")
        lines += [
            f"  run {g['run_id']} node {g['node_id']}: {str(g.get('prompt') or '')[:160]}"
            for g in live.gates
        ] or ["  none"]
    if live.issues is not None:
        lines.append("Issues (Problems):")
        lines += [
            f"  #{i['number']} [{i.get('status')}] {str(i.get('title') or '')[:120]}"
            for i in live.issues
        ] or ["  none"]
    if live.incidents is not None:
        lines.append("Crash reports: " + (", ".join(live.incidents[:20]) or "none"))
    lines.append(
        "Monthly budget: " + (f"${live.budget:g}" if live.budget is not None else "none set")
    )
    if live.chat is not None:
        lines.append("Chat apps:")
        for name, st in live.chat.items():
            if name != "notify":
                state = "linked" if st.get("linked") else "not linked"
                keys = "" if st.get("keys_set") else " (keys not set)"
                lines.append(f"  {name}: {state}{keys}")
        lines.append(f"  notify: {json.dumps(live.chat.get('notify') or {})}")
    for name, commits in live.history.items():
        lines.append(f"HISTORY of {name} (newest first):")
        lines += [f"  {c['commit'][:10]} {c.get('message', '')}" for c in commits[:12]] or [
            "  none"
        ]
    if live.detected is not None:
        new = [d for d in live.detected if d.get("status") == "new"]
        lines.append("DETECTED MCP servers on this machine, not imported yet (mcp_import ids):")
        for d in new:
            keys = ", ".join(k["key"] for k in d["secrets"])
            needs = f", keys {keys} come with it" if keys else ""
            lines.append(f"  {d['id']}: {d['name']} ({d['source']}, {d.get('transport')}{needs})")
        if not new:
            lines.append("  none")
    if live.own_skills is not None:
        lines.append("Your own and installed skills: " + (", ".join(live.own_skills) or "none"))
    if live.triage is not None:
        runs = [t for t in live.triage if not t.get("incident")]
        lines.append("TRIAGE of failed runs (newest first):")
        lines += [
            f"  {t['id']}: run {t['run']} node {t.get('node') or '-'}:"
            f" {t.get('verdict') or t.get('status')} {str(t.get('summary') or '')[:120]}"
            + (" (on GitHub)" if t.get("github") else "")
            for t in runs
        ] or ["  none"]
        app = [t for t in live.triage if t.get("verdict") == "infrastructure"]
        lines.append("APP REPORTS (agentline's own problems; not on Problems):")
        lines += [
            f"  {t['id']}: "
            + (f"crash report {t['incident']}" if t.get("incident") else f"run {t['run']}")
            + f": {str(t.get('summary') or '')[:120]}"
            + (" (on GitHub)" if t.get("github") else " (not reported yet)")
            for t in app
        ] or ["  none"]
    if live.reviews is not None:
        lines.append("REVIEWS (recent run reviews; probe takes the proposal index):")
        for r in live.reviews:
            lines.append(
                f"  {r['id']}: run {r['run']} of {r['flow']} {r.get('status')}:"
                f" {str(r.get('summary') or '')[:120]}"
            )
            lines += [
                f"    [{p['index']}] {p['title']}"
                + (f" (probed: {p['probe']})" if p.get("probe") else "")
                for p in r.get("proposals") or []
            ]
        if not live.reviews:
            lines.append("  none")
    for name, text in live.lessons.items():
        lines.append(f"LESSONS of pipeline {name} (its lesson library):")
        lines += [f"  {ln}" for ln in text.splitlines() if ln.strip()]
    if live.github is not None:
        g = live.github
        gh = g.get("gh") or {}
        if gh.get("logged_in"):
            cli = f"signed in as {gh.get('user')}"
        else:
            cli = "installed, not signed in" if gh.get("installed") else "not installed"
        lines.append(
            f"GITHUB for reports: repo {g.get('repo') or 'not set'}; token from"
            f" {g.get('token_source') or 'nowhere yet'}; GitHub CLI {cli};"
            f" auto-report {'on' if g.get('auto') else 'off'}"
        )
        det = ", ".join(
            f"{d['repo']} ({d['where']}, {d['protocol']})" for d in g.get("detected") or []
        )
        lines.append(f"  DETECTED remotes: {det or 'none'}")
    if live.conversations is not None:
        import time

        lines.append("CONVERSATIONS (your past chats with the user, newest first):")
        lines += [
            f"  {c['id']}: {str(c.get('title') or '')[:80]}"
            f" ({'archived' if c.get('archived') else 'active'}, last message"
            f" {time.strftime('%Y-%m-%d', time.localtime(c.get('updated_at') or 0))})"
            for c in live.conversations
        ] or ["  none"]
    return "\n".join(lines)


def _pipeline_known(name: Any, root: Path) -> list[str]:
    n = str(name or "").removesuffix(".yaml")
    if not n or not _safe_rel(n + ".yaml"):
        return [f"bad pipeline name {name!r}"]
    if not (root / f"{n}.yaml").is_file():
        return [f"no pipeline file {n}.yaml"]
    return []


def _check_one(a: dict[str, Any], root: Path, cfg: Config | None, live: Live) -> list[str]:
    from agentline.issues import MAX_NOTE, STATUSES
    from agentline.skills import NAME

    kind = a.get("type")
    if kind == "delete_setting":
        section, name = a.get("section"), str(a.get("name") or "")
        if section not in SECTIONS:
            return [f"unknown settings section {section!r}"]
        if section == "providers":  # built-in sources can be removed too
            if name not in ai.providers(cfg):
                return [f"no model source {name!r}"]
        elif name not in getattr(cfg or Config(), str(section)):
            return [f"{name!r} isn't in agentline.toml (built-ins can't be deleted)"]
        if section == "providers" and cfg:
            users = [m.name for m in cfg.models.values() if m.provider == name]
            if users and not a.get("with_models"):
                return [
                    f"model source {name!r} is used by models: {', '.join(users)}"
                    " (set with_models: true to delete them too)"
                ]
            gone = users + [
                m.name for m in ai.BUILTIN_MODELS.values() if m.provider == name
            ]  # a removed built-in source takes its built-in models along
            defaults = [x.name for x in cfg.harnesses.values() if x.model in gone]
            if defaults:
                return [f"models of {name!r} are defaults of harnesses: {', '.join(defaults)}"]
        if section == "models" and cfg:
            users = [x.name for x in cfg.harnesses.values() if x.model == name]
            if users:
                return [f"model {name!r} is the default of harnesses: {', '.join(users)}"]
        return []
    if kind == "claude_logout":
        prov = ai.providers(cfg).get(str(a.get("provider") or ""))
        if prov is None or prov.auth != "subscription":
            return [f"{a.get('provider')!r} isn't a Claude subscription source"]
        return []
    if kind == "model_visibility":
        if str(a.get("model") or "") not in ai.models(cfg):
            return [f"unknown model {a.get('model')!r}"]
        return [] if isinstance(a.get("hidden"), bool) else ["hidden must be true or false"]
    if kind == "source_models":
        models = a.get("models")
        if a.get("provider") not in ai.providers(cfg):
            return [f"unknown model source {a.get('provider')!r}"]
        ok = isinstance(models, list) and all(isinstance(m, str) and m.strip() for m in models)
        return [] if ok else ["models must be a list of model ids"]
    if kind in ("key", "delete_key"):
        name = str(a.get("name") or "")
        a.pop("value", None)  # never carry a value, even if the model made one up
        return [] if _KEY.match(name) else [f"bad key name {name!r}"]
    if kind == "chat_uses":
        harness = str(a.get("harness") or "")
        if harness not in ai.harnesses(cfg):
            return [f"unknown harness {harness!r}"]
        try:
            ai.resolve(harness, a.get("model") or None, cfg)
        except ValidationError as e:
            return [str(e)]
        return []
    if kind == "chat_vision":
        choice = str(a.get("choice") or "")
        if choice not in ("@auto", "@claude-code") and choice not in ai.models(cfg):
            return [f"choice must be @auto, @claude-code or a [models] name, not {choice!r}"]
        return []
    if kind == "chat_instructions":
        from agentline.chat_instructions import MAX_BYTES

        text = a.get("text")
        if not isinstance(text, str) or not text.strip():
            return ["the instructions are empty (to go back to the defaults, say so)"]
        return [] if len(text.encode()) <= MAX_BYTES else ["the instructions are longer than 20 KB"]
    if kind == "fetch_skills":
        return []
    if kind == "skill":
        name = str(a.get("name") or "")
        ok = NAME.match(name) and str(a.get("body") or "").strip()
        return [] if ok else ["a skill needs a lowercase name and a body"]
    if kind == "delete_skill":
        name = str(a.get("name") or "")
        if live.own_skills is not None and name not in live.own_skills:
            return [
                f"{name!r} isn't one of your own or installed skills "
                "(library skills can't be deleted)"
            ]
        return [] if NAME.match(name) else [f"bad skill name {name!r}"]
    if kind == "install_skills":
        from agentline.skill_sources import parse_source

        try:
            parse_source(str(a.get("source") or ""))
        except ValidationError as e:
            return [str(e)]
        names = a.get("names")
        if names is not None and (
            not isinstance(names, list)
            or not names
            or not all(isinstance(n, str) and NAME.match(n) for n in names)
        ):
            return ["names must be a list of skill names (or left out for all of them)"]
        return []
    if kind == "mcp_import":
        items = a.get("items")
        if not isinstance(items, list) or not items or not all(isinstance(i, dict) for i in items):
            return ["items must be a list of {id, name?}"]
        if live.detected is not None:
            new = {d["id"] for d in live.detected if d.get("status") == "new"}
            unknown = [str(i.get("id")) for i in items if i.get("id") not in new]
            if unknown:
                return [f"not a detected new server: {', '.join(unknown)}"]
        return []
    if kind == "chat_app":
        from agentline.chatlink.platforms import PLATFORMS

        if a.get("platform") not in PLATFORMS:
            return [f"unknown chat app {a.get('platform')!r} ({', '.join(PLATFORMS)})"]
        return [] if a.get("op") in CHAT_APP_OPS else [f"op must be {' | '.join(CHAT_APP_OPS)}"]
    if kind == "chat_notify":
        prefs = {k: a[k] for k in NOTIFY_KEYS if k in a}
        if not prefs or not all(isinstance(v, bool) for v in prefs.values()):
            return [f"set one or more of {', '.join(NOTIFY_KEYS)} to true or false"]
        return []
    if kind == "engine":
        op = a.get("op")
        if op not in ENGINE_OPS:
            return [f"op must be {' | '.join(ENGINE_OPS)}"]
        if op == "reveal" and a.get("target", "data") not in REVEAL_TARGETS:
            return [f"target must be {' | '.join(REVEAL_TARGETS)}"]
        if "path" in a and not (isinstance(a["path"], str) and a["path"].strip()):
            return ["path must be a full folder path"]
        return []
    if kind in ("export", "dry_run", "restore", "delete_pipeline"):
        issues = _pipeline_known(a.get("pipeline"), root)
        if issues:
            return issues
        a["pipeline"] = str(a["pipeline"]).removesuffix(".yaml")
        if kind == "dry_run":
            if "routes" in a and not isinstance(a["routes"], dict):
                return ["routes must map a router to a list of labels"]
            return []
        if kind == "restore":
            commit = str(a.get("commit") or "")
            if not _COMMIT.match(commit):
                return [f"bad commit {commit!r}"]
            known = live.history.get(a["pipeline"])
            if known is not None and not any(c["commit"].startswith(commit) for c in known):
                return [f"{commit} isn't in the history of {a['pipeline']}"]
        return []
    if kind == "import":
        folder = a.get("folder")
        if folder and not _safe_rel(str(folder)):
            return [f"bad folder {folder!r}"]
        return []
    if kind == "start_run":
        name = str(a.get("pipeline") or "")
        if live.flows is not None and name not in live.flows:
            return [f"no loaded pipeline {name!r} (loaded: {', '.join(live.flows) or 'none'})"]
        if not name:
            return ["which pipeline?"]
        text = a.get("text")
        if text is not None and (not isinstance(text, str) or not text.strip()):
            return ["text must be the words that start the run"]
        if text is None and live.starts.get(name) in ("text", "chat"):
            return [f"{name} starts from {live.starts[name]}: give 'text', not a payload"]
        return []
    if kind == "run_message":
        if not _RUN.match(str(a.get("run") or "")):
            return ["bad run id"]
        text = a.get("text")
        return [] if isinstance(text, str) and text.strip() else ["what's the message?"]
    if kind == "retry":
        return [] if _RUN.match(str(a.get("run") or "")) else ["bad run id"]
    if kind == "gate":
        run, node = str(a.get("run") or ""), str(a.get("node") or "")
        if not _RUN.match(run) or not node:
            return ["a gate decision needs a run id and the gate node"]
        if a.get("decision") not in ("approve", "reject"):
            return ["decision must be approve or reject"]
        if live.gates is not None and not any(
            g["run_id"] == run and g["node_id"] == node for g in live.gates
        ):
            return [f"no gate {node!r} waiting in run {run}"]
        return []
    if kind == "issue":
        n = a.get("number")
        if not isinstance(n, int) or isinstance(n, bool):
            return ["number must be the issue number"]
        if live.issues is not None and n not in {i["number"] for i in live.issues}:
            return [f"no issue #{n}"]
        if a.get("status") is None and not str(a.get("note") or "").strip():
            return ["change the status, add a note, or both"]
        if a.get("status") is not None and a["status"] not in STATUSES:
            return [f"status must be one of {', '.join(STATUSES)}"]
        return [] if len(str(a.get("note") or "")) <= MAX_NOTE else ["the note is too long"]
    if kind == "incident":
        if a.get("op") == "clear":
            return []
        if a.get("op") not in ("delete", "seen"):
            return ["op must be delete, seen or clear"]
        if live.incidents is not None and a.get("id") not in live.incidents:
            return [f"no crash report {a.get('id')!r}"]
        return []
    if kind == "triage":  # older runs aren't in LIVE STATE: the API says if one is missing
        return [] if _RUN.match(str(a.get("run") or "")) else ["bad run id"]
    if kind == "lesson":
        text = " ".join(str(a.get("text") or "").split())
        if not text:
            return ["the lesson is empty"]
        a["text"] = text
        return [] if len(text) <= 300 else ["a lesson is one line of at most 300 characters"]
    if kind == "review":  # older runs aren't in LIVE STATE: the API says if one is missing
        return [] if _RUN.match(str(a.get("run") or "")) else ["bad run id"]
    if kind == "probe":
        rid = str(a.get("review") or "")
        if not re.fullmatch(r"v[0-9a-f]{12}", rid):
            return [f"bad review id {rid!r}"]
        i = a.get("index")
        if not isinstance(i, int) or isinstance(i, bool):
            return ["index must be the proposal's number in the review"]
        if live.reviews is not None:
            hit = next((r for r in live.reviews if r["id"] == rid), None)
            if hit is None:
                return [f"no review {rid!r}"]
            props = {p["index"]: p for p in hit.get("proposals") or []}
            if i not in props or props[i]["type"] != "pipeline":
                return [f"proposal {i} of review {rid} isn't a pipeline edit"]
        return []
    if kind == "pipeline_lesson":
        from agentline import lessons

        bad = _pipeline_known(a.get("pipeline"), root)
        return bad or lessons.check(a)
    if kind == "report_issue":
        tid = str(a.get("triage") or "")
        if live.triage is not None:
            hit = next((t for t in live.triage if t["id"] == tid), None)
            if hit is None:
                return [f"no triage {tid!r}"]
            if hit.get("verdict") != "infrastructure":
                return [f"triage {tid} isn't an infrastructure verdict (nothing to report)"]
        return [] if re.fullmatch(r"t[0-9a-f]{12}", tid) else [f"bad triage id {tid!r}"]
    if kind == "github":
        from agentline.github_setup import parse_remote
        from agentline.integrations.github import split_repo

        op = a.setdefault("op", "save")
        if op not in ("save", "login", "test"):
            return ["op must be save, login or test"]
        if op == "save":
            repo = str(a.get("repo") or "").strip()
            if repo:
                repo = (parse_remote(repo) or (repo, ""))[0]
                try:
                    split_repo(repo)
                except ValueError as e:
                    return [str(e)]
                a["repo"] = repo
            if "auto" in a and not isinstance(a["auto"], bool):
                return ["auto must be true or false"]
            if not repo and "auto" not in a:
                return ["give the repo (owner/name), auto, or both"]
        return []
    if kind == "conversation":
        if a.get("op") not in ("archive", "unarchive"):
            return ["op must be archive or unarchive"]
        cid = str(a.get("id") or "")
        if live.conversations is not None:
            c = next((x for x in live.conversations if x["id"] == cid), None)
            if c is None:
                return [f"no conversation {cid!r} (see CONVERSATIONS)"]
            if bool(c.get("archived")) == (a["op"] == "archive"):
                return [f"conversation {cid!r} is already {a['op']}d"]
        return []
    if kind == "person":
        from agentline.people import check_person_name

        op, name = a.get("op"), str(a.get("name") or "")
        if op not in PERSON_OPS:
            return [f"op must be one of {', '.join(PERSON_OPS)}"]
        if live.me is not None and not live.me.get("admin"):
            return ["only an admin can manage people"]
        if op == "add":
            try:
                check_person_name(name)
            except ValidationError as e:
                return [str(e)]
            if live.people is not None and any(
                p["name"].lower() == name.lower() for p in live.people
            ):
                return [f"{name!r} is already a person here (see PEOPLE)"]
        elif live.people is not None and not any(
            p["name"].lower() == name.lower() for p in live.people
        ):
            return [f"no person {name!r} (see PEOPLE)"]
        if op == "admin" and not isinstance(a.get("admin"), bool):
            return ["admin must be true or false"]
        return []
    if kind == "budget":
        v = a.get("monthly_usd")
        ok = v is None or (isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0)
        return [] if ok else ["monthly_usd must be a number of dollars, or null to remove it"]
    return [f"unknown action type {kind!r}"]


def _renamed(a: dict[str, Any]) -> dict[str, Any]:
    """An action with names from before phase 35 (section `agents`, `chat_uses.agent`, harness
    `llm`) in the new spelling."""
    a = dict(a)
    if a.get("section") == "agents":
        a["section"] = "harnesses"
    if a.get("type") == "chat_uses" and "agent" in a:
        a.setdefault("harness", a.pop("agent"))
    if a.get("type") == "chat_uses" and a.get("harness") in ai.HARNESS_ALIASES:
        a["harness"] = ai.HARNESS_ALIASES[a["harness"]]
    return a


def check_actions(
    actions: list[dict[str, Any]],
    root: Path,
    registry: Any,
    cfg: Config | None,
    live: Live | None = None,
) -> list[dict[str, Any]]:
    out = []
    for a in map(_renamed, actions):
        kind = a.get("type")
        issues: list[str]
        if kind == "setting":
            issues = check_setting(a, cfg)
        elif kind == "pipeline":
            issues = check_pipeline(a, root, registry, cfg)
            a["exists"] = (root / str(a.get("path") or "x")).exists()
        else:
            issues = _check_one(a, root, cfg, live or Live())
        out.append({**a, "issues": issues})
    return out


def title(a: dict[str, Any]) -> str:
    """A proposal in a few words (chat links; the web has its own in lib/assistant.ts)."""
    t = a.get("type")
    simple = {
        "setting": f"set {a.get('section')}.{a.get('name')}",
        "delete_setting": f"delete {a.get('section')}.{a.get('name')}",
        "claude_logout": "sign Claude Code out",
        "source_models": f"choose the models of {a.get('provider')}",
        "model_visibility": f"{'hide' if a.get('hidden') else 'show'} {a.get('model')} in pickers",
        "key": f"set key {a.get('name')}",
        "delete_key": f"delete key {a.get('name')}",
        "chat_uses": f"Chat uses {a.get('harness')}",
        "chat_vision": f"images read by {a.get('choice')}",
        "chat_instructions": "change the Chat instructions",
        "fetch_skills": "download the skill library",
        "skill": f"add your skill {a.get('name')}",
        "delete_skill": f"delete skill {a.get('name')}",
        "install_skills": f"install skills from {a.get('source')}",
        "mcp_import": "import MCP servers",
        "chat_app": f"{a.get('op')} {a.get('platform')}",
        "chat_notify": "change chat notifications",
        "engine": f"engine: {a.get('op')}",
        "pipeline": f"{'update' if a.get('exists') else 'create'} pipeline {a.get('path')}",
        "export": f"export {a.get('pipeline')}",
        "import": "import a pipeline",
        "dry_run": f"dry run {a.get('pipeline')}",
        "restore": f"restore {a.get('pipeline')} to {str(a.get('commit'))[:8]}",
        "delete_pipeline": f"delete pipeline {a.get('pipeline')}",
        "start_run": f"start {a.get('pipeline')}",
        "run_message": f"message run {a.get('run')}",
        "retry": f"retry run {a.get('run')}" + (f" at {a.get('node')}" if a.get("node") else ""),
        "gate": f"{a.get('decision')} {a.get('node')} in run {a.get('run')}",
        "issue": f"update issue #{a.get('number')}",
        "incident": {"clear": "delete crash reports", "seen": "mark a crash report seen"}.get(
            str(a.get("op")), "delete a crash report"
        ),
        "budget": "set the monthly budget",
        "triage": f"triage run {a.get('run')}" + (f" at {a.get('node')}" if a.get("node") else ""),
        "lesson": "add a lesson to the Chat instructions",
        "report_issue": "report it on GitHub",
        "review": f"review run {a.get('run')}",
        "probe": f"probe proposal {a.get('index')} of review {a.get('review')}",
        "pipeline_lesson": f"add a lesson for {a.get('node')} to {a.get('pipeline')}",
        "conversation": f"{a.get('op')} the conversation {a.get('id')}",
        "person": {
            "add": f"add {a.get('name')}" + (" as an admin" if a.get("admin") else ""),
            "token": f"issue a new token for {a.get('name')}",
            "admin": f"{'make' if a.get('admin') else 'unmake'} {a.get('name')} admin",
        }.get(str(a.get("op")), f"revoke {a.get('name')}"),
        "github": {"login": "sign in with GitHub", "test": "test the GitHub setup"}.get(
            str(a.get("op")), f"report to GitHub repo {a.get('repo') or '(unchanged)'}"
        ),
    }
    return simple.get(str(t), str(t))


def json_text(reply: dict[str, Any]) -> str:
    """A reply as the JSON the agent sent (without the checker's additions)."""
    clean = [
        {k: v for k, v in a.items() if k not in ("issues", "exists")} for a in reply["actions"]
    ]
    return json.dumps({**reply, "actions": clean})


# actions whose issues the agent can fix by looking again (a pipeline that doesn't validate, a
# run / gate / issue / commit / server that isn't there): one repair round, like pipelines
REPAIRABLE = ("pipeline", "start_run", "gate", "issue", "restore", "mcp_import", "delete_skill",
              "install_skills",
              "delete_setting", "incident", "dry_run", "export", "report_issue",
              "delete_pipeline", "conversation")  # fmt: skip


def repair_note(actions: list[dict[str, Any]]) -> str | None:
    bad = [a for a in actions if a.get("issues") and a.get("type") in REPAIRABLE]
    if not bad:
        return None
    lines = [
        "Some proposed actions don't check out (a pipeline that doesn't validate, or something"
        " that doesn't exist: see LIVE STATE). Fix them and reply with the full JSON again:"
    ]
    for a in bad:
        lines.append(f"{a.get('path') or title(a)}:")
        lines += [f"  - {i}" for i in a["issues"]]
    return "\n".join(lines)


def write_pipeline(root: Path, path: str, yaml_text: str, files: dict[str, str]) -> Path:
    """Write an applied pipeline and its files (paths already checked)."""
    from agentline.api.editor import _write

    if not path.endswith(".yaml") or not _safe_rel(path):
        raise ValidationError(f"bad pipeline path {path!r}")
    target = (root / path).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValidationError(f"{path!r} is outside the pipelines folder")
    for rel, text in files.items():
        if not _safe_rel(rel) or not isinstance(text, str):
            raise ValidationError(f"bad file {rel!r}")
        dest = (target.parent / rel).resolve()
        if not dest.is_relative_to(root.resolve()):
            raise ValidationError(f"{rel!r} is outside the pipelines folder")
        _write(dest, text)
        if rel.startswith("scripts/") or text.startswith("#!"):
            dest.chmod(0o755)
    _write(target, yaml_text)
    return target


# -- one turn (shared by the editor API and chat links) --------------------------------------
CHAT_TIMEOUT = 300.0


class AssistantError(Exception):
    """A turn that couldn't run: `status` is the HTTP status the API answers with."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class ChatWorkspace:
    """The folder a claude-code Chat turn runs in: `<home>/assistant/workspace`, with every
    skill installed in `.claude/skills/` so Claude Code can invoke them (its Skill tool)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    async def exec(
        self,
        cmd: list[str],
        *,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        input: str | None = None,
    ) -> tuple[int, str, str]:
        from agentline.runners.common import base_env, run_process

        return await run_process(
            cmd, cwd=self.path, env={**base_env(), **(env or {})}, input=input, timeout=timeout
        )


def chat_workspace(lib: SkillLibrary) -> ChatWorkspace:
    from agentline.skills import install_into

    root = attachments_dir().parent / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    installed = root / ".claude" / "skills"
    if installed.is_dir():  # skills deleted since the last turn go too
        shutil.rmtree(installed)
    install_into(root, lib.list())
    return ChatWorkspace(root)


def skills_for_chat(kind: str, instructions: str, lib: SkillLibrary) -> tuple[str, Any, tuple]:
    """(the USING SKILLS note, the workspace, allowed tools) for a harness kind. Claude Code gets
    the whole library as real skills; other harnesses get the skills the instructions name in
    full in the prompt (they can't load skill folders)."""
    from agentline import chat_instructions
    from agentline.skills import prompt_section

    if kind == "builtin":  # the built-in harness has a read_skill tool (chat_harness.py)
        note = (
            "When a skill in SKILLS applies (the CHAT INSTRUCTIONS may say when), read it with"
            " your read_skill tool and follow it."
        )
        return note, None, ("read",)
    if kind == "claude-code":
        note = (
            "Every skill in SKILLS is installed for you. When one applies (the CHAT INSTRUCTIONS"
            " may say when), invoke it with your Skill tool and follow it, then reply with the"
            " JSON object as usual."
        )
        return note, chat_workspace(lib), ("read", "Skill")
    named = [x for x in lib.list() if x.name in chat_instructions.skill_names(instructions)]
    note = (
        "You can't load skill folders, so the skills the CHAT INSTRUCTIONS name are written out"
        " below: follow one when it applies. Others in SKILLS can be put on pipeline nodes."
    )
    if not named:
        note = "Skills in SKILLS can be put on pipeline nodes (`skills:`)."
    return note + prompt_section(named), None, ("read",)


async def _ask(
    files: Any,
    harness: str,
    model: str | None,
    prompt: str,
    tools: tuple[str, ...] = ("read",),
    workspace: Any = None,
) -> tuple[Any, float]:
    from agentline.errors import AgentlineError
    from agentline.registry import Limits, RunRequest
    from agentline.runners.common import RunnerError

    try:
        res = ai.resolve(harness, model, files.config)
        runner = files.registry.get_runner(res.runner)
    except ValidationError as e:
        raise AssistantError(422, f"harness {harness!r} can't run: {e}") from None
    request = RunRequest(
        prompt=prompt,
        inputs={},
        limits=Limits(allowed_tools=tools, max_turns=4, timeout=CHAT_TIMEOUT),
        workspace=workspace,
        params=dict(res.params),
        node_id="assistant",
    )
    try:
        result = await runner.run(request)
    except (AgentlineError, RunnerError, OSError) as e:
        raise AssistantError(502, f"{harness} failed: {e}") from None
    return result.output, result.cost_usd


async def transcribe_images(files: Any, messages: list[dict[str, Any]]) -> dict[str, Any]:
    """{attachment id: transcript} for every image in the conversation (cached per image)."""
    from agentline import vision
    from agentline.errors import AgentlineError
    from agentline.runners.common import RunnerError

    out: dict[str, dict[str, Any]] = {}
    for m in messages:
        for a in m.get("attachments") or []:
            if str(a) in out:
                continue
            try:
                out[str(a)] = await vision.transcribe(files, str(a))
            except ValidationError as e:
                raise AssistantError(422, str(e)) from None
            except (AgentlineError, RunnerError, OSError) as e:
                raise AssistantError(502, f"reading the image failed: {e}") from None
    return out


async def converse(
    files: Any,
    messages: list[dict[str, Any]],
    *,
    harness: str | None = None,
    model: str | None = None,
    pipeline: str | None = None,
    source: str | None = None,
    live: Live | None = None,
    app: Any = None,
    incidents: Any = None,
) -> dict[str, Any]:
    """One assistant turn: {message, choices, actions (checked), harness, model, cost_usd,
    repaired, steps, report}. `files` is the editor's PipelineFiles; `live` what exists right
    now; `app` and `incidents` are for the built-in harness (run lookups, App reports)."""
    from agentline import setup
    from agentline.api.editor import node_types

    if not messages or messages[-1].get("role") != "user":
        raise AssistantError(422, "the last message must be the user's")
    saved = (setup.read_state().get("assistant") or {}) if not harness else {}
    harness = ai.harness_name(harness, files.config) or saved.get("harness") or ai.FALLBACK_HARNESS
    model = model or (saved.get("model") if saved.get("harness") == harness else None)
    current = None
    if pipeline:
        path = files.resolve(pipeline)
        text = source
        if text is None and path.exists():
            text = path.read_text(encoding="utf-8")
        if text is not None:
            current = (files.name_of(path) + ".yaml", text)
    context = build_context(
        files.config,
        node_types(files.registry, files.config),
        SkillLibrary(),
        [files.name_of(p) + ".yaml" for p in files.files()],
        current,
        live,
    )
    from agentline import chat_instructions

    try:
        kind = ai.resolve(harness, model, files.config).kind.name
    except ValidationError as e:
        raise AssistantError(422, f"harness {harness!r} can't run: {e}") from None
    has_images = any(m.get("attachments") for m in messages)
    transcripts: dict[str, dict[str, Any]] = {}
    if has_images and kind != "claude-code":  # it can't see images: a vision model transcribes
        transcripts = await transcribe_images(files, messages)
    text = chat_instructions.read()
    note, workspace, tools = skills_for_chat(kind, text, SkillLibrary())
    instructions = instructions_text(text, note)

    if kind == "builtin":
        return await _builtin_turn(
            files, harness, model, context, instructions, messages, live, app, incidents,
            transcripts,
        )  # fmt: skip
    staged = stage_attachments(messages, Path(workspace.path)) if workspace is not None else None

    async def ask(msgs: list[dict[str, Any]]) -> tuple[Any, float]:
        prompt = build_prompt(context, msgs, instructions, staged, transcripts)
        return await _ask(files, harness, model, prompt, tools, workspace)

    output, cost = await ask(messages)
    cost += sum(float(t.get("cost_usd") or 0) for t in transcripts.values() if not t["cached"])
    reply = parse_reply(output)
    actions = check_actions(reply["actions"], files.root, files.registry, files.config, live)
    repaired = False
    note = repair_note(actions)
    if note:  # one round: show the agent what the validator said
        retry = [
            *messages,
            {"role": "assistant", "content": json_text(reply)},
            {"role": "user", "content": note},
        ]
        output, more = await ask(retry)
        cost += more
        again = parse_reply(output)
        if again["actions"]:
            reply = again
            actions = check_actions(
                again["actions"], files.root, files.registry, files.config, live
            )
            repaired = True
    return {
        "message": reply["message"],
        "choices": reply["choices"],
        "actions": actions,
        "harness": harness,
        "model": model,
        "cost_usd": cost,
        "repaired": repaired,
        "transcripts": [
            {k: t.get(k) for k in ("id", "text", "by", "edited", "cached")}
            for t in transcripts.values()
        ],
    }


async def _builtin_turn(
    files: Any,
    harness: str,
    model: str | None,
    context: str,
    instructions: str,
    messages: list[dict[str, Any]],
    live: Live | None,
    app: Any,
    incidents: Any,
    transcripts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """A turn on the built-in harness (chat_harness.py): a tool loop instead of one call."""
    from agentline import chat_harness

    res = ai.resolve(harness, model, files.config)
    try:
        runner = files.registry.get_runner(res.runner)
    except Exception:
        runner = None
    try:
        out = await chat_harness.run_turn(
            files,
            res,
            system_text(context, instructions),
            messages,
            check=lambda acts: check_actions(acts, files.root, files.registry, files.config, live),
            app=app,
            live=live,
            incidents=incidents,
            transcripts=transcripts,
            pricing=dict(getattr(runner, "pricing", None) or {}),
        )
    except chat_harness.HarnessError as e:
        raise AssistantError(502, f"{harness} failed: {e}") from None
    cost = out["cost_usd"] + sum(
        float(t.get("cost_usd") or 0) for t in transcripts.values() if not t["cached"]
    )
    reply = out["reply"]
    return {
        "message": reply["message"],
        "choices": reply["choices"],
        "actions": reply["actions"],
        "harness": harness,
        "model": model,
        "cost_usd": cost,
        "repaired": False,
        "steps": out["steps"],
        "report": out["report"],
        "transcripts": [
            {k: t.get(k) for k in ("id", "text", "by", "edited", "cached")}
            for t in transcripts.values()
        ],
    }


def apply_setting(files: Any, section: str, name: str, body: dict[str, Any]) -> None:
    """Write one settings table the way the Settings API does (for chat links)."""
    import tomllib

    from agentline.builtins import default_registry
    from agentline.tomlwrite import set_table, write_file

    issues = check_setting({"section": section, "name": name, "body": body}, files.config)
    if issues:
        raise ValidationError(issues[0])
    cfg = files.config or Config()
    import os

    path = (
        Path(cfg.path) if cfg.path else Path(os.environ.get("AGENTLINE_CONFIG") or "agentline.toml")
    )
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    table = {k: v for k, v in body.items() if v not in (None, "", [], {})}
    new = set_table(text, section, name, table)
    Config.from_dict(tomllib.loads(new), path)
    write_file(path, new)
    files.config = Config.load(path)
    files.registry = default_registry(files.config)
    from agentline import restart

    restart.note(f"{section}.{name} changed")
