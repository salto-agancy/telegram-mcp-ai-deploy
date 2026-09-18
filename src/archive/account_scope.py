"""Which archived account a session is allowed to read.

One authenticated session is one Telegram account. The archive, however, holds
every account the collector gathers, so any read of it has to say whose rows it
wants. Leaving that out does not fail loudly — it quietly answers with someone
else's messages.

Found in production: an archive search returned the same nine attachments to both
connectors, so the work account served personal correspondence and the personal
account served work. The snapshot path had this right from the start; the search
path never passed the account down.
"""

from __future__ import annotations


def pick_archive_account(
    available: list[str],
    requested: list[str],
    session_label: str,
    *,
    configured: str = "",
) -> str | None:
    """Choose which archived account this session may read, or None to refuse.

    The configured label wins, because the two naming schemes never agree on
    their own: an archive labels accounts the way its operator thinks about them
    ("personal", "work") while a session knows itself by Telegram username. A
    first live run proved the cost of relying on the accidental match — the
    archive held every message and answered none of them, and the snapshot came
    back entirely from live Telegram while reporting success.

    Returning None means "do not read the archive at all". That is the safe end
    of the trade: a live-only answer is incomplete, an answer from the wrong
    account is a leak.
    """
    if not available:
        return None
    if configured:
        return configured if configured in available else None
    for candidate in (session_label, *requested):
        if candidate in available:
            return candidate
    if len(available) == 1:
        return available[0]
    return None
