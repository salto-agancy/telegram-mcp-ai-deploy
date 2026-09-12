---
name: telegram-reply
description: Draft and, only after explicit human confirmation, send or edit a Telegram message. Use when the user asks to reply, send, edit, or post through Telegram.
---

# Telegram reply

Verify the destination and thread, draft the complete final message, and show destination,
IDs, attachments, and text. Require explicit confirmation immediately before the tool call;
one approval covers one exact action only. Prefer `send_message`, reserve
`send_rich_message` for structured layout, and verify IDs before `edit_message`. Report the
returned ID. Never enable raw MTProto to bypass a denial.
