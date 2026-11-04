"""`agentline` command line (argparse). See docs/phase-6.md for the reference.

Global options work before or after the subcommand: `--db`, `--config`, `--pipelines`,
`--log-format`, `--log-level`. Test fakes can replace the registry with the hidden
`--registry module:function` option or `$AGENTLINE_REGISTRY` (a function `(config) ->
Registry`).
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

from agentline.cli import commands as c
from agentline.cli.env import CliEnv
from agentline.errors import AgentlineError
from agentline.logs import setup_logging


def _globals(p: argparse.ArgumentParser, *, sub: bool) -> None:
    d = argparse.SUPPRESS if sub else None
    p.add_argument(
        "--db",
        default=d,
        help="ledger file (default $AGENTLINE_DB or "
        "$AGENTLINE_HOME/agentline.db, AGENTLINE_HOME defaults to ~/.agentline)",
    )
    p.add_argument(
        "--config", default=d, help="agentline.toml (default $AGENTLINE_CONFIG or ./agentline.toml)"
    )
    p.add_argument(
        "--pipelines",
        metavar="DIR",
        default=d,
        help="pipelines directory (default pipelines_dir in config, else ./pipelines)",
    )
    p.add_argument(
        "--log-format", choices=("text", "json"), default=argparse.SUPPRESS if sub else "text"
    )
    p.add_argument("--log-level", default=argparse.SUPPRESS if sub else "INFO")
    p.add_argument("--registry", default=d, help=argparse.SUPPRESS)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    _globals(common, sub=True)
    parser = argparse.ArgumentParser(
        prog="agentline", description="Durable multi-agent delivery pipelines."
    )
    _globals(parser, sub=False)
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    def add(
        group: argparse._SubParsersAction, name: str, fn: object, help: str
    ) -> argparse.ArgumentParser:  # type: ignore[type-arg]
        p = group.add_parser(name, help=help, description=help, parents=[common])
        p.set_defaults(func=fn)
        return p

    p = add(sub, "validate", c.cmd_validate, "check pipeline files; print every issue")
    p.add_argument("files", nargs="+", metavar="pipeline.yaml")
    p.add_argument("-q", "--quiet", action="store_true", help="print only issues")

    p = add(sub, "run", c.cmd_run, "submit a run (optionally work it until it parks)")
    p.add_argument("pipeline", help="pipeline name (from --pipelines) or a YAML path")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--payload", help="trigger payload as JSON")
    g.add_argument("--payload-file", help="file with the trigger payload as JSON")
    p.add_argument("--dedupe-key")
    p.add_argument(
        "--wait",
        action="store_true",
        help="run an in-process worker until the run is done, dead, waiting or needs review",
    )
    p.add_argument(
        "--timeout", type=float, default=None, help="seconds to --wait (default: no limit)"
    )
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--lease-ttl", type=float, default=30.0)

    p = add(
        sub, "worker", c.cmd_worker, "run a worker, the poller and schedules until SIGINT/SIGTERM"
    )
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--lease-ttl", type=float, default=30.0)
    p.add_argument(
        "--until-idle",
        action="store_true",
        help="exit when nothing is ready, running or backing off",
    )

    p = add(
        sub, "serve", c.cmd_serve, "worker + poller + schedules + webhook HTTP API in one process"
    )
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--lease-ttl", type=float, default=30.0)

    runs = add(sub, "runs", None, "list, show, retry or cancel runs")
    rs = runs.add_subparsers(dest="runs_command", required=True, metavar="subcommand")
    p = add(rs, "list", c.cmd_runs_list, "list runs, newest first")
    p.add_argument("--status")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true")
    p = add(rs, "show", c.cmd_runs_show, "everything about one run, plus what to do next")
    p.add_argument("run")
    p.add_argument("--json", action="store_true")
    p = add(rs, "retry", c.cmd_runs_retry, "give dead executions a fresh set of attempts")
    p.add_argument("run")
    p.add_argument("--node", help="only this node (dead or needs-review); keeps finished tasks")
    p = add(rs, "cancel", c.cmd_runs_cancel, "cancel a run")
    p.add_argument("run")

    p = add(
        sub,
        "approve",
        c.cmd_approve,
        "approve a waiting gate, or apply an image proposal (--proposal)",
    )
    p.add_argument("run")
    p.add_argument("--node")
    p.add_argument("--comment")
    p.add_argument(
        "--proposal",
        metavar="ID",
        help="apply this image proposal to agentline.toml, build, retry the node",
    )

    p = add(sub, "reject", c.cmd_reject, "reject a waiting gate (the run goes dead)")
    p.add_argument("run")
    p.add_argument("--node")
    p.add_argument("--comment")

    p = add(sub, "resolve", c.cmd_resolve, "decide a needs-review execution")
    p.add_argument("run")
    p.add_argument("--node")
    p.add_argument("--action", required=True, choices=("retry", "skip", "fail", "done"))
    p.add_argument("--result", help="JSON result for --action done")
    p.add_argument(
        "--effect",
        choices=("redo", "done"),
        help="for an unknown effect: do it again, or record it as done",
    )
    p.add_argument("--effect-result", help="JSON result of the effect for --effect done")

    p = add(sub, "mirror", c.cmd_mirror, "create or fetch a repo's bare mirror")
    p.add_argument("repo")

    p = add(sub, "schedules", c.cmd_schedules, "list schedule pipelines and next fire times")
    p.add_argument("--count", type=int, default=3, help="fire times per pipeline (default 3)")
    p.add_argument("--json", action="store_true")

    p = add(sub, "templates", c.cmd_templates, "list the pipeline templates for 'new'")
    p.add_argument("--json", action="store_true")

    p = add(sub, "new", c.cmd_new, "create a pipeline from a template")
    p.add_argument("template", help="template name (see 'agentline templates')")
    p.add_argument("dest", metavar="dest_dir", help="directory to write <name>.yaml and files to")
    p.add_argument("--name", help="pipeline name (default: the template's name)")
    p.add_argument("--force", action="store_true", help="overwrite existing files")

    p = add(sub, "incidents", c.cmd_incidents, "crash and failure reports (stored locally)")
    p.add_argument("action", choices=["list", "show", "seen", "rm", "clear", "export"])
    p.add_argument("target", nargs="?", help="incident id (show/seen/rm) or a folder (export)")
    p.add_argument("--json", action="store_true")

    p = add(sub, "triage", c.cmd_triage, "failure triage verdicts and their proposals")
    p.add_argument("action", choices=["list", "show"])
    p.add_argument("target", nargs="?", help="triage id (show)")
    p.add_argument("--run", help="list: only this run's")
    p.add_argument("--json", action="store_true")

    p = add(sub, "review", c.cmd_review, "run reviews: what to change in a pipeline, and probes")
    p.add_argument("action", choices=["list", "show", "run"])
    p.add_argument("target", nargs="?", help="review id (show) or run id (run)")
    p.add_argument("--run", help="list: only this run's")
    p.add_argument("--pipeline", help="list: only this pipeline's (its name:)")
    p.add_argument("--json", action="store_true")

    p = add(sub, "lessons", c.cmd_lessons, "a pipeline's lesson library and adopted edits")
    p.add_argument("pipeline", help="pipeline file name (as in the editor, without .yaml)")
    p.add_argument("--json", action="store_true")

    p = add(sub, "issues", c.cmd_issues, "one tracked issue per problem, with a status")
    p.add_argument("action", choices=["list", "show", "set", "note"])
    p.add_argument("number", nargs="?", type=int, help="issue number (show/set/note)")
    p.add_argument("value", nargs="?", help="set: open|in_progress|fixed|wont_fix; note: text")
    p.add_argument("--note", help="set: a note recorded with the status change")
    p.add_argument("--all", action="store_true", help="list: closed issues too")
    p.add_argument("--json", action="store_true")

    p = add(sub, "export", c.cmd_export, "export a pipeline (and its files) as a .zip")
    p.add_argument("name", help="pipeline name in the pipelines folder, e.g. review/pipeline")
    p.add_argument("-o", "--output", help="output file (default: <folder>.agentline.zip)")

    p = add(sub, "import", c.cmd_import, "import a pipeline .zip into the pipelines folder")
    p.add_argument("file")
    p.add_argument("--folder", help="folder to import into (default: the bundle's)")
    p.add_argument("--force", action="store_true", help="replace an existing folder")
    p.add_argument(
        "--with-settings", action="store_true", help="add the bundle's settings not set up here"
    )

    p = add(sub, "skills", c.cmd_skills, "agent skills library (addyosmani/agent-skills)")
    p.add_argument("action", choices=["list", "fetch", "show", "search", "add", "update", "remove"])
    p.add_argument(
        "name", nargs="?",
        help="skill name (show, remove, update), search words (search), or a source (add: "
        "owner/repo, owner/repo@skill, a github.com URL)",
    )  # fmt: skip
    p.add_argument("--skill", action="append", help="add: only these skills (repeatable)")
    p.add_argument("--json", action="store_true")

    p = add(sub, "keys", c.cmd_keys, "API keys in the local secrets file (never shown)")
    p.add_argument("action", choices=["list", "set", "rm"])
    p.add_argument("name", nargs="?", help="env var name, e.g. OPENROUTER_API_KEY")
    p.add_argument("--stdin", action="store_true", help="read the value from stdin, not a prompt")

    users = add(sub, "users", None, "people who use the API with their own token (spec 007)")
    us = users.add_subparsers(dest="users_command", required=True, metavar="subcommand")
    p = add(us, "add", c.cmd_users_add, "add a person; prints their token once")
    p.add_argument("name")
    p.add_argument("--admin", action="store_true", help="may add and revoke people")
    p = add(us, "list", c.cmd_users_list, "list people (names only, never tokens)")
    p.add_argument("--json", action="store_true")
    p = add(us, "token", c.cmd_users_token, "issue a new token (the old one stops working)")
    p.add_argument("name")
    p = add(us, "admin", c.cmd_users_admin, "mark a person admin or not")
    p.add_argument("name")
    p.add_argument("state", choices=("on", "off"))
    p = add(us, "revoke", c.cmd_users_revoke, "remove a person; their token stops working")
    p.add_argument("name")

    p = add(sub, "audit", c.cmd_audit, "show the audit log")
    p.add_argument("--run")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    env = CliEnv(args)
    root = logging.getLogger()
    old_level = root.level
    handler = setup_logging(args.log_format, args.log_level, redactor=env.redactor)
    try:
        return int(args.func(env, args))
    except (AgentlineError, PermissionError, FileNotFoundError) as e:
        env.err(f"error: {e}")
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        env.close()
        root.removeHandler(handler)
        root.setLevel(old_level)


__all__ = ["build_parser", "main"]
