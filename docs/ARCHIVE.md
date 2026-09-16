# Archive backend (optional)

Disabled by default. With no `ARCHIVE_DSN` set, this gateway behaves exactly as it
always has: every read goes to live Telegram.

## What problem it solves

Answering "what happened in the last 24 hours" through live Telegram alone means
discovering chats, then opening each one — one call per chat — and waiting while
voice messages are transcribed inside the request. Measured on a working account
before this feature: 56 tool calls, 129 seconds, and 30 of those seconds spent
re-transcribing audio that had already been transcribed elsewhere.

Two further gaps have the same root. Telegram's search index only covers message
text, so a phrase that exists inside a voice message, or on a screenshot, cannot
be found at all — you can read a picture only after you have already located it.

An archive fixes all three, because the work is done once, in the background,
before anyone asks.

## What it does and does not answer

| Question | Source | Why |
|---|---|---|
| message text, sender, direction, reply links | archive | already collected |
| voice transcript | archive | computing it again costs seconds per message |
| attachment name, type, size, reference | archive | stable metadata |
| text recognised on an image | archive | produced by background enrichment |
| **unread count, read markers** | **always live** | these change the instant the owner opens a chat; a snapshot presented as current would be confidently wrong |
| chats not covered by the archive | live top-up | coverage is never assumed to be total |
| a chat whose archived copy is behind | live top-up | staleness is detected, not hoped away |

## Required schema

The backend expects two tables maintained by a deterministic collector. Column
names below are the contract; anything else in the tables is ignored.

`chats`: `chat_id`, `account`, `peer_name`, `chat_type`, `last_sync_at`,
`last_msg_id`, `keep_synced`, `sync_scope`.

`messages`: `msg_id`, `chat_id`, `account`, `from_id`, `date`, `text`,
`media_type`, `voice_file_id`, `voice_duration`, `voice_transcription`,
`file_name`, `file_mime`, `file_size`, `media_file_id`, `media_text`,
`media_text_status`, `reply_to_msg_id`, `is_outgoing`, `is_service`, `edit_date`,
`search_tsv`.

Two conventions matter:

**Dates are fixed-width ISO-8601 UTC strings** (`2026-09-16T17:17:16+00:00`, 25
characters). Lexicographic comparison is then equivalent to temporal comparison,
and the existing btree indexes stay usable. Casting to `timestamptz` inside the
predicate would read more obviously and would discard those indexes, so callers
normalise instead.

**`search_tsv` covers four channels**, not one: message text and voice transcript
at weight A, attachment name and recognised image text at weight B. A transcript
that is stored but not indexed is a transcript nobody can find.

## Enrichment status vocabulary

A message carrying voice or an image reports where its enrichment stands:

- `ready` — produced, and the text is included;
- `pending` — the background worker has not reached it yet;
- `failed` — attempted and did not succeed;
- `skipped` — looked at, nothing to extract (a photo with no text);
- `absent` — there is no such media on this message.

The distinction between `pending` and `skipped` is load-bearing. "Nobody has
looked yet" and "we looked and there was nothing" must never collapse into
silence, or a caller will read an unprocessed voice message as a message that was
never sent.

## Configuration

```bash
ARCHIVE_DSN=postgresql://user:password@host:5432/database
ARCHIVE_POOL_SIZE=4                  # pooled connections
ARCHIVE_STATEMENT_TIMEOUT_MS=15000   # a slow archive must fail into live, not hang
ARCHIVE_MAX_FRESHNESS_SECONDS=5400   # 90 min: one missed hourly collection plus margin
```

Install the optional dependency: `uv sync --extra archive` (adds `asyncpg`).

## Failure behaviour

Every archive failure degrades to live retrieval and says so in the response
(`coverage.archive_error`, or `archive_status` on a search). The gateway does not
fail a request because a convenience layer is unavailable, and it does not pretend
the archive answered when it did not.

## What belongs here and what does not

This backend stores Telegram facts. It does not decide which chat belongs to which
client, who a username really is, or what counts as an obligation — those
interpretations belong to whatever consumes this gateway. A raw sender identity is
never presented as a canonical person.
