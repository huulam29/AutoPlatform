# Phase 48: Chat history

The Chat kept one conversation in the browser's localStorage (`agentline.assistant.chat`, last
60 messages). In the Mac app that copy is easily lost (the page's origin changes with the
engine's port), and "New chat" threw the old one away. Conversations now live on the server
and the Chat panel has a History view (decision #75).

## Engine

- `conversations.py`: one JSON file per conversation in
  `$AGENTLINE_HOME/assistant/conversations/<id>.json` (next to the Chat's attachments, like
  incidents/triage/reviews; no ledger table: the Chat isn't a run): `{id, title, created_at,
  updated_at, kept_at, archived_at, messages}`. Title = the first user message's first line
  (80 chars). Written atomically (temp file + rename). Saving the same list again is not
  activity (`updated_at` stays).
- Archive rule (`is_archived`): no activity for more than `ARCHIVE_DAYS` = 7 days, where
  activity is the last message (`updated_at`) or an Unarchive (`kept_at`); or archived by hand
  (`archived_at`) with no message since. Nothing is deleted, and there is no delete endpoint.
- API (`api/assistant.py`): `GET /assistant/conversations?q=` (summaries, newest message first,
  each with `archived`; `q` searches titles and every message, archived ones included),
  `GET|PUT /assistant/conversations/{id}` (`{messages}`, the client makes the id),
  `POST /assistant/conversations/{id}/archive|unarchive`.
- Chat parity: action `conversation` `{op: archive|unarchive, id}` (`ACTION_TYPES`,
  `_check_one` against LIVE STATE `CONVERSATIONS`, the GUIDE list, `title`, `REPAIRABLE`);
  `gather_live` lists the 30 newest conversations. Open / New chat / search only change what
  the panel shows: no card (docs/phase-25.md table).

## Web

- `lib/conversations.ts`: `isArchived` (same rule as the server), `historyGroups` (recent and
  archived, each newest first), `lastActive` ("3 h ago"), `newConversationId` (not
  `crypto.randomUUID`: the Mac app's page isn't a secure context), the open conversation's id
  in localStorage (`agentline.assistant.conversation`; `""` = a new chat not saved yet).
- `components/Assistant.tsx`: on open, the old localStorage chat becomes a conversation once
  (then the key is removed); else the remembered conversation; else the newest recent one.
  Every change of the messages is PUT, one save after the other; messages just read from the
  server are not saved back. "History" and "New chat" in the bar; an archived conversation
  shows a banner with Unarchive (sending a message also brings it back).
- `components/ChatHistory.tsx`: search, the recent list, a collapsed **Archived (n)** section
  (opened while searching), Archive / Unarchive per row.
- `lib/assistant.ts`: `saveChat` is gone (`forStorage` cleans in-flight card states;
  `loadChat`/`clearOldChat` only migrate); `applyAction` handles `conversation`.

## Checked

`uv run pytest` (tests/ai/test_conversations.py, parity cases), ruff, `npm test` (vitest
`conversations.test.ts`, apply cases), `tsc`, `npm run build`, `lint_hardcodes.py`. In the
browser on a scratch data folder (`agentline-48` in `.claude/launch.json`) with seeded
conversations 3 h, 2, 10 and 40 days old: the old browser chat moved to the server; History
listed three recent and two archived; opening an archived one showed the banner; Unarchive
moved it back; a reload kept the open conversation and its applied card; search "MCP" found
the archived one; "New chat" then reload stayed on an empty chat.

Not done: a real model reply in the browser (the Chat's `conversation` card is covered by the
vitest apply cases and the engine parity tests only).
