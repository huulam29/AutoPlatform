"""Chat links: talk to agentline from a messaging app (Telegram first).

- `state.py`   the link (which chat/user is yours), pairing codes, what was already sent
- `telegram.py` a small Bot API client (stdlib HTTP, long polling: no public URL needed)
- `service.py`  the loop run by `agentline serve`: commands, approvals, questions, reports,
                and free text to the editor assistant
- `steps.py`    pipeline steps: action `chat.send`, waiter `chat.ask`
"""
