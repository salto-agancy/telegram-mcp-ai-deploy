---
name: telegram-reply
description: Draft and, only after explicit human confirmation, send or edit a Telegram message. Use when the user asks to reply, send, edit, or post through Telegram.
---

# Telegram reply

1. Read the smallest relevant conversation/thread and verify chat ID and reply target.
2. Draft the exact message first. Preserve source facts; do not invent dates, sums, or IDs.
3. Show destination, reply/message ID, attachments, and full final text. Ask for explicit
   confirmation immediately before the tool call.
4. Without confirmation, stop at the draft. Approval for one message does not authorize
   another destination, edit, attachment, or follow-up.
5. After approval, prefer `send_message`; use `send_rich_message` only for native tables or
   structured layouts and only in chats shared with that bot. Use `edit_message` only when
   the exact existing message ID was verified.
6. Report the returned message ID. Never enable raw MTProto to bypass a denied write.
