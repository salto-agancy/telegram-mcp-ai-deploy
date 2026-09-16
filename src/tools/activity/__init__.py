"""Batch activity retrieval: one prepared Telegram snapshot per call."""

from src.tools.activity.recent import recent_activity_impl

__all__ = ["recent_activity_impl"]
