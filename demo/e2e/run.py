"""End-to-end acceptance for v0.1: the full ticket-to-eod pipeline under chaos.

    uv sync --extra server
    uv run python -m demo.e2e.run --tickets 3 --seed 1 --expect-clean

What happens, with real processes and real git:
1. Fake Jira/GitHub run in their own process (`demo.e2e.services`); a bare git repo plays the
   GitHub remote; a script plays the EOD environment (`demo.e2e.deploy`).
2. `agentline serve` (worker + poller + webhooks) runs the pipeline. The driver SIGKILLs it at
   random points and restarts it.
3. The driver plays everyone else: Jira sends a signed `issue_assigned` webhook per ticket
   (twice: duplicate delivery), GitHub sends review webhooks (changes requested on the first
   review, approved on the second), a release manager approves the gate through the CLI.
   Webhooks that hit a dead server are redelivered with the same delivery id, like GitHub does.
4. At the end it checks: exactly one PR per ticket, the iteration-2 commit pushed, exactly one
   deploy per ticket of that commit, tests ran, every run done, and a complete audit trail.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import random
import shlex
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentline import App
from agentline.ledger import SqliteLedger

REPO_ROOT = Path(__file__).resolve().parents[2]
GH_SECRET = "e2e-github-webhook-secret"
JIRA_SECRET = "e2e-jira-webhook-secret"
GITHUB_TOKEN = "ghp_faketoken_0123456789"  # matches tests/actions/fakes.py
JIRA_TOKEN = "jira-fake-token-abcdef"
JIRA_EMAIL = "bot@example.com"
APPROVER = "release-manager"
REPO = "acme/app"

PIPELINE = """\
# The ticket-to-eod pipeline with scripted agents (runner: python) and a local git workspace.
version: 1
name: ticket-to-eod
trigger: { type: webhook, source: jira, event: issue_assigned }
budget_usd: 5.0
nodes:
  read_ticket:
    type: action
    uses: jira.get_ticket
    with: { key: "{{payload.issue_key}}" }
    out: ticket
  plan:
    type: agent
    runner: python
    prompt: "Plan {{ticket.key}}: {{ticket.summary}}"
    with: { function: "demo.e2e.agent:plan" }
    in: [ticket]
    out: plan
  implement:
    type: agent
    runner: python
    prompt: >-
      Implement {{ticket.key}}: {{plan.summary}}
      {{#review?}}Address: {{review.comments}}{{/review?}}
    with: { function: "demo.e2e.agent:implement" }
    in: [ticket, plan, review?]
    out: branch
    allowed_tools: [read, edit]
    workspace: { type: local }
  open_pr:
    type: action
    uses: github.open_pr
    in: [ticket, plan, branch]
    out: pr
    with: { title: "{{ticket.key}}: {{plan.summary}}" }
  await_review: { type: wait, for: github.review, in: [pr], out: review, poll_every: 3s }
  route: { type: router, switch: review.state }
  approve:
    type: gate
    in: [pr, branch]
    prompt: "Deploy {{branch.name}} (PR #{{pr.number}}) to EOD?"
  deploy: { type: action, uses: env.deploy, with: { env: eod }, in: [branch], out: deployment }
  test: { type: action, uses: env.test, with: { env: eod }, in: [deployment], out: test_report }
edges:
  - read_ticket -> plan -> implement -> open_pr -> await_review -> route
  - { from: route, to: implement, when: changes_requested, max_loops: 3 }
  - { from: route, to: approve, when: approved }
  - approve -> deploy -> test
"""


def _git(*args: str, cwd: Path | None = None) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "e2e",
        "GIT_AUTHOR_EMAIL": "e2e@example.com",
        "GIT_COMMITTER_NAME": "e2e",
        "GIT_COMMITTER_EMAIL": "e2e@example.com",
    }
    out = subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _http(
    method: str,
    url: str,
    body: Any = None,
    headers: dict[str, str] | None = None,
    raw: bytes | None = None,
    timeout: float = 5.0,
) -> tuple[int, Any]:
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read()
            return resp.status, json.loads(text) if text else None
    except urllib.error.HTTPError as e:
        text = e.read()
        try:
            return e.code, json.loads(text) if text else None
        except ValueError:
            return e.code, text.decode(errors="replace")


@dataclass
class Delivery:
    """A webhook the driver keeps redelivering (same id) until the server accepts it."""

    path: str
    body: bytes
    headers: dict[str, str]
    attempts: int = 0
    status: int | None = None


@dataclass
class World:
    workdir: Path
    services: subprocess.Popen[bytes] | None = None
    services_url: str = ""
    serve: subprocess.Popen[bytes] | None = None
    port: int = 0
    env: dict[str, str] = field(default_factory=dict)
    cli: list[str] = field(default_factory=list)
    pending: list[Delivery] = field(default_factory=list)
    handled_waits: set[str] = field(default_factory=set)
    approved_gates: set[str] = field(default_factory=set)
    kills: int = 0
    redeliveries: int = 0
    restarts: int = 0
    webhook_posts: int = 0

    @property
    def state_dir(self) -> Path:
        return self.workdir / "eod"


def setup(workdir: Path, tickets: list[str]) -> World:
    w = World(workdir)
    workdir.mkdir(parents=True)
    w.state_dir.mkdir()
    # the "GitHub" git remote
    origin, seed = workdir / "origin.git", workdir / "seed"
    _git("init", "-q", "--bare", "-b", "main", str(origin))
    _git("init", "-q", "-b", "main", str(seed))
    (seed / "README.md").write_text("# app\n")
    (seed / "src").mkdir()
    (seed / "src" / "__init__.py").write_text("")
    _git("add", "-A", cwd=seed)
    _git("commit", "-q", "-m", "init", cwd=seed)
    _git("push", "-q", str(origin), "main", cwd=seed)
    # pipeline + config
    pipelines = workdir / "pipelines"
    pipelines.mkdir()
    (pipelines / "ticket-to-eod.yaml").write_text(PIPELINE)
    py, state = shlex.quote(sys.executable), shlex.quote(str(w.state_dir))
    (workdir / "agentline.toml").write_text(
        f'pipelines_dir = "{pipelines}"\n\n'
        f'[repos.app]\nremote = "{origin}"\ngithub = "{REPO}"\ndefault_branch = "main"\n\n'
        f"[environments.eod]\n"
        f'deploy = "{py} -m demo.e2e.deploy deploy {state} {{branch}} {{sha}}"\n'
        f'check = "{py} -m demo.e2e.deploy check {state} {{sha}}"\n'
        f'test = "{py} -m demo.e2e.deploy test {state}"\n'
        f'cwd = "{REPO_ROOT}"\n'
        f"deploy_class = true\n"
    )
    # fake services
    port_file = workdir / "services.port"
    w.services = subprocess.Popen(
        [sys.executable, "-m", "demo.e2e.services", "--port-file", str(port_file)], cwd=REPO_ROOT
    )
    for _ in range(300):
        if port_file.exists() and port_file.read_text():
            break
        time.sleep(0.05)
    w.services_url = f"http://127.0.0.1:{port_file.read_text()}"
    for t in tickets:
        _http("POST", f"{w.services_url}/_control/issue", {"key": t})
    w.port = _free_port()
    w.env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "AGENTLINE_HOME": str(workdir / "home"),
        "AGENTLINE_DB": str(workdir / "agentline.db"),
        "AGENTLINE_CONFIG": str(workdir / "agentline.toml"),
        "GITHUB_API_URL": w.services_url,
        "GITHUB_TOKEN": GITHUB_TOKEN,
        "JIRA_BASE_URL": w.services_url,
        "JIRA_EMAIL": JIRA_EMAIL,
        "JIRA_API_TOKEN": JIRA_TOKEN,
        "GITHUB_WEBHOOK_SECRET": GH_SECRET,
        "JIRA_WEBHOOK_SECRET": JIRA_SECRET,
        "AGENTLINE_TRIAGE": "off",  # failure triage would call a real agent
        "AGENTLINE_REVIEW": "off",  # and so would run reviews
        "AGENTLINE_RUNCHAT": "off",  # and chat start nodes' answers
    }
    w.cli = [sys.executable, "-m", "agentline.cli"]
    return w


def start_serve(w: World) -> None:
    log = (w.workdir / "serve.log").open("ab")
    w.serve = subprocess.Popen(
        [
            *w.cli,
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(w.port),
            "--concurrency",
            "4",
            "--lease-ttl",
            "1.5",
        ],
        cwd=REPO_ROOT,
        env=w.env,
        stdout=log,
        stderr=log,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if _http("GET", f"http://127.0.0.1:{w.port}/healthz", timeout=1)[0] == 200:
                return
        except OSError:
            pass
        if w.serve.poll() is not None:
            raise RuntimeError(f"agentline serve exited: see {w.workdir / 'serve.log'}")
        time.sleep(0.1)
    raise RuntimeError("agentline serve did not become healthy")


def kill_serve(w: World) -> None:
    if w.serve and w.serve.poll() is None:
        w.serve.send_signal(signal.SIGKILL)
        w.serve.wait()
        w.kills += 1


# -- webhooks ---------------------------------------------------------------------------------
def queue_jira_assignment(w: World, ticket: str, changelog_id: str) -> None:
    event = {
        "webhookEvent": "jira:issue_updated",
        "issue_event_type_name": "issue_updated",
        "timestamp": 1700000000000,
        "issue": {
            "key": ticket,
            "fields": {
                "assignee": {"accountId": "acc-1", "emailAddress": "dev@example.com"},
                "project": {"key": ticket.split("-")[0]},
            },
        },
        "changelog": {"id": changelog_id, "items": [{"field": "assignee", "to": "acc-1"}]},
    }
    w.pending.append(
        Delivery("/webhooks/jira", json.dumps(event).encode(), {"X-Agentline-Secret": JIRA_SECRET})
    )


def queue_review(
    w: World, number: int, state: str, commit_id: str, delivery: str, review: dict[str, Any]
) -> None:
    payload = {
        "action": "submitted",
        "review": {**review, "state": state, "commit_id": commit_id},
        "pull_request": {"number": number},
        "repository": {"full_name": REPO},
    }
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(GH_SECRET.encode(), body, hashlib.sha256).hexdigest()
    w.pending.append(
        Delivery(
            "/webhooks/github",
            body,
            {
                "X-Hub-Signature-256": sig,
                "X-GitHub-Event": "pull_request_review",
                "X-GitHub-Delivery": delivery,
            },
        )
    )


def flush_deliveries(w: World) -> None:
    still = []
    for d in w.pending:
        try:
            if d.attempts:
                w.redeliveries += 1
            d.attempts += 1
            w.webhook_posts += 1
            if w.serve is None:
                raise ConnectionRefusedError
            status, _ = _http(
                "POST",
                f"http://127.0.0.1:{w.port}{d.path}",
                raw=d.body,
                headers=d.headers,
                timeout=3,
            )
        except OSError:
            still.append(d)  # server down: redeliver later with the same delivery id
            continue
        d.status = status
        if status >= 500:
            still.append(d)
        elif status not in (200, 202, 409):
            raise RuntimeError(f"webhook {d.path} rejected with {status}")
    w.pending = still


# -- the humans -------------------------------------------------------------------------------
def act(w: World, app: App) -> None:
    """Review PRs and approve gates for whatever is waiting."""
    with app.ledger.tx() as tx:
        waiting = [
            x
            for r in tx.list_runs(limit=10_000)
            for x in tx.list_executions(r.id)
            if x.status == "waiting"
        ]
    for ex in waiting:
        key = ex.correlation_key or ""
        if ex.detail.get("kind") == "gate" and ex.id not in w.approved_gates:
            out = subprocess.run(
                [*w.cli, "approve", ex.run_id, "--node", ex.node_id, "--comment", "ship it"],
                cwd=REPO_ROOT,
                env={**w.env, "AGENTLINE_USER": APPROVER},
                capture_output=True,
                text=True,
            )
            if out.returncode == 0:
                w.approved_gates.add(ex.id)
        elif key.startswith("github:pr:") and ex.id not in w.handled_waits:
            number = int(key.rsplit("#", 1)[1])
            sha = (app.result(ex.run_id, "implement") or {}).get("sha", "")
            state = "changes_requested" if ex.iteration == 1 else "approved"
            body = "Please reject empty passwords and add a test." if ex.iteration == 1 else "LGTM"
            _, review = _http(
                "POST",
                f"{w.services_url}/_control/review",
                {
                    "repo": REPO,
                    "number": number,
                    "state": state,
                    "commit_id": sha,
                    "body": body,
                },
            )
            queue_review(w, number, state, sha, f"review-{ex.id}", review)
            w.handled_waits.add(ex.id)


# -- verification -----------------------------------------------------------------------------
def verify(w: World, app: App, tickets: list[str]) -> dict[str, Any]:
    _, state = _http("GET", f"{w.services_url}/_control/state")
    with app.ledger.tx() as tx:
        runs = tx.list_runs(limit=10_000)
        execs = {r.id: tx.list_executions(r.id) for r in runs}
        audit = {r.id: tx.list_audit(r.id) for r in runs}
    deploys = (
        [json.loads(x) for x in (w.state_dir / "deploys.jsonl").read_text().splitlines()]
        if (w.state_dir / "deploys.jsonl").exists()
        else []
    )
    tests_run = (
        len((w.state_dir / "tests.jsonl").read_text().splitlines())
        if (w.state_dir / "tests.jsonl").exists()
        else 0
    )
    origin = w.workdir / "origin.git"
    prs = state["prs"].get(REPO, [])
    pr_heads = Counter(p["head"]["ref"] for p in prs)
    deploys_by_branch = Counter(d["branch"] for d in deploys)
    problems: list[str] = []
    per_run: list[dict[str, Any]] = []

    by_ticket = Counter(r.payload.get("issue_key") for r in runs)
    for t in tickets:
        if by_ticket[t] != 1:
            problems.append(f"{t}: {by_ticket[t]} runs (expected exactly 1)")
    for r in runs:
        branch_art = app.result(r.id, "implement") or {}
        branch, sha = branch_art.get("name"), branch_art.get("sha")
        keys = {x.key: x.status for x in execs[r.id]}
        approvals = [a for a in audit[r.id] if a.action == "approve"]
        deploy_rows = [a for a in audit[r.id] if a.action == "deploy"]
        try:
            pushed = _git("rev-parse", f"refs/heads/{branch}", cwd=origin) if branch else None
        except subprocess.CalledProcessError:
            pushed = None
        mine = [d for d in deploys if d["branch"] == branch]
        row = {
            "ticket": r.payload.get("issue_key"),
            "run": r.id,
            "status": r.status,
            "branch": branch,
            "prs": pr_heads.get(branch or "", 0),
            "implement_iterations": sorted(
                x.iteration for x in execs[r.id] if x.node_id == "implement" and x.status == "done"
            ),
            "pushed_sha_matches": pushed == sha,
            "deploys": deploys_by_branch.get(branch or "", 0),
            "deployed_sha_matches": bool(mine) and all(d["sha"] == sha for d in mine),
            "approved_by": sorted({a.actor for a in approvals}),
            "deploy_audit": [
                {k: a.detail.get(k) for k in ("sha", "approved_by", "env", "exit_code")}
                for a in deploy_rows
            ],
            "cost_usd": round(r.cost_usd, 4),
        }
        per_run.append(row)
        t = row["ticket"]
        if r.status != "done":
            problems.append(f"{t}: run {r.status} ({keys})")
        if row["prs"] != 1:
            problems.append(f"{t}: {row['prs']} PRs")
        if row["implement_iterations"] != [1, 2]:
            problems.append(f"{t}: implement iterations {row['implement_iterations']}")
        if not row["pushed_sha_matches"]:
            problems.append(f"{t}: pushed {pushed} != implemented {sha}")
        if row["deploys"] != 1:
            problems.append(f"{t}: {row['deploys']} deploys")
        if not row["deployed_sha_matches"]:
            problems.append(f"{t}: deployed sha differs from iteration-2 sha")
        if row["approved_by"] != [APPROVER]:
            problems.append(f"{t}: approvals by {row['approved_by']}")
        if (
            len(deploy_rows) != 1
            or deploy_rows[0].detail.get("approved_by") != APPROVER
            or deploy_rows[0].detail.get("sha") != sha
        ):
            problems.append(f"{t}: deploy audit {row['deploy_audit']}")
    if state["pr_creates"] != len(tickets):
        problems.append(f"GitHub saw {state['pr_creates']} PR creations for {len(tickets)} tickets")
    if tests_run < len(tickets):
        problems.append(f"tests ran {tests_run} times for {len(tickets)} tickets")
    return {
        "tickets": len(tickets),
        "runs": len(runs),
        "kills": w.kills,
        "restarts": w.restarts,
        "webhook_posts": w.webhook_posts,
        "webhook_redeliveries": w.redeliveries,
        "pr_creates": state["pr_creates"],
        "deploys": len(deploys),
        "tests_run": tests_run,
        "reviews": len(state["reviews"]),
        "per_run": per_run,
        "problems": problems,
        "clean": not problems,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    rng = random.Random(args.seed)
    tickets = [f"E2E-{i}" for i in range(1, args.tickets + 1)]
    workdir = (args.out / f"e2e-seed{args.seed}-{int(time.time())}").resolve()
    w = setup(workdir, tickets)
    app: App | None = None
    try:
        start_serve(w)
        app = App(SqliteLedger(workdir / "agentline.db"))
        for t in tickets:  # every assignment delivered twice: the dedupe key must absorb it
            queue_jira_assignment(w, t, changelog_id=f"cl-{t}")
            queue_jira_assignment(w, t, changelog_id=f"cl-{t}")
        next_kill = time.monotonic() + rng.uniform(args.kill_min, args.kill_max)
        down_until = 0.0
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            flush_deliveries(w)
            act(w, app)
            with app.ledger.tx() as tx:
                runs = tx.list_runs(limit=10_000)
            terminal = ("done", "dead", "cancelled")
            if (
                len(runs) == len(tickets)
                and all(r.status in terminal for r in runs)
                and not w.pending
            ):
                break
            now = time.monotonic()
            if w.serve is None and now >= down_until:
                start_serve(w)
                w.restarts += 1
                next_kill = time.monotonic() + rng.uniform(args.kill_min, args.kill_max)
            elif w.serve is not None and now >= next_kill and w.kills < args.max_kills:
                kill_serve(w)
                w.serve = None  # stays down a while: webhooks fail and get redelivered
                down_until = now + rng.uniform(0.0, args.max_downtime)
            time.sleep(0.1)
        report = verify(w, app, tickets)
        report["workdir"] = str(workdir)
        report["elapsed_s"] = round(args.timeout - (deadline - time.monotonic()), 1)
        (workdir / "report.json").write_text(json.dumps(report, indent=2))
        return report
    finally:
        kill_serve(w)
        if w.services and w.services.poll() is None:
            w.services.kill()
            w.services.wait()
        if app is not None:
            app.ledger.close()


def format_report(r: dict[str, Any]) -> str:
    lines = [
        f"e2e report: tickets={r['tickets']} runs={r['runs']} kills={r['kills']} "
        f"elapsed={r['elapsed_s']}s",
        f"  webhook posts / redeliveries        {r['webhook_posts']} / {r['webhook_redeliveries']}",
        f"  PR creations at GitHub              {r['pr_creates']}",
        f"  reviews submitted                   {r['reviews']}",
        f"  deploys to EOD                      {r['deploys']}",
        f"  test runs                           {r['tests_run']}",
    ]
    for row in r["per_run"]:
        lines.append(
            f"  {row['ticket']}: {row['status']}, PRs {row['prs']}, implement "
            f"{row['implement_iterations']}, deploys {row['deploys']} "
            f"(sha ok: {row['deployed_sha_matches']}), approved by {row['approved_by']}, "
            f"${row['cost_usd']}"
        )
    for p in r["problems"]:
        lines.append(f"  PROBLEM: {p}")
    lines.append(f"  CLEAN                               {'yes' if r['clean'] else 'NO'}")
    lines.append(f"  report: {r['workdir']}/report.json")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--tickets", type=int, default=3)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--kill-min", type=float, default=1.0)
    p.add_argument("--kill-max", type=float, default=3.0)
    p.add_argument("--max-kills", type=int, default=8)
    p.add_argument("--max-downtime", type=float, default=1.5, help="seconds the server stays down")
    p.add_argument("--timeout", type=float, default=240.0)
    p.add_argument("--out", type=Path, default=REPO_ROOT / "chaos-out")
    p.add_argument("--expect-clean", action="store_true")
    args = p.parse_args(argv)
    report = run(args)
    print(format_report(report))
    return 1 if args.expect_clean and not report["clean"] else 0


if __name__ == "__main__":
    sys.exit(main())
