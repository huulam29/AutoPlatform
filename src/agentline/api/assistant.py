"""Editor assistant API (agentline.assistant). Proposes; never applies on its own.

POST /assistant/chat     {harness?, messages: [{role, content}], pipeline?, source?}
                         -> {message, choices, actions (each with `issues`), harness, cost_usd}
POST /assistant/attachments   an image (raw body) -> {id, path}; send its id in a user
                         message's `attachments`
PUT  /assistant/transcripts/{id}   {text}  the user's correction of an image transcript
POST /assistant/transcripts/{id}/redo   transcribe the image again (agentline.vision)
POST /assistant/apply    {path, yaml, files}   write an accepted pipeline proposal
                         (every other action is applied through the API of its GUI page)
GET  /assistant/conversations?q=   {conversations: [summary], archive_days}: newest first;
                         `archived` = no activity for 7 days or archived by hand
GET  /assistant/conversations/{id}   the whole conversation (messages as the panel keeps them)
PUT  /assistant/conversations/{id}   {messages}  create or replace (agentline.conversations)
POST /assistant/conversations/{id}/archive | /unarchive
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from agentline import assistant, conversations, history
from agentline.errors import ValidationError


class TranscriptBody(BaseModel):
    text: str


class Message(BaseModel):
    role: str
    content: str
    attachments: list[str] = Field(default_factory=list)  # ids from /assistant/attachments


class ChatBody(BaseModel):
    harness: str | None = None  # empty: the setup choice (agentline.setup), else claude-code
    agent: str | None = None  # the old name of `harness`
    model: str | None = None
    messages: list[Message] = Field(default_factory=list)
    pipeline: str | None = None  # the open pipeline's name
    source: str | None = None  # its current (maybe unsaved) YAML


class ConversationBody(BaseModel):
    messages: list[dict[str, Any]]


class ApplyBody(BaseModel):
    path: str
    yaml: str
    files: dict[str, str] = Field(default_factory=dict)


def create_assistant_router(
    files: Any, app: Any = None, issues: Any = None, incidents: Any = None
) -> APIRouter:
    """`files` is the editor's PipelineFiles (root, registry, config); `app`, `issues` and
    `incidents` feed the LIVE STATE (runs, gates, issues) the agent proposes against."""
    router = APIRouter()

    @router.post("/assistant/chat")
    async def chat(body: ChatBody, request: Request) -> dict[str, Any]:
        """Cancel = the client goes away (the panel's Cancel aborts the request): the agent's
        process group is killed (runners kill on cancellation)."""
        live = await asyncio.to_thread(
            assistant.gather_live, files, app, issues, incidents, body.pipeline
        )
        turn = asyncio.ensure_future(
            assistant.converse(
                files,
                [m.model_dump() for m in body.messages],
                harness=body.harness or body.agent,
                model=body.model,
                pipeline=body.pipeline,
                source=body.source,
                live=live,
                app=app,
                incidents=incidents,
            )
        )
        while not turn.done():
            await asyncio.wait({turn}, timeout=0.5)
            if not turn.done() and await request.is_disconnected():
                turn.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await turn
                raise HTTPException(499, "cancelled")
        try:
            return turn.result()
        except assistant.AssistantError as e:
            raise HTTPException(e.status, str(e)) from None

    @router.post("/assistant/attachments")
    async def attach(request: Request) -> dict[str, Any]:
        """Body: the image bytes (Content-Type image/png, jpeg, gif or webp)."""
        try:
            return assistant.save_attachment(
                await request.body(), request.headers.get("content-type", "")
            )
        except ValidationError as e:
            raise HTTPException(422, str(e)) from None

    @router.put("/assistant/transcripts/{att_id}")
    def put_transcript(att_id: str, body: TranscriptBody) -> dict[str, Any]:
        from agentline import vision

        if assistant.attachment_path(att_id) is None:
            raise HTTPException(404, f"no attached image {att_id!r}")
        if not body.text.strip():
            raise HTTPException(422, "the transcript is empty")
        return vision.save(att_id, body.text, "you", edited=True)

    @router.post("/assistant/transcripts/{att_id}/redo")
    async def redo_transcript(att_id: str) -> dict[str, Any]:
        from agentline import vision

        try:
            return await vision.transcribe(files, att_id, refresh=True)
        except ValidationError as e:
            raise HTTPException(422, str(e)) from None
        except Exception as e:  # a model API or CLI failure
            raise HTTPException(502, f"reading the image failed: {e}") from None

    @router.post("/assistant/apply")
    def apply(body: ApplyBody) -> dict[str, Any]:
        issues = assistant.check_pipeline(
            {"path": body.path, "yaml": body.yaml, "files": body.files},
            files.root,
            files.registry,
            files.config,
        )
        try:
            with files.lock:
                history.record(files.root, "before assistant change")
                target = assistant.write_pipeline(files.root, body.path, body.yaml, body.files)
                history.record(files.root, f"assistant: {body.path}")
        except ValidationError as e:
            raise HTTPException(422, str(e)) from None
        return {"name": files.name_of(target), "issues": issues}

    @router.get("/assistant/conversations")
    def list_conversations(q: str = "") -> dict[str, Any]:
        return {
            "conversations": conversations.list_all(q),
            "archive_days": conversations.ARCHIVE_DAYS,
        }

    @router.get("/assistant/conversations/{conv_id}")
    def get_conversation(conv_id: str) -> dict[str, Any]:
        c = conversations.get(conv_id)
        if c is None:
            raise HTTPException(404, f"no conversation {conv_id!r}")
        return c | {"archived": conversations.is_archived(c)}

    @router.put("/assistant/conversations/{conv_id}")
    def put_conversation(conv_id: str, body: ConversationBody) -> dict[str, Any]:
        try:
            c = conversations.save(conv_id, body.messages)
        except ValidationError as e:
            raise HTTPException(422, str(e)) from None
        return conversations.summary(c)

    @router.post("/assistant/conversations/{conv_id}/{op}")
    def archive_conversation(conv_id: str, op: str) -> dict[str, Any]:
        if op not in ("archive", "unarchive"):
            raise HTTPException(404, f"unknown operation {op!r}")
        try:
            c = conversations.set_archived(conv_id, op == "archive")
        except KeyError:
            raise HTTPException(404, f"no conversation {conv_id!r}") from None
        return conversations.summary(c)

    return router


def create_attachments_router() -> APIRouter:
    """GET /assistant/attachments/{id} without the bearer token, so <img> tags can show them:
    ids are 128-bit content hashes (unguessable), and only images are ever stored."""
    router = APIRouter()

    @router.get("/assistant/attachments/{att_id}")
    def get_attachment(att_id: str) -> FileResponse:
        path = assistant.attachment_path(att_id)
        if path is None:
            raise HTTPException(404, "no such attachment")
        return FileResponse(path, headers={"cache-control": "private, max-age=86400"})

    return router
