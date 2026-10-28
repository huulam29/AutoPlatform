"""The chat-link loop (run by `agentline serve`): Telegram in and out.

In (only from the linked user; anyone else gets a "private bot" note):
  /start <code> or <code>   link this chat (code from Settings > Chat > Link, 10 minutes)
  /status /runs /pipelines  what's going on
  /approve <run> [node], /reject <run> [comment]   decide a gate (also: the buttons)
  /run <pipeline> [json]    start a run (text / chat start points: /run <pipeline> <words>)
  /say <run> <text>         talk to a run's gatekeeper (or reply to one of its messages)
  /answer <text>            answer the newest open question (or reply to it)
  /new                      start a new assistant conversation
  anything else             the editor assistant (proposals come with Apply buttons; keys
                            are never accepted here)
Out (Settings > Chat > Notify): gates waiting for approval (Approve / Reject; a gate that
guards a deploy-class step asks for a second tap), questions from `chat.ask`, runs that end
dead or needs-review, their failure triage verdicts, and finished runs. Every decision is
audited with `via: <platform>:@<user>`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from pathlib import Path
from typing import Any

from agentline.app import App, default_actor
from agentline.chatlink import state
from agentline.chatlink.telegram import MAX_TEXT, Telegram, TelegramError, token
from agentline.errors import ValidationError

log = logging.getLogger("agentline.chatlink")

HELP = (
    "agentline commands:\n"
    "/status: runs, approvals and questions waiting\n"
    "/runs: recent runs\n"
    "/pipelines: pipelines you can start\n"
    "/run <pipeline> [json payload, or words for text / chat pipelines]: start a run\n"
    "/say <run> <text>: talk to a run's gatekeeper (or reply to its message)\n"
    "/approve <run> [node], /reject <run> [comment]: decide a gate\n"
    "/retry <run> [node]: retry a failed run, or one node (only its failed tasks)\n"
    "/answer <text>: answer the newest question (or reply to it)\n"
    "/new: start a new assistant conversation\n"
    "Anything else goes to the assistant (set up agentline, build or change pipelines)."
)


class ChatLink:
    def __init__(
        self,
        app: App,
        *,
        registry: Any = None,
        pipelines_dir: Path | None = None,
        config_path: Path | None = None,
        poll_wait: int = 5,
        files: Any = None,
        platform: str = "telegram",
        transport: Any = None,
    ) -> None:
        self.app = app
        self.registry = registry
        self.pipelines_dir = pipelines_dir
        self.config_path = config_path
        self.poll_wait = poll_wait
        self.files_factory = files  # tests: a callable returning PipelineFiles
        self.platform = platform
        self.state = state.ChatState(platform)
        # () -> a connected transport, or None while its keys aren't set
        self.transport_factory = transport or (lambda: Telegram(t) if (t := token()) else None)
        self.turn_lock = asyncio.Lock()
        self.tasks: set[asyncio.Task[Any]] = set()

    # -- the loop ---------------------------------------------------------------------------
    async def run(self, stop: asyncio.Event) -> None:
        backoff = 0.0
        tg: Any = None
        while not stop.is_set():
            if tg is None:
                try:
                    tg = await asyncio.to_thread(self.transport_factory)
                except Exception as e:  # can't connect: try again later
                    log.warning("%s link: %s", self.platform, e)
                    tg = None
                if tg is None:  # no keys yet: check again later (keys can be set while running)
                    await _sleep(stop, 10)
                    continue
            try:
                await self.step(tg)
                backoff = 0.0
            except Exception as e:  # TelegramError, a dropped websocket, ...
                log.warning("%s link: %s", self.platform, e)
                if getattr(tg, "reconnect_on_error", False):
                    with contextlib.suppress(Exception):
                        tg.close()
                    tg = None
                backoff = min(60.0, backoff * 2 or 5.0)
                await _sleep(stop, backoff)
        if tg is not None and hasattr(tg, "close"):
            with contextlib.suppress(Exception):
                tg.close()
        for task in list(self.tasks):
            task.cancel()

    async def step(self, tg: Telegram) -> None:
        """One poll: handle new updates, then send what's due."""
        offset = self.state.read().get("offset")
        updates = await asyncio.to_thread(tg.updates, offset, self.poll_wait)
        for u in updates:
            with self.state.edit() as d:
                d["offset"] = int(u["update_id"]) + 1
            try:
                await self.handle(tg, u)
            except (TelegramError, ConnectionError):
                raise
            except Exception as e:  # one bad update mustn't stop the loop
                log.exception("chat link update failed")
                chat = _chat_of(u)
                if chat:
                    await asyncio.to_thread(tg.send, chat, f"Sorry, that failed: {e}")
        if self.state.linked():
            await self.notify(tg)

    # -- in ---------------------------------------------------------------------------------
    async def handle(self, tg: Telegram, u: dict[str, Any]) -> None:
        if "callback_query" in u:
            await self.callback(tg, u["callback_query"])
            return
        msg = u.get("message") or {}
        chat = (msg.get("chat") or {}).get("id")
        user = (msg.get("from") or {}).get("id")
        text = str(msg.get("text") or "").strip()
        if chat is None or not text:
            return
        link = self.state.linked()
        if link is None:
            await self.try_pair(tg, msg, text)
            return
        if user != link.get("user_id"):
            await asyncio.to_thread(
                tg.send, chat, "This is a private agentline bot; it only answers its owner."
            )
            return
        reply_to = (msg.get("reply_to_message") or {}).get("message_id")
        asks = self.state.read().get("asks") or {}
        if reply_to is not None and str(reply_to) in asks:
            await self.answer(tg, chat, asks[str(reply_to)], text, link)
            return
        threads = self.state.read().get("thread_msgs") or {}
        if reply_to is not None and str(reply_to) in threads and not text.startswith("/"):
            await self.say_to_run(tg, chat, threads[str(reply_to)], text, link)
            return
        if text.startswith("/"):
            await self.command(tg, chat, text, link)
            return
        self._spawn(self.assistant_turn(tg, chat, text))

    async def try_pair(self, tg: Telegram, msg: dict[str, Any], text: str) -> None:
        chat = msg["chat"]["id"]
        code = text.split()[-1] if text.split() else ""
        pair = self.state.read().get("pair") or {}
        if (
            (msg.get("chat") or {}).get("type") == "private"
            and code
            and code == pair.get("code")
            and time.time() < float(pair.get("expires") or 0)
        ):
            frm = msg.get("from") or {}
            with self.state.edit() as d:
                d[self.platform] = {
                    "chat_id": chat,
                    "user_id": frm.get("id"),
                    "username": frm.get("username") or frm.get("first_name") or "",
                    "linked_at": time.time(),
                }
                d.pop("pair", None)
            await asyncio.to_thread(
                tg.send, chat, "Linked to agentline. You'll get approvals, questions and "
                "reports here.\n\n" + HELP
            )  # fmt: skip
            return
        await asyncio.to_thread(
            tg.send,
            chat,
            "This is a private agentline bot. To link it, open agentline Settings > Chat > "
            "Link and send the code shown there.",
        )

    async def command(self, tg: Telegram, chat: int, text: str, link: dict[str, Any]) -> None:
        cmd, _, rest = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        rest = rest.strip()
        say = lambda t: asyncio.to_thread(tg.send, chat, t)  # noqa: E731
        if cmd in ("/start", "/help"):
            await say(HELP)
        elif cmd == "/status":
            await say(self.status_text())
        elif cmd == "/runs":
            await say(self.runs_text())
        elif cmd == "/pipelines":
            names = self.app.flow_names()
            await say("Pipelines: " + (", ".join(names) or "none loaded"))
        elif cmd in ("/approve", "/reject"):
            parts = rest.split(maxsplit=1)
            if not parts:
                await say(f"usage: {cmd} <run id> [{'node' if cmd == '/approve' else 'comment'}]")
                return
            run_id = parts[0]
            arg = parts[1] if len(parts) > 1 else None
            if cmd == "/approve":
                await say(await self.decide(run_id, arg, True, link))
            else:
                await say(await self.decide(run_id, None, False, link, comment=arg or ""))
        elif cmd == "/retry":
            parts = rest.split()
            if not parts:
                await say("usage: /retry <run id> [node]")
                return
            await say(await self.retry(parts[0], parts[1] if len(parts) > 1 else None, link))
        elif cmd == "/run":
            name, _, payload_text = rest.partition(" ")
            if not name:
                await say("usage: /run <pipeline> [json payload]")
                return
            kind, template = self._start_input(name)
            try:
                if kind in ("text", "chat"):
                    if not payload_text.strip():
                        await say(f"usage: /run {name} <what to do> ({name} starts from {kind})")
                        return
                    payload: Any = {"text": payload_text.strip()}
                else:
                    payload = json.loads(payload_text) if payload_text.strip() else {}
                sub = await self.app.submit(name, payload)
            except (ValueError, ValidationError, KeyError) as e:
                hint = f"\nIt takes JSON like {json.dumps(template)}" if template else ""
                await say(f"Couldn't start {name}: {e}{hint}")
                return
            more = " Its gatekeeper answers here: reply to its messages." if kind == "chat" else ""
            await say(f"Started {name}: run {sub.run_id}.{more}")
        elif cmd == "/say":
            run_id, _, words = rest.partition(" ")
            if not run_id or not words.strip():
                await say("usage: /say <run id> <text>")
                return
            await self.say_to_run(tg, chat, run_id, words.strip(), link)
        elif cmd == "/answer":
            open_asks = self.state.read().get("asks") or {}
            if not open_asks or not rest:
                await say("No open question." if not open_asks else "usage: /answer <text>")
                return
            newest = max(open_asks, key=float)  # ids: Telegram ints, Slack ts, Discord snowflakes
            await self.answer(tg, chat, open_asks[newest], rest, link)
        elif cmd == "/new":
            with self.state.edit() as d:
                d["history"] = []
                d["proposals"] = []
            await say("New conversation.")
        else:
            await say(f"Unknown command {cmd}.\n\n{HELP}")

    async def callback(self, tg: Telegram, cq: dict[str, Any]) -> None:
        link = self.state.linked()
        data = str(cq.get("data") or "")
        msg = cq.get("message") or {}
        chat = (msg.get("chat") or {}).get("id")
        if link is None or (cq.get("from") or {}).get("id") != link.get("user_id"):
            await asyncio.to_thread(tg.answer, cq["id"], "Not allowed")
            return
        kind, _, arg = data.partition(":")
        await asyncio.to_thread(tg.answer, cq["id"])
        gates = self.state.read().get("gates") or {}
        if kind in ("ga", "gr", "gc") and arg in gates:
            g = gates[arg]
            if kind == "ga" and g.get("deploy"):
                await asyncio.to_thread(
                    tg.send,
                    chat,
                    f"This approves a deploy ({g['pipeline']}, run {g['run']}). Sure?",
                    buttons=[[("Confirm deploy", f"gc:{arg}"), ("Reject", f"gr:{arg}")]],
                )
                return
            text = await self.decide(g["run"], g["node"], kind != "gr", link)
            if msg.get("message_id"):
                await asyncio.to_thread(tg.clear_buttons, chat, msg["message_id"])
            await asyncio.to_thread(tg.send, chat, text)
        elif kind == "q":
            key, _, idx = arg.rpartition(":")
            ask = (self.state.read().get("ask_choices") or {}).get(key)
            if ask and idx.isdigit() and int(idx) < len(ask["choices"]):
                await self.answer(tg, chat, ask["key"], ask["choices"][int(idx)], link)
                if msg.get("message_id"):
                    await asyncio.to_thread(tg.clear_buttons, chat, msg["message_id"])
        elif kind == "rt":
            r = (self.state.read().get("retries") or {}).get(arg)
            if r:
                await asyncio.to_thread(
                    tg.send, chat, await self.retry(r["run"], r.get("node"), link)
                )
                if msg.get("message_id"):
                    await asyncio.to_thread(tg.clear_buttons, chat, msg["message_id"])
        elif kind == "fx":
            r = (self.state.read().get("retries") or {}).get(arg)
            if r:
                from agentline import fixit

                found = await asyncio.to_thread(fixit.for_run, self.app, r["run"])
                if found:
                    self._spawn(
                        self.assistant_turn(tg, chat, fixit.request_text(found["text"], r["run"]))
                    )
        elif kind in ("rc", "rC", "rd"):
            card = (self.state.read().get("cards") or {}).get(arg)
            if card is None:
                return
            if kind == "rc" and card.get("deploy"):
                await asyncio.to_thread(
                    tg.send, chat, f"{card['title']} lets a deploy run (run {card['run']}). Sure?",
                    buttons=[[("Confirm deploy", f"rC:{arg}"), ("Dismiss", f"rd:{arg}")]],
                )  # fmt: skip
                return
            await self.decide_card(tg, chat, card, dismiss=kind == "rd", link=link)
            if msg.get("message_id"):
                await asyncio.to_thread(tg.clear_buttons, chat, msg["message_id"])
        elif kind == "p" and arg.isdigit():
            await self.apply_proposal(tg, chat, int(arg))
        elif kind == "s" and arg.isdigit():
            choices = self.state.read().get("choices") or []
            if int(arg) < len(choices):
                self._spawn(self.assistant_turn(tg, chat, choices[int(arg)]))

    # -- decisions and answers --------------------------------------------------------------
    async def decide(
        self,
        run_id: str,
        node: str | None,
        approve: bool,
        link: dict[str, Any],
        comment: str = "",
    ) -> str:
        actor = default_actor()
        via = f"{self.platform}:@{link.get('username') or link.get('user_id')}"
        fn = self.app.approve if approve else self.app.reject
        try:
            gate = await fn(run_id, node, actor=actor, comment=comment or f"from {via}", via=via)
        except PermissionError as e:
            return f"Not allowed: {e}"
        except ValidationError as e:
            return f"Couldn't {'approve' if approve else 'reject'}: {e}"
        return f"{'Approved' if approve else 'Rejected'} {gate.node_id} of run {run_id}."

    async def retry(self, run_id: str, node: str | None, link: dict[str, Any]) -> str:
        via = f"{self.platform}:@{link.get('username') or link.get('user_id')}"
        try:
            n = await self.app.retry(run_id, node_id=node, actor=default_actor(), via=via)
        except ValidationError as e:
            return f"Couldn't retry: {e}"
        if not n:
            return f"Nothing to retry in run {run_id}" + (f" at {node}" if node else "") + "."
        return f"Retrying run {run_id}" + (f" at {node}" if node else "") + "."

    async def answer(
        self, tg: Telegram, chat: int, key: str, text: str, link: dict[str, Any]
    ) -> None:
        payload = {"text": text, "by": link.get("username") or str(link.get("user_id")),
                   "at": time.time(), "via": self.platform}  # fmt: skip
        woken = await self.app.deliver_event(key, payload)
        with self.state.edit() as d:
            d["asks"] = {k: v for k, v in (d.get("asks") or {}).items() if v != key}
            d["ask_choices"] = {
                k: v for k, v in (d.get("ask_choices") or {}).items() if v.get("key") != key
            }
        await asyncio.to_thread(
            tg.send,
            chat,
            "Thanks, passed it on." if woken else "That question was already answered.",
        )

    # -- run conversations (chat start nodes, agentline.runchat) ----------------------------
    def _via(self, link: dict[str, Any]) -> str:
        return f"{self.platform}:@{link.get('username') or link.get('user_id')}"

    def _start_input(self, name: str) -> tuple[str, Any]:
        from agentline.pipeline.start import start_info

        try:
            spec = self.app.graph(name).meta.get("spec")
        except Exception:
            return "json", None
        if spec is None:
            return "json", None
        info = start_info(spec)
        return info["input"], info["template"]

    async def say_to_run(
        self, tg: Telegram, chat: int, run_id: str, text: str, link: dict[str, Any]
    ) -> None:
        from agentline import runchat

        try:
            await runchat.say(self.app, run_id, text, via=self._via(link))
        except ValidationError as e:
            await asyncio.to_thread(tg.send, chat, f"Couldn't pass it on: {e}")

    async def decide_card(
        self, tg: Telegram, chat: int, card: dict[str, Any], *, dismiss: bool, link: dict[str, Any]
    ) -> None:
        from agentline import runchat

        try:
            out = await runchat.apply_card(self.app, card["run"], int(card["message"]),
                                           int(card["index"]), actor=default_actor(),
                                           via=self._via(link), dismiss=dismiss)  # fmt: skip
        except ValidationError as e:
            await asyncio.to_thread(tg.send, chat, f"Couldn't: {e}")
            return
        if out["state"] == "failed":
            await asyncio.to_thread(tg.send, chat, f"{card['title']} failed: {out.get('error')}")

    def _chat_runs(self, since: float) -> list[tuple[Any, Any]]:
        """(run, its chat node) for recent runs of pipelines with a gatekeeper."""
        from agentline import runchat

        out = []
        with self.app.ledger.tx() as tx:
            runs = tx.list_runs(None, 100)
        for r in runs:
            if r.created_at < since:
                continue
            node = runchat.chat_node(runchat.spec_of(self.app, r))
            if node is not None:
                out.append((r, node))
        return out

    async def forward_threads(self, tg: Telegram, chat: int, since: float) -> set[str]:
        """Send what gatekeepers and pipelines said since the last poll; returns the ids of
        runs with a gatekeeper (their generic notices are left out: the thread covers them)."""
        from agentline import runchat

        seen: set[str] = set()
        for run, node in await asyncio.to_thread(self._chat_runs, since):
            seen.add(run.id)
            last = int((self.state.read().get("threads") or {}).get(run.id, 0))
            msgs = await asyncio.to_thread(runchat.thread, self.app, run.id, last)
            for m in msgs:
                if m.id <= last:
                    continue
                skip = m.role == "user" or (m.role == "system" and m.detail.get("kind") == "gate")
                if not skip:
                    await self._send_thread_message(tg, chat, run, node.id, m)
                with self.state.edit() as d:
                    d.setdefault("threads", {})[run.id] = m.id
        return seen

    async def _send_thread_message(
        self, tg: Telegram, chat: int, run: Any, node_id: str, m: Any
    ) -> None:
        who = node_id if m.role == "agent" else "pipeline"
        text = f"💬 {run.flow} (run {run.id}) · {who}:\n\n{m.text}"
        if m.role == "agent":
            text += "\n\nReply to this message to answer."
        buttons = []
        cards: dict[str, Any] = {}
        for i, c in enumerate(m.detail.get("actions") or []):
            if c.get("state") != "proposed":
                continue
            short = f"{len(self.state.read().get('cards') or {}) + len(cards) + 1}"
            title = c["type"].capitalize() + (f" {c['node']}" if c.get("node") else "")
            cards[short] = {"run": run.id, "message": m.id, "index": i, "title": title,
                            "deploy": bool(c.get("deploy"))}  # fmt: skip
            buttons.append([(f"Confirm: {title}", f"rc:{short}"), ("Dismiss", f"rd:{short}")])
        mid = await asyncio.to_thread(tg.send, chat, text, buttons=buttons or None)
        self._mark(f"m:{run.id}:{m.id}", mid, thread_msgs={str(mid): run.id}, cards=cards)

    # -- out --------------------------------------------------------------------------------
    def _waiting(self) -> list[tuple[Any, Any]]:
        out = []
        with self.app.ledger.tx() as tx:
            for run in tx.list_runs("waiting", 500):
                for x in tx.list_executions(run.id):
                    if x.status == "waiting":
                        out.append((run, x))
        return out

    def _guards_deploy(self, flow: str, node_id: str) -> bool:
        from agentline.runchat import guards_deploy

        return guards_deploy(self.app, self.registry, flow, node_id)

    async def notify(self, tg: Telegram) -> None:
        link = self.state.linked()
        if link is None:
            return
        chat = link["chat_id"]
        from agentline import costs

        crossed = await asyncio.to_thread(costs.due_alerts, self.app)
        for a in crossed[-1:]:  # only the highest threshold crossed since the last check
            await asyncio.to_thread(
                tg.send,
                chat,
                f"💸 Spend this month ({a['month']}) is ${a['spent']:.2f}: {a['threshold']}% of the"
                f" ${a['budget']:.2f} budget." + (" Runs keep going: pause pipelines if needed."
                                                  if a["threshold"] >= 100 else ""),
            )  # fmt: skip
        prefs = self.state.notify_prefs()
        since = float(link.get("linked_at") or 0)
        gatekept = await self.forward_threads(tg, chat, since)
        sent = self.state.read().get("sent") or {}
        waiting = await asyncio.to_thread(self._waiting)
        for run, x in waiting:
            key = f"x:{x.id}"
            if key in sent:
                continue
            kind = x.detail.get("kind")
            if kind == "gate" and prefs["gates"]:
                await self._send_gate(tg, chat, run, x, key)
            elif x.detail.get("waiter") == "chat.ask" and prefs["questions"]:
                await self._send_ask(tg, chat, run, x, key)
        if prefs["failures"] or prefs["done"]:
            statuses = [s for s, on in (("dead", prefs["failures"]),
                                        ("needs-review", prefs["failures"]),
                                        ("done", prefs["done"])) if on]  # fmt: skip
            with self.app.ledger.tx() as tx:
                runs = [r for s in statuses for r in tx.list_runs(s, 50)]
            for r in runs:
                key = f"r:{r.id}:{r.status}"
                if key in sent or r.updated_at < since or r.id in gatekept:
                    continue  # a gatekeeper reports its own run's end in the thread
                icon = {"done": "✅", "dead": "❌", "needs-review": "⚠️"}.get(r.status, "")
                text = f"{icon} {r.flow} run {r.id}: {r.status}"
                if r.error:
                    text += f"\n{r.error[:1500]}"
                if r.status == "done":
                    mid = await asyncio.to_thread(tg.send, chat, text)
                    self._mark(key, mid)
                    continue
                failed_nodes = self._failed_nodes(r.id)
                short = f"{len(self.state.read().get('retries') or {}) + 1}"
                retry_label = f"Retry {failed_nodes[0]}" if len(failed_nodes) == 1 else "Retry run"
                buttons = [[(retry_label, f"rt:{short}"), ("Fix it", f"fx:{short}")]]
                text += "\n\nRetry reruns only what failed (finished tasks are kept)."
                mid = await asyncio.to_thread(tg.send, chat, text, buttons=buttons)
                node = failed_nodes[0] if len(failed_nodes) == 1 else None
                self._mark(key, mid, retries={short: {"run": r.id, "node": node}})
        if prefs["failures"]:  # failure triage verdicts (agentline.triage)
            from agentline.triage import TriageStore, chat_text

            for rec in await asyncio.to_thread(TriageStore().list, None, 30):
                key = f"t:{rec['id']}:{rec.get('created_at')}"
                if rec.get("status") != "done" or key in sent:
                    continue
                if float(rec.get("updated_at") or 0) < since:
                    continue
                mid = await asyncio.to_thread(tg.send, chat, chat_text(rec))
                self._mark(key, mid)
        if prefs.get("reviews"):  # run reviews (agentline.review)
            from agentline import review

            for rec in await asyncio.to_thread(review.ReviewStore().list, None, None, 30):
                key = f"v:{rec['id']}:{rec.get('created_at')}"
                if rec.get("status") != "done" or key in sent:
                    continue
                if float(rec.get("updated_at") or 0) < since:
                    continue
                mid = await asyncio.to_thread(tg.send, chat, review.chat_text(rec))
                self._mark(key, mid)

    def _failed_nodes(self, run_id: str) -> list[str]:
        with self.app.ledger.tx() as tx:
            return sorted({x.node_id for x in tx.list_executions(run_id)
                           if x.status in ("dead", "needs-review")})  # fmt: skip

    def _mark(self, key: str, message_id: int, **extra: Any) -> None:
        with self.state.edit() as d:
            d.setdefault("sent", {})[key] = message_id
            for field, value in extra.items():
                d.setdefault(field, {}).update(value)

    async def _send_gate(self, tg: Telegram, chat: int, run: Any, x: Any, key: str) -> None:
        short = f"{len(self.state.read().get('gates') or {}) + 1}"
        deploy = await asyncio.to_thread(self._guards_deploy, run.flow, x.node_id)
        prompt = str(x.detail.get("prompt") or "Approve?")
        text = f"🔔 {run.flow} (run {run.id}) is waiting at {x.node_id}:\n\n{prompt}"
        if deploy:
            text += "\n\nThis gate guards a deploy."
        mid = await asyncio.to_thread(
            tg.send, chat, text, buttons=[[("Approve", f"ga:{short}"), ("Reject", f"gr:{short}")]]
        )
        gate = {"run": run.id, "node": x.node_id, "pipeline": run.flow, "deploy": deploy}
        self._mark(key, mid, gates={short: gate})

    async def _send_ask(self, tg: Telegram, chat: int, run: Any, x: Any, key: str) -> None:
        params = x.detail.get("params") or {}
        question = str(params.get("question") or "agentline has a question.")
        choices = [str(c) for c in (params.get("choices") or [])][:8]
        short = f"{len(self.state.read().get('ask_choices') or {}) + 1}"
        text = (
            f"❓ {run.flow} (run {run.id}) asks:\n\n{question}\n\nReply to this message to answer."
        )
        buttons = [[(c, f"q:{short}:{i}")] for i, c in enumerate(choices)] or None
        mid = await asyncio.to_thread(tg.send, chat, text, buttons=buttons)
        corr = x.correlation_key or ""
        self._mark(
            key, mid, asks={str(mid): corr}, ask_choices={short: {"key": corr, "choices": choices}}
        )

    def status_text(self) -> str:
        with self.app.ledger.tx() as tx:
            counts = {s: len(tx.list_runs(s, 1000)) for s in
                      ("running", "pending", "waiting", "needs-review", "dead")}  # fmt: skip
        waiting = self._waiting()
        gates = [f"- {r.flow} run {r.id} at {x.node_id}" for r, x in waiting
                 if x.detail.get("kind") == "gate"]  # fmt: skip
        asks = [f"- {r.flow} run {r.id}: {(x.detail.get('params') or {}).get('question', '')}"
                for r, x in waiting if x.detail.get("waiter") == "chat.ask"]  # fmt: skip
        active = ", ".join(f"{v} {k}" for k, v in counts.items() if v)
        lines = [f"Runs: {active or 'none active'}"]
        if gates:
            lines += ["Waiting for approval:", *gates]
        if asks:
            lines += ["Open questions:", *asks]
        return "\n".join(lines)

    def runs_text(self) -> str:
        with self.app.ledger.tx() as tx:
            runs = tx.list_runs(None, 10)
        if not runs:
            return "No runs yet."
        return "\n".join(f"{r.id}  {r.flow}  {r.status}" for r in runs)

    # -- the assistant ----------------------------------------------------------------------
    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def _files(self) -> Any:
        if self.files_factory is not None:
            return self.files_factory()
        from agentline.api.editor import PipelineFiles
        from agentline.builtins import default_registry
        from agentline.config import Config

        cfg = (
            Config.load(self.config_path)
            if self.config_path and self.config_path.exists()
            else Config()
        )
        return PipelineFiles(self.pipelines_dir or Path.cwd(), default_registry(cfg), cfg)

    async def assistant_turn(self, tg: Telegram, chat: int, text: str) -> None:
        from agentline import assistant

        async with self.turn_lock:
            history = (self.state.read().get("history") or [])[-20:]
            history.append({"role": "user", "content": text})
            await asyncio.to_thread(tg.send, chat, "Thinking…")
            try:
                reply = await assistant.converse(self._files(), history)
            except assistant.AssistantError as e:
                await asyncio.to_thread(tg.send, chat, f"The assistant couldn't run: {e}")
                return
            actions = reply["actions"]
            history.append({"role": "assistant", "content": reply["message"]})
            with self.state.edit() as d:
                d["history"] = history[-20:]
                d["proposals"] = actions
                d["choices"] = reply["choices"]
            body = reply["message"] or "(no answer)"
            rows: list[list[tuple[str, str]]] = []
            for i, a in enumerate(actions):
                title = assistant_title(a)
                if a.get("type") == "key":
                    body += (
                        f"\n\n🔑 {title}: set it in the app (Settings > Chat apps);"
                        " keys never go through chat."
                    )
                    continue
                if a.get("issues"):
                    body += f"\n\n{title} has issues: " + "; ".join(a["issues"])[:400]
                rows.append([(f"Apply: {title}"[:60], f"p:{i}")])
            rows += [[(c[:60], f"s:{i}")] for i, c in enumerate(reply["choices"])]
            await asyncio.to_thread(tg.send, chat, body[:MAX_TEXT], buttons=rows or None)

    async def apply_proposal(self, tg: Telegram, chat: int, i: int) -> None:
        from agentline import assistant
        from agentline.skills import SkillLibrary

        proposals = self.state.read().get("proposals") or []
        if i >= len(proposals):
            await asyncio.to_thread(tg.send, chat, "That proposal is gone; ask again.")
            return
        a = proposals[i]
        files = self._files()
        try:
            if a.get("type") == "setting":
                await asyncio.to_thread(
                    assistant.apply_setting, files, a["section"], a["name"], a.get("body") or {}
                )
                done = f"Saved {a['section']}.{a['name']}. Restart the engine to use it in runs."
            elif a.get("type") == "fetch_skills":
                info = await asyncio.to_thread(SkillLibrary().fetch)
                done = f"Downloaded {info['count']} skills."
            elif a.get("type") == "skill":
                await asyncio.to_thread(
                    SkillLibrary().save_local,
                    str(a.get("name")),
                    str(a.get("description") or ""),
                    str(a.get("body") or ""),
                )
                done = f"Saved your skill {a.get('name')}."
            elif a.get("type") == "retry":
                link = self.state.linked() or {}
                done = await self.retry(str(a.get("run")), a.get("node") or None, link)
            elif a.get("type") == "pipeline":
                from agentline import history

                await asyncio.to_thread(history.record, files.root, "before assistant change")
                target = await asyncio.to_thread(
                    assistant.write_pipeline, files.root, a["path"], a["yaml"], a.get("files") or {}
                )
                await asyncio.to_thread(
                    history.record, files.root, f"assistant (chat): {a['path']}"
                )
                done = f"Wrote {files.name_of(target)}.yaml. Open it in the editor to review."
            elif a.get("type") == "lesson":
                from agentline import chat_instructions

                await asyncio.to_thread(chat_instructions.add_lesson, str(a.get("text") or ""))
                done = "Added the lesson to the Chat instructions."
            elif a.get("type") == "key":
                done = "Keys are set in the app (Settings > Chat apps)."
            else:
                done = "Apply this one in the app: open the Chat there and ask again."
        except (ValidationError, KeyError, OSError) as e:
            done = f"Couldn't apply it: {e}"
        with self.state.edit() as d:
            d.setdefault("history", []).append(
                {"role": "assistant", "content": f"[{assistant_title(a)}: {done}]"}
            )
        await asyncio.to_thread(tg.send, chat, done)


def assistant_title(a: dict[str, Any]) -> str:
    from agentline.assistant import title

    return title(a)


def _chat_of(u: dict[str, Any]) -> int | None:
    msg = u.get("message") or (u.get("callback_query") or {}).get("message") or {}
    return (msg.get("chat") or {}).get("id")


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), seconds)


async def run_chatlink(app: App, stop: asyncio.Event, **kw: Any) -> None:
    await ChatLink(app, **kw).run(stop)
