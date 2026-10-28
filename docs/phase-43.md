# Phase 43: pick the Chat's model in the Chat panel

User request: choose the harness **and** the model in the Chat, with only models that work
with the chosen harness (the built-in harness can't use the Claude login's models).

- The Chat panel bar has **Harness** and **Model** selects (`web/src/components/Assistant.tsx`).
  The Model list is `compatible_models[harness]` from the server: the models that harness's
  kind can use (claude-code: Anthropic only, plus the Claude login; builtin, codex, aider: API
  sources, never the Claude login) **and** whose source is ready now. A saved model that no
  longer fits stays visible, marked "not supported by …" (`chatModelOptions`).
- The first entry says what runs when nothing is picked (`chatDefaultModelLabel`): with the
  harness on default, the model saved under "Chat uses"; else the harness's own default model,
  marked "(not ready)" when its source isn't; "pick a model" for a harness that needs one.
- Switching the harness drops a model the new one can't use (`modelAfterHarnessChange`).
- The pick is remembered in the browser (`agentline.assistant.model`) and sent as `model` with
  each turn; the engine still refuses a model the harness can't use (422).

Tests: `web/src/lib/ai.test.ts` (labels, builtin never lists the Claude login's models),
`tests/ai/test_chat_harness.py::test_a_model_the_harness_cant_use_is_refused`.
