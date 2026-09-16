"""Value objects returned by an archive backend.

The archive is a *source-local* projection of Telegram: chats, messages, voice
transcripts, attachment metadata and enrichment produced by a deterministic
background collector. It deliberately knows nothing about people, projects or
obligations — that interpretation belongs to a layer above this gateway.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

EnrichmentStatus = Literal["ready", "pending", "failed", "skipped", "absent"]


@dataclass(slots=True)
class ArchiveMedia:
    """Attachment facts for one message, without the binary itself."""

    kind: str | None = None
    file_name: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    file_reference: str | None = None
    duration_seconds: int | None = None
    # Voice / round video speech-to-text.
    transcription_status: EnrichmentStatus = "absent"
    transcription: str | None = None
    # Text recognised inside an image or screenshot.
    media_text_status: EnrichmentStatus = "absent"
    media_text: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind}
        for key, value in (
            ("file_name", self.file_name),
            ("mime_type", self.mime_type),
            ("size_bytes", self.size_bytes),
            ("file_reference", self.file_reference),
            ("duration_seconds", self.duration_seconds),
        ):
            if value is not None:
                out[key] = value
        if self.transcription_status != "absent":
            out["transcription_status"] = self.transcription_status
            if self.transcription is not None:
                out["transcription"] = self.transcription
        if self.media_text_status != "absent":
            out["media_text_status"] = self.media_text_status
            if self.media_text is not None:
                out["media_text"] = self.media_text
        return out


@dataclass(slots=True)
class ArchiveMessage:
    """One archived Telegram message."""

    id: int
    date: str
    account: str
    chat_id: int
    sender_id: int | None = None
    is_outgoing: bool | None = None
    is_service: bool = False
    reply_to_msg_id: int | None = None
    edit_date: str | None = None
    text: str | None = None
    media: ArchiveMedia | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "date": self.date}
        if self.is_outgoing is not None:
            out["is_outgoing"] = self.is_outgoing
        if self.sender_id is not None:
            out["sender_id"] = self.sender_id
        if self.reply_to_msg_id is not None:
            out["reply_to_msg_id"] = self.reply_to_msg_id
        if self.is_service:
            out["is_service"] = True
        if self.edit_date:
            out["edit_date"] = self.edit_date
        if self.text:
            out["text"] = self.text
        if self.media is not None:
            out["media"] = self.media.to_dict()
        return out


@dataclass(slots=True)
class ArchiveChat:
    """Archive-side facts about a chat, including how fresh its copy is."""

    account: str
    chat_id: int
    title: str | None = None
    chat_type: str | None = None
    last_synced_at: str | None = None
    last_archived_msg_id: int | None = None
    sync_scope: str = "auto"
    messages: list[ArchiveMessage] = field(default_factory=list)
    has_more: bool = False


@dataclass(slots=True)
class ArchiveSearchHit:
    """One search result with the channel that produced it."""

    message: ArchiveMessage
    chat_title: str | None = None
    chat_type: str | None = None
    rank: float = 0.0
    matched_in: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out = self.message.to_dict()
        out["account"] = self.message.account
        out["chat_id"] = self.message.chat_id
        if self.chat_title:
            out["chat_title"] = self.chat_title
        if self.chat_type:
            out["chat_type"] = self.chat_type
        if self.matched_in:
            out["matched_in"] = self.matched_in
        out["rank"] = round(self.rank, 6)
        return out
