# Phase 51: built-in Chat harness fixes from App reports

Two `chat-harness` App reports, both from free OpenRouter models:

- **`unparseable: read_skill`** (`openrouter/free`): OpenRouter answered 200 with
  `finish_reason: "error"` (the upstream model failed mid-reply). The OpenAI SDK rejects that
  value, so Pydantic AI raised `UnexpectedModelBehavior` and it was reported as a harness bug.
  It's the provider's failure: `_provider_failed` turns it into a user-side error ("try again,
  or pick another model"), shown in the Chat and not reported.
- **`unparseable: no tools`** (`nvidia/nemotron-3.5-super-vl:free`): the reply's pipeline
  action failed a check, the check's retry used the only output retry, and the model's next
  answer didn't call `reply`, so the whole turn was lost. Now `OUTPUT_RETRIES = 2` (the checks'
  retry plus one for a reply that doesn't parse), and if retries still run out after a reply
  reached the checks, that reply is kept (its actions show their issues) and reported as
  `invalid`, not `unparseable`.

Tests: `tests/ai/test_chat_harness.py` (FakeLLM dict replies may set `finish_reason`).
