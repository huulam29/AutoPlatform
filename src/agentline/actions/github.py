"""github.open_pr, github.read_reviews, and the github.review waiter."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agentline.actions._util import config_of, git, repo_section
from agentline.config import Config
from agentline.context import StepContext
from agentline.errors import PermanentError
from agentline.integrations.github import GitHubClient, pr_summary, split_repo
from agentline.integrations.http import PermanentApiError
from agentline.registry import ActionCall

GitHubFactory = Callable[[], GitHubClient]
DECISIVE = ("approved", "changes_requested")


def resolve_repo(params: dict[str, Any], branch: dict[str, Any], config: Config) -> str:
    """`owner/name` from `with: {repo}`, else the branch's repo (a slug or a config name)."""
    for ref in (params.get("repo"), branch.get("repo")):
        if not ref:
            continue
        ref = str(ref)
        if "/" in ref and ":" not in ref:
            split_repo(ref)
            return ref
        slug = repo_section(config, ref).get("github")
        if slug:
            split_repo(str(slug))
            return str(slug)
    raise PermanentError(
        "github: can't tell which repo to use; set `with: { repo: owner/name }` or "
        '`[repos.<name>] github = "owner/name"` in agentline.toml'
    )


def _push_remote(params: dict[str, Any], branch: dict[str, Any], config: Config) -> str:
    if params.get("remote"):
        return str(params["remote"])
    name = branch.get("repo")
    if name and (remote := repo_section(config, str(name)).get("remote")):
        return str(remote)
    return "origin"


def _branch_input(call: ActionCall) -> dict[str, Any]:
    branch = call.inputs.get("branch")
    if not isinstance(branch, dict) or not branch.get("name"):
        raise PermanentError("github.open_pr needs a `branch` input with at least `name`")
    return branch


@dataclass
class OpenPR:
    """Push the branch, then open (or reuse) its pull request.

    Effect strategies:
    - `push` (own effect, **key** `push:<repo>:<branch>@<sha>`): `git -C <mirror> push <remote>
      <sha>:refs/heads/<name>`. Pushing the same sha again is a no-op, so after a crash the
      guard just pushes again. Skipped with `with: { push: false }` or when the branch has no
      `mirror`. `with: { force: true }` force-pushes (the branch was rewritten).
    - `open_pr` (**check** + find-or-create, decision 12): `do()` first lists open PRs with
      `head=<owner>:<branch>` and reuses one if found, so loop iterations reuse the PR; the
      check after a crash is the same lookup.

    Params: `repo` (owner/name; else from config `[repos.<branch.repo>] github`), `title`,
    `body`, `base` (default: branch `base` / repo `default_branch` / main), `draft`, `remote`.
    Returns `{number, url, branch, repo, head_sha, base, state}`.
    """

    client: GitHubFactory = GitHubClient.from_env
    config: Config | None = None
    name: str = "github.open_pr"
    deploy_class: bool = False

    async def __call__(self, ctx: StepContext, call: ActionCall) -> dict[str, Any]:
        config = config_of(call, self.config)
        params = call.params
        branch = _branch_input(call)
        repo = resolve_repo(params, branch, config)
        owner, _ = split_repo(repo)
        name, sha = str(branch["name"]), branch.get("sha")
        on_unknown = call.effect.on_unknown or "halt"

        mirror = branch.get("mirror")
        if params.get("push", True) is not False and mirror:
            if not sha:
                raise PermanentError("github.open_pr: branch has a mirror but no sha to push")
            remote = _push_remote(params, branch, config)
            refspec = f"{'+' if params.get('force') else ''}{sha}:refs/heads/{name}"

            async def push() -> dict[str, Any]:
                await git("-C", str(mirror), "push", remote, refspec)
                return {"remote_ref": f"refs/heads/{name}", "sha": sha}

            await ctx.effect(
                "push",
                push,
                key=f"push:{repo}:{name}@{sha}",
                on_unknown=on_unknown,
                description=f"git push {sha} to {name}",
            )

        gh = self.client()
        head = f"{owner}:{name}"
        repo_cfg = repo_section(config, str(branch.get("repo") or ""))
        base = params.get("base") or branch.get("base") or repo_cfg.get("default_branch", "main")
        title = str(params.get("title") or name)
        body = str(params.get("body") or "")
        body = f"{body}\n\n<!-- agentline run {ctx.run_id} -->".lstrip()

        def summarize(pr: dict[str, Any]) -> dict[str, Any]:
            out = pr_summary(pr, repo)
            out["branch"] = name
            if sha:  # the commit under review now (iteration 2 pushes a new one)
                out["head_sha"] = sha
            return out

        async def find() -> dict[str, Any] | None:
            prs = await asyncio.to_thread(gh.list_pulls, repo, head=head, state="open")
            return summarize(prs[0]) if prs else None

        async def find_or_create() -> dict[str, Any]:
            if existing := await find():
                return existing
            try:
                created = await asyncio.to_thread(
                    gh.create_pull,
                    repo,
                    head=name,
                    base=str(base),
                    title=title,
                    body=body,
                    draft=bool(params.get("draft", False)),
                )
            except PermanentApiError as e:
                # 422 "already exists": someone opened it between our lookup and create
                if e.status == 422 and (raced := await find()):
                    return raced
                raise
            return summarize(created)

        pr = await ctx.effect(
            "open_pr",
            find_or_create,
            check=find,
            key=call.effect.key,
            on_unknown=on_unknown,
            description=f"open a PR for {head} on {repo}",
        )
        if sha and pr.get("head_sha") != sha:
            pr = {**pr, "head_sha": sha}
        return pr


def _pr_ref(params: dict[str, Any], inputs: dict[str, Any]) -> tuple[str, int]:
    pr = inputs.get("pr") if isinstance(inputs.get("pr"), dict) else {}
    repo = params.get("repo") or pr.get("repo")
    number = params.get("number") or params.get("pr") or pr.get("number")
    if not repo or not number:
        raise PermanentError("needs a `pr` input with `repo` and `number` (or with: repo, number)")
    split_repo(str(repo))
    return str(repo), int(number)


def review_comments(
    comments: list[dict[str, Any]], body: str | None = None
) -> list[str]:  # rendered into the next implement prompt
    out = [body.strip()] if body and body.strip() else []
    for c in comments:
        where = c.get("path") or ""
        if where and c.get("line"):
            where = f"{where}:{c['line']}"
        text = str(c.get("body") or "").strip()
        if text:
            out.append(f"{where}: {text}" if where else text)
    return out


def review_payload(
    repo: str, number: int, review: dict[str, Any], comments: list[dict[str, Any]], *, source: str
) -> dict[str, Any]:
    """The event a github.review wait resumes with (same shape from webhook and poller).
    `event_id` names the review, so the webhook and the poller never deliver it twice."""
    return {
        "event_id": f"github-review:{repo}#{number}:{review.get('id')}",
        "state": str(review.get("state") or "").lower(),
        "comments": review_comments(comments, review.get("body")),
        "reviewer": (review.get("user") or {}).get("login"),
        "commit_id": review.get("commit_id"),
        "review_id": review.get("id"),
        "submitted_at": review.get("submitted_at"),
        "source": source,
    }


@dataclass
class ReadReviews:
    """Read a PR's reviews. Read-only: no effect guard.

    Params: `repo`, `number` (default: from the `pr` input). Returns `{state, reviews,
    comments}` where `state` is the latest decisive review state (approved /
    changes_requested) or `pending`.
    """

    client: GitHubFactory = GitHubClient.from_env
    name: str = "github.read_reviews"
    deploy_class: bool = False

    async def __call__(self, ctx: StepContext, call: ActionCall) -> dict[str, Any]:
        repo, number = _pr_ref(call.params, call.inputs)
        gh = self.client()
        reviews = await asyncio.to_thread(gh.list_reviews, repo, number)
        comments = await asyncio.to_thread(gh.list_review_comments, repo, number)
        decisive = [r for r in reviews if str(r.get("state", "")).lower() in DECISIVE]
        latest = decisive[-1] if decisive else None
        return {
            "repo": repo,
            "number": number,
            "state": str(latest["state"]).lower() if latest else "pending",
            "reviews": [
                {
                    "id": r.get("id"),
                    "state": str(r.get("state", "")).lower(),
                    "reviewer": (r.get("user") or {}).get("login"),
                    "body": r.get("body") or "",
                    "commit_id": r.get("commit_id"),
                    "submitted_at": r.get("submitted_at"),
                }
                for r in reviews
            ],
            "comments": review_comments(comments),
        }


@dataclass
class MergePR:
    """Merge a pull request (into its base, usually main).

    Effect strategy: **check** — after a crash the PR is read back and an already-merged PR
    counts as done, so a PR is merged once. Params: `repo`, `number` (default: the `pr`
    input), `method` (squash | merge | rebase, default squash), `sha` (default: the PR's
    `head_sha`, so a head that moved since the review is not merged). Returns `{merged, sha,
    number, repo}`.
    """

    client: GitHubFactory = GitHubClient.from_env
    name: str = "github.merge_pr"
    deploy_class: bool = False

    async def __call__(self, ctx: StepContext, call: ActionCall) -> dict[str, Any]:
        repo, number = _pr_ref(call.params, call.inputs)
        method = str(call.params.get("method") or "squash")
        if method not in ("squash", "merge", "rebase"):
            raise PermanentError(f"github.merge_pr: unknown method {method!r}")
        pr_input = call.inputs.get("pr") if isinstance(call.inputs.get("pr"), dict) else {}
        sha = call.params.get("sha") or pr_input.get("head_sha")
        gh = self.client()

        async def check() -> dict[str, Any] | None:
            pr = await asyncio.to_thread(gh.get_pull, repo, number)
            if pr.get("merged"):
                return {"merged": True, "sha": pr.get("merge_commit_sha"), "number": number,
                        "repo": repo}  # fmt: skip
            return None

        async def merge() -> dict[str, Any]:
            if done := await check():
                return done
            r = await asyncio.to_thread(gh.merge_pull, repo, number, method=method, sha=sha)
            return {"merged": bool(r.get("merged")), "sha": r.get("sha"), "number": number,
                    "repo": repo}  # fmt: skip

        return await ctx.effect(
            "merge",
            merge,
            check=check,
            on_unknown=call.effect.on_unknown or "halt",
            description=f"merge {repo}#{number}",
        )


def _epoch(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def review_key(repo: str, number: int) -> str:
    return f"github:pr:{repo}#{number}"


@dataclass
class ReviewWaiter:
    """`type: wait, for: github.review`: parks on `github:pr:<owner>/<repo>#<number>` from the
    `pr` input. The GitHub webhook delivers `pull_request_review` events on that key; `poll()`
    is the fallback when `poll_every` is set (see agentline.poller)."""

    client: GitHubFactory = GitHubClient.from_env
    name: str = "github.review"

    def correlation_key(self, params: dict[str, Any], inputs: dict[str, Any]) -> str:
        return review_key(*_pr_ref(params, inputs))

    async def poll(
        self, params: dict[str, Any], inputs: dict[str, Any], since: float | None = None
    ) -> dict[str, Any] | None:
        """Latest approved / changes_requested review of the PR's current head commit (the
        `pr` input's `head_sha`). Without a head sha, only reviews submitted at or after
        `since` count, so the review that started a loop iteration isn't delivered again."""
        repo, number = _pr_ref(params, inputs)
        pr = inputs.get("pr") if isinstance(inputs.get("pr"), dict) else {}
        head_sha = pr.get("head_sha")
        gh = self.client()
        reviews = await asyncio.to_thread(gh.list_reviews, repo, number)
        decisive = [
            r
            for r in reviews
            if str(r.get("state", "")).lower() in DECISIVE
            and (
                r.get("commit_id") == head_sha
                if head_sha
                else since is None or (_epoch(r.get("submitted_at")) or 0.0) >= since
            )
        ]
        if not decisive:
            return None
        latest = decisive[-1]
        comments = await asyncio.to_thread(gh.list_review_comments, repo, number, latest["id"])
        return review_payload(repo, number, latest, comments, source="poll")
