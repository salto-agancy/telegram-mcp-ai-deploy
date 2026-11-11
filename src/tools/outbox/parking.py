"""Parking rules: pure functions, no Telegram, no database.

Owner's decisions (10.11.2026):
- a scheduled message far ahead is a parked note, not a real send: he never
  schedules a real message a year ahead;
- parking is recognised by two signs — more than six months ahead AND 23:59 Moscow
  time. Both: parking. One: "looks like parking", shown, never touched;
- the server keeps parking alive by moving its date forward (text never changes),
  because Telegram refuses anything beyond about 367 days (measured 10.11.2026);
- if it cannot, the item is evacuated to Saved Messages before Telegram sends it.
"""

from __future__ import annotations

import datetime as dt

MSK = dt.timezone(dt.timedelta(hours=3))
FAR = dt.timedelta(days=180)
# Telegram accepted +367 days and refused +368 (Saved Messages, 10.11.2026).
TARGET = dt.timedelta(days=365)
# Prolong when less than this is left: a weekly run keeps parking 330-365 days out.
PROLONG_BELOW = dt.timedelta(days=330)
# Evacuate when less than this is left: prolonging has failed for a long time.
EVACUATE_BELOW = dt.timedelta(days=14)


def classify(scheduled_for: dt.datetime, now: dt.datetime) -> str | None:
    """'parking', 'looks_like_parking' or None for a real scheduled message."""
    msk = scheduled_for.astimezone(MSK)
    far = scheduled_for - now > FAR
    at_2359 = msk.hour == 23 and msk.minute == 59
    if far and at_2359:
        return "parking"
    if far or at_2359:
        return "looks_like_parking"
    return None


def prolong_target(now: dt.datetime, order: int) -> dt.datetime:
    """New date for the order-th parked message of one chat: 23:59:<order> Moscow time,
    one year ahead. The seconds keep several parked messages of a chat in their order."""
    day = (now + TARGET).astimezone(MSK).date()
    second = max(0, min(int(order), 59))
    return dt.datetime(day.year, day.month, day.day, 23, 59, second, tzinfo=MSK)


def action(
    scheduled_for: dt.datetime, now: dt.datetime, kind: str | None
) -> str | None:
    """What the maintenance run does with one item: 'evacuate', 'prolong' or nothing.
    Only confirmed parking is ever touched."""
    if kind != "parking":
        return None
    left = scheduled_for - now
    if left < EVACUATE_BELOW:
        return "evacuate"
    if left < PROLONG_BELOW:
        return "prolong"
    return None


def evacuation_text(
    chat_title: str | None, account: str, scheduled_for: dt.datetime, text: str
) -> str:
    when = scheduled_for.astimezone(MSK).strftime("%d.%m.%Y %H:%M")
    return (
        f"⏳ Парковка · {chat_title or 'чат'} · {account} аккаунт\n"
        f"Telegram отправил бы её {when} мск; сервер не смог продлить и убрал её из чата.\n\n"
        f"{text}"
    )
