"""PostgreSQL archive backend.

Expects the schema documented in ``docs/ARCHIVE.md``: a ``chats`` table and a
``messages`` table maintained by a deterministic background collector, plus the
``search_tsv`` index that covers text, voice transcript, attachment name and
recognised image text.

Dates are stored as fixed-width ISO-8601 UTC strings (``2026-09-16T17:17:16+00:00``),
so lexicographic comparison is equivalent to temporal comparison and the existing
``(chat_id, date DESC)`` and ``(account, date DESC)`` btree indexes stay usable.
Casting to ``timestamptz`` inside the predicate would be more obvious and would
also discard those indexes, so callers normalise to that exact string instead.
"""

from __future__ import annotations

import logging
from typing import Any

from src.archive.models import (
    ArchiveChat,
    ArchiveMedia,
    ArchiveMessage,
    ArchiveSearchHit,
)

logger = logging.getLogger(__name__)

_VOICE_KINDS = ("voice", "round", "audio")

# Message columns every query selects, in one place so row mapping stays in sync.
_MESSAGE_COLUMNS = """
    m.msg_id, m.chat_id, m.account, m.from_id, m.date, m.text,
    m.media_type, m.voice_duration, m.voice_transcription, m.voice_file_id,
    m.file_name, m.file_mime, m.file_size, m.media_file_id,
    m.media_text, m.media_text_status,
    m.reply_to_msg_id, m.is_outgoing, m.is_service, m.edit_date
"""


def _enrichment_status(value: str | None, *, has_source: bool) -> str:
    """Map a stored status to the contract vocabulary.

    A missing status on a message that *does* carry the media means the
    background worker has not reached it yet — ``pending``, never a silent
    ``absent``. Claiming there is no voice because nobody transcribed it is the
    exact failure this contract exists to prevent.
    """
    if not has_source:
        return "absent"
    if value in ("ready", "pending", "failed", "skipped"):
        return value
    return "pending"


def _media_from_row(row: dict[str, Any]) -> ArchiveMedia | None:
    kind = row.get("media_type")
    has_file = bool(row.get("file_name") or row.get("media_file_id"))
    if not kind and not has_file:
        return None

    is_voice = kind in _VOICE_KINDS
    transcription = row.get("voice_transcription")
    transcription_status = (
        "ready"
        if transcription
        else _enrichment_status(None, has_source=bool(is_voice or row.get("voice_file_id")))
    )

    media_text = row.get("media_text")
    media_text_status = _enrichment_status(
        row.get("media_text_status"),
        has_source=kind in ("photo", "image", "document") and bool(row.get("media_file_id")),
    )
    if media_text and media_text_status != "ready":
        media_text_status = "ready"

    return ArchiveMedia(
        kind=kind,
        file_name=row.get("file_name"),
        mime_type=row.get("file_mime"),
        size_bytes=row.get("file_size"),
        file_reference=row.get("media_file_id") or row.get("voice_file_id"),
        duration_seconds=row.get("voice_duration"),
        transcription_status=transcription_status,  # type: ignore[arg-type]
        transcription=transcription,
        media_text_status=media_text_status,  # type: ignore[arg-type]
        media_text=media_text,
    )


def _message_from_row(row: dict[str, Any]) -> ArchiveMessage:
    outgoing = row.get("is_outgoing")
    return ArchiveMessage(
        id=row["msg_id"],
        date=row["date"],
        account=row["account"],
        chat_id=row["chat_id"],
        sender_id=row.get("from_id"),
        is_outgoing=None if outgoing is None else bool(outgoing),
        is_service=bool(row.get("is_service")),
        reply_to_msg_id=row.get("reply_to_msg_id"),
        edit_date=row.get("edit_date"),
        text=row.get("text") or None,
        media=_media_from_row(row),
    )


class PostgresArchiveBackend:
    """Async archive backend over asyncpg.

    Opened lazily so a deployment without the optional dependency, or without a
    configured DSN, degrades to live-only retrieval instead of failing at import.
    """

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 4,
                 statement_timeout_ms: int = 15_000) -> None:
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._statement_timeout_ms = statement_timeout_ms
        self._pool: Any = None

    # ── lifecycle ───────────────────────────────────────────────────────────

    async def _get_pool(self):
        if self._pool is None:
            try:
                import asyncpg
            except ImportError as exc:  # pragma: no cover - depends on install extras
                raise RuntimeError(
                    "archive backend requires the 'archive' extra (asyncpg)"
                ) from exc
            self._pool = await asyncpg.create_pool(
                self._dsn,
                min_size=self._min_size,
                max_size=self._max_size,
                command_timeout=self._statement_timeout_ms / 1000,
                server_settings={"statement_timeout": str(self._statement_timeout_ms)},
            )
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    # ── introspection ───────────────────────────────────────────────────────

    async def health(self) -> dict[str, object]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            has_search_tsv = await conn.fetchval(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'messages' AND column_name = 'search_tsv'"
            )
            freshest = await conn.fetchval("SELECT max(last_sync_at) FROM chats")
            chats = await conn.fetchval("SELECT count(*) FROM chats WHERE keep_synced = 1")
            return {
                "reachable": True,
                "search_index_ready": bool(has_search_tsv),
                "last_sync_at": freshest,
                "chats_tracked": chats,
            }

    async def accounts(self) -> list[str]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT DISTINCT account FROM chats ORDER BY account")
            return [r["account"] for r in rows]

    # ── retrieval ───────────────────────────────────────────────────────────

    async def recent_chats(
        self,
        *,
        account: str,
        since: str,
        until: str | None = None,
        chat_ids: list[int] | None = None,
        messages_per_chat: int = 20,
    ) -> list[ArchiveChat]:
        """Every chat with activity in the window plus its recent messages.

        Deliberately one round trip: the window function slices per chat inside
        the database, so the caller never loops chat by chat.
        """
        pool = await self._get_pool()
        sql = f"""
            WITH windowed AS (
                SELECT {_MESSAGE_COLUMNS},
                       row_number() OVER (
                           PARTITION BY m.chat_id ORDER BY m.date DESC, m.msg_id DESC
                       ) AS rn,
                       count(*) OVER (PARTITION BY m.chat_id) AS in_window
                FROM messages m
                WHERE m.account = $1
                  AND m.date >= $2
                  AND ($3::text IS NULL OR m.date <= $3)
                  AND ($4::bigint[] IS NULL OR m.chat_id = ANY($4))
            )
            SELECT w.*, c.peer_name, c.chat_type, c.last_sync_at,
                   c.last_msg_id, c.sync_scope
            FROM windowed w
            JOIN chats c ON c.account = w.account AND c.chat_id = w.chat_id
            WHERE w.rn <= $5
            ORDER BY w.date DESC, w.chat_id, w.msg_id DESC
        """
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                sql, account, since, until, chat_ids, messages_per_chat
            )

        by_chat: dict[int, ArchiveChat] = {}
        for raw in rows:
            row = dict(raw)
            cid = row["chat_id"]
            chat = by_chat.get(cid)
            if chat is None:
                chat = ArchiveChat(
                    account=account,
                    chat_id=cid,
                    title=row.get("peer_name"),
                    chat_type=row.get("chat_type"),
                    last_synced_at=row.get("last_sync_at"),
                    last_archived_msg_id=row.get("last_msg_id"),
                    sync_scope=row.get("sync_scope") or "auto",
                    has_more=row["in_window"] > messages_per_chat,
                )
                by_chat[cid] = chat
            chat.messages.append(_message_from_row(row))
        return list(by_chat.values())

    async def messages_for_chats(
        self,
        *,
        account: str,
        chat_ids: list[int],
        since: str,
        until: str | None = None,
        messages_per_chat: int = 20,
    ) -> dict[int, list[ArchiveMessage]]:
        if not chat_ids:
            return {}
        chats = await self.recent_chats(
            account=account,
            since=since,
            until=until,
            chat_ids=chat_ids,
            messages_per_chat=messages_per_chat,
        )
        return {c.chat_id: c.messages for c in chats}

    async def enrichment_for_messages(
        self,
        *,
        account: str,
        chat_id: int,
        msg_ids: list[int],
    ) -> dict[int, dict[str, Any]]:
        """What the background pipeline already extracted from these messages.

        Answers one question: is there text for this attachment yet. The caller
        needs it before deciding between "here is what the screenshot says" and
        "nothing has read it yet" — the two are indistinguishable otherwise, and
        the second was being reported as the first.
        """
        if not msg_ids:
            return {}
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT msg_id, media_text, media_text_status,
                       voice_transcription, media_type, file_name
                FROM messages
                WHERE account = $1 AND chat_id = $2 AND msg_id = ANY($3)
                """,
                account, chat_id, msg_ids,
            )
        return {
            row["msg_id"]: {
                "media_text": row["media_text"],
                "media_text_status": row["media_text_status"],
                "voice_transcription": row["voice_transcription"],
                "media_type": row["media_type"],
                "file_name": row["file_name"],
            }
            for row in rows
        }

    async def count_matches(
        self,
        *,
        query: str,
        accounts: list[str] | None = None,
        chat_ids: list[int] | None = None,
        sender_ids: list[int] | None = None,
        since: str | None = None,
        until: str | None = None,
        media_kinds: list[str] | None = None,
        match_in: list[str] | None = None,
    ) -> int:
        """How many messages match, beyond the page being returned.

        Without it a page reads as a total. Observed: an agent asked how many
        voice messages mention payment, received the fifty it had asked for, and
        reported fifty as the answer — the archive held 242.
        """
        pool = await self._get_pool()
        sql = """
            SELECT count(*)
            FROM messages m,
                 websearch_to_tsquery('russian', $1) AS q
            WHERE m.search_tsv @@ q
              AND ($2::text[]   IS NULL OR m.account  = ANY($2))
              AND ($3::bigint[] IS NULL OR m.chat_id  = ANY($3))
              AND ($4::bigint[] IS NULL OR m.from_id  = ANY($4))
              AND ($5::text     IS NULL OR m.date    >= $5)
              AND ($6::text     IS NULL OR m.date    <= $6)
              AND ($7::text[]   IS NULL OR m.media_type = ANY($7))
              AND ($8::text[] IS NULL OR (
                    ('text' = ANY($8)
                     AND m.text IS NOT NULL
                     AND to_tsvector('russian', m.text) @@ q)
                 OR ('voice_transcription' = ANY($8)
                     AND m.voice_transcription IS NOT NULL
                     AND to_tsvector('russian', m.voice_transcription) @@ q)
                 OR ('file_name' = ANY($8)
                     AND m.file_name IS NOT NULL
                     AND to_tsvector('russian', m.file_name) @@ q)
                 OR ('media_text' = ANY($8)
                     AND m.media_text IS NOT NULL
                     AND to_tsvector('russian', m.media_text) @@ q)))
        """
        async with pool.acquire() as conn:
            return int(
                await conn.fetchval(
                    sql, query, accounts, chat_ids, sender_ids,
                    since, until, media_kinds, match_in,
                )
                or 0
            )

    # ── search ──────────────────────────────────────────────────────────────

    async def search(
        self,
        *,
        query: str,
        accounts: list[str] | None = None,
        chat_ids: list[int] | None = None,
        sender_ids: list[int] | None = None,
        since: str | None = None,
        until: str | None = None,
        media_kinds: list[str] | None = None,
        match_in: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ArchiveSearchHit]:
        """Lexical search with every structured filter inside the candidate query.

        ``matched_in`` reports which channel produced the hit — message text,
        voice transcript, attachment name or recognised image text — so a caller
        can tell "she said it out loud" from "it was written on a screenshot".

        ``match_in`` turns that report into a filter. Without it "find where they
        said it in a voice message" cannot be expressed: typed messages outnumber
        transcripts by two orders of magnitude and fill the page before a single
        transcript appears. Measured on the owner's archive: 12 051 transcripts
        existed and none reached the first fifty results for a common word.
        """
        pool = await self._get_pool()
        sql = f"""
            SELECT {_MESSAGE_COLUMNS},
                   c.peer_name, c.chat_type,
                   -- Normalisation 32 divides by (rank + 1), which stops a short
                   -- caption from outranking a long voice transcript that answers
                   -- the question. Measured on a real corpus: without it the
                   -- wanted transcript sat tenth out of twelve; with it, first.
                   ts_rank_cd(m.search_tsv, q, 32) AS rank,
                   (m.text IS NOT NULL
                    AND to_tsvector('russian', m.text) @@ q) AS hit_text,
                   (m.voice_transcription IS NOT NULL
                    AND to_tsvector('russian', m.voice_transcription) @@ q) AS hit_voice,
                   (m.file_name IS NOT NULL
                    AND to_tsvector('russian', m.file_name) @@ q) AS hit_file,
                   (m.media_text IS NOT NULL
                    AND to_tsvector('russian', m.media_text) @@ q) AS hit_media
            FROM messages m
            JOIN chats c ON c.account = m.account AND c.chat_id = m.chat_id,
                 websearch_to_tsquery('russian', $1) AS q
            WHERE m.search_tsv @@ q
              AND ($2::text[]   IS NULL OR m.account  = ANY($2))
              AND ($3::bigint[] IS NULL OR m.chat_id  = ANY($3))
              AND ($4::bigint[] IS NULL OR m.from_id  = ANY($4))
              AND ($5::text     IS NULL OR m.date    >= $5)
              AND ($6::text     IS NULL OR m.date    <= $6)
              AND ($7::text[]   IS NULL OR m.media_type = ANY($7))
              AND ($8::text[] IS NULL OR (
                    ('text' = ANY($8)
                     AND m.text IS NOT NULL
                     AND to_tsvector('russian', m.text) @@ q)
                 OR ('voice_transcription' = ANY($8)
                     AND m.voice_transcription IS NOT NULL
                     AND to_tsvector('russian', m.voice_transcription) @@ q)
                 OR ('file_name' = ANY($8)
                     AND m.file_name IS NOT NULL
                     AND to_tsvector('russian', m.file_name) @@ q)
                 OR ('media_text' = ANY($8)
                     AND m.media_text IS NOT NULL
                     AND to_tsvector('russian', m.media_text) @@ q)))
            ORDER BY ts_rank_cd(m.search_tsv, q, 32) DESC, m.date DESC
            LIMIT $9 OFFSET $10
        """
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                sql, query, accounts, chat_ids, sender_ids,
                since, until, media_kinds, match_in, limit, offset,
            )

        hits: list[ArchiveSearchHit] = []
        for raw in rows:
            row = dict(raw)
            matched = [
                name
                for name, flag in (
                    ("text", row.get("hit_text")),
                    ("voice_transcription", row.get("hit_voice")),
                    ("file_name", row.get("hit_file")),
                    ("media_text", row.get("hit_media")),
                )
                if flag
            ]
            hits.append(
                ArchiveSearchHit(
                    message=_message_from_row(row),
                    chat_title=row.get("peer_name"),
                    chat_type=row.get("chat_type"),
                    rank=float(row.get("rank") or 0.0),
                    matched_in=matched,
                )
            )
        return hits
