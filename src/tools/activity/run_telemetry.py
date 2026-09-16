"""Per-run retrieval telemetry.

Measures one user-visible retrieval as a whole rather than one tool call at a
time: how much of the answer came from the archive, how much needed live
Telegram, how stale the archived part was, and where the time went.

Nothing here records content. Message text, transcripts, chat titles, usernames,
phone numbers and identifiers stay out by construction — the record holds counts,
durations and status labels only, so it is safe to log and safe to publish in a
benchmark.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RetrievalRun:
    """Counters for a single retrieval run."""

    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    started_at: float = field(default_factory=time.monotonic)

    telegram_rpc_calls: int = 0
    archive_queries: int = 0

    chats_discovered: int = 0
    chats_inspected: int = 0
    chats_from_archive: int = 0
    chats_from_live: int = 0
    chats_stale_topped_up: int = 0

    messages_returned: int = 0
    messages_from_archive: int = 0
    messages_from_live: int = 0

    voice_ready: int = 0
    voice_pending: int = 0
    voice_failed: int = 0
    media_text_ready: int = 0
    media_text_pending: int = 0

    transcriptions_run_in_request: int = 0
    flood_wait_events: int = 0
    flood_wait_seconds: int = 0
    errors: int = 0

    archive_lag_seconds_max: int | None = None
    phase_seconds: dict[str, float] = field(default_factory=dict)

    @contextmanager
    def phase(self, name: str):
        """Time one named stage of the run."""
        start = time.monotonic()
        try:
            yield
        finally:
            elapsed = time.monotonic() - start
            self.phase_seconds[name] = round(
                self.phase_seconds.get(name, 0.0) + elapsed, 4
            )

    def note_media(self, media: Any) -> None:
        """Fold one message's enrichment status into the counters."""
        if media is None:
            return
        status = getattr(media, "transcription_status", "absent")
        if status == "ready":
            self.voice_ready += 1
        elif status == "pending":
            self.voice_pending += 1
        elif status == "failed":
            self.voice_failed += 1

        media_status = getattr(media, "media_text_status", "absent")
        if media_status == "ready":
            self.media_text_ready += 1
        elif media_status == "pending":
            self.media_text_pending += 1

    def to_dict(self) -> dict[str, Any]:
        total = round(time.monotonic() - self.started_at, 4)
        record: dict[str, Any] = {
            "run_id": self.run_id,
            "total_seconds": total,
            "telegram_rpc_calls": self.telegram_rpc_calls,
            "archive_queries": self.archive_queries,
            "chats_discovered": self.chats_discovered,
            "chats_inspected": self.chats_inspected,
            "chats_from_archive": self.chats_from_archive,
            "chats_from_live": self.chats_from_live,
            "messages_returned": self.messages_returned,
            "messages_from_archive": self.messages_from_archive,
            "messages_from_live": self.messages_from_live,
            "voice": {
                "ready": self.voice_ready,
                "pending": self.voice_pending,
                "failed": self.voice_failed,
                "transcribed_in_request": self.transcriptions_run_in_request,
            },
            "media_text": {
                "ready": self.media_text_ready,
                "pending": self.media_text_pending,
            },
            "errors": self.errors,
        }
        if self.chats_stale_topped_up:
            record["chats_stale_topped_up"] = self.chats_stale_topped_up
        if self.flood_wait_events:
            record["flood_wait"] = {
                "events": self.flood_wait_events,
                "seconds": self.flood_wait_seconds,
            }
        if self.archive_lag_seconds_max is not None:
            record["archive_lag_seconds_max"] = self.archive_lag_seconds_max
        if self.phase_seconds:
            record["phase_seconds"] = dict(self.phase_seconds)
        return record

    def log(self) -> None:
        logger.info("retrieval run %s: %s", self.run_id, self.to_dict())
