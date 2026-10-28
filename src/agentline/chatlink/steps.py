"""Pipeline steps for chat links.

    report:
      type: action
      uses: chat.send
      with: { text: "Deployed {{branch.name}}: {{test_report.summary}}" }

    ask:
      type: wait
      for: chat.ask
      with: { question: "Which environment?", choices: [eod, staging] }
      out: answer                     # {text, by, at}

`chat.ask` parks the run; the chat-link loop sends the question and delivers the reply (a
Telegram reply to the question, or a tap on a choice).
"""

from __future__ import annotations

from typing import Any

from agentline.chatlink import state
from agentline.chatlink.telegram import TelegramError
from agentline.context import StepContext
from agentline.errors import PermanentError
from agentline.registry import ActionCall, KeyWaiter, Registry


def ask_key(params: dict[str, Any], inputs: dict[str, Any]) -> str:
    return (
        f"chat:ask:{params.get('_run', '')}:{params.get('_node', '')}:{params.get('_iteration', 1)}"
    )


def send_linked(text: str) -> Any:
    """Send to every linked chat; PermanentError when none is linked (or keys are missing)."""
    from agentline.chatlink.platforms import PLATFORMS

    sent: list[Any] = []
    missing: list[str] = []
    for name, p in PLATFORMS.items():
        link = state.ChatState(name).linked()
        if not link:
            continue
        t = p.connect(False)
        if t is None:
            missing.append(p.title)
            continue
        try:
            sent.append(t.send(link["chat_id"], text))
        except (TelegramError, ConnectionError) as e:
            raise RuntimeError(f"{p.title}: {e}") from None  # retried like any outage
    if not sent:
        why = f" (keys missing for {', '.join(missing)})" if missing else ""
        raise PermanentError("chat.send: no chat is linked yet (Settings > Chat)" + why)
    return sent[0]


class ChatSend:
    name = "chat.send"
    deploy_class = False

    async def __call__(self, ctx: StepContext, call: ActionCall) -> dict[str, Any]:
        import asyncio

        text = call.params.get("text")
        if not isinstance(text, str) or not text.strip():
            raise PermanentError("chat.send: `text` is required")

        async def do() -> dict[str, Any]:
            message_id = await asyncio.to_thread(send_linked, text)
            return {"sent": True, "message_id": message_id}

        # a crash between sending and recording: send again rather than stay silent
        return await ctx.effect("send", do, on_unknown=call.effect.on_unknown or "retry",
                                description="chat message")  # fmt: skip


def register(registry: Registry) -> None:
    registry.add_action(ChatSend())
    registry.add_waiter(KeyWaiter("chat.ask", ask_key))
