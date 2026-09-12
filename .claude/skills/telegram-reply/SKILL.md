---
name: telegram-reply
description: Draft and, only after explicit human confirmation, send or edit a Telegram message.
---

Verify destination/IDs, show the complete draft and attachments, then require immediate
explicit confirmation for one exact action. Prefer `send_message`; use rich/edit tools only
when justified and verified. Report the result ID. Never bypass ACL with raw MTProto.
