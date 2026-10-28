# Phase 33: images in the Chat

User report: attaching an image and asking the Chat to build "a pipeline like this" got
"I can't see an attached image. Only your text came through".

## Cause (two bugs)
1. `web/src/lib/assistant.ts toHistory()` sent user messages as `{role, content}` only, so the
   attachment ids never reached the server and the prompt never mentioned the image. The
   upload itself worked (the image was saved in `<home>/assistant/attachments/`).
2. Claude Code reads only inside its working folder (`<home>/assistant/workspace`), and the
   images were saved outside it. `assistant.stage_attachments` now copies them into
   `workspace/attachments/` for the turn.

Checked end to end in the browser with a stand-in `claude` that reports whether each image is
readable from its own folder. Not tried against the real model.

## Images for agents that can't see them (option A)
- `agentline/vision.py`. For a Chat agent other than claude-code, each attached image is
  transcribed once by a vision model (prompt `vision.PROMPT`: TITLE, STEPS, ARROWS, GROUPS,
  OTHER TEXT, UNCLEAR; never guess a label). The turn's prompt carries the transcript
  instead of the image. The transcription cost counts in the turn.
- Who transcribes is "Images" under Chat uses (`setup.json` `vision`; `GET/PUT
  /api/setup/vision`; the Chat action `chat_vision`):
  - `@auto`: Claude Code if the CLI is installed, else the first Anthropic API model whose key
    is set;
  - `@claude-code`: Claude Code reads the image in `<home>/assistant/vision/`;
  - a `[models]` name: the `llm` runner, which now attaches `params.images` (Anthropic image
    blocks, OpenAI-compatible `image_url` data parts; base64 is kept out of stored
    transcripts).

  The ids start with `@` because a built-in model is named `auto`.
- Cache: `<home>/assistant/transcripts/<image id>.json`. `PUT /api/assistant/transcripts/{id}`
  saves the user's correction, and an edit wins. `POST .../redo` reads the image again.
- With no transcriber available, the Chat answers 422 and says to pick one under Images.
- UI: a transcript card in the Chat (thumbnail, who read it, the text, Correct it / Read
  again), and the Images select in the Chat uses bar.

## Checks
- `tests/ai/test_vision.py`: auto pick, cache, correction, redo, no-model 422, Claude Code's
  folder, runners attaching images.
- Assistant and parity tests; vitest (`toHistory` keeps attachments, the transcript byline).
- Browser: the Images menu, and the card with a stubbed Chat reply plus a real correction
  saved to the cache.
