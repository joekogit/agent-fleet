"""Timestamp normalization. Everything in this program is epoch seconds (float)."""
from datetime import datetime, timezone


def iso_to_epoch(value):
    """Parse a transcript ISO-8601 timestamp ('2026-08-06T17:22:10.050Z')."""
    if not value or not isinstance(value, str):
        return None
    try:
        # fromisoformat handles '+00:00' but not the 'Z' suffix before 3.11
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def ms_to_epoch(value):
    """Convert session-JSON epoch milliseconds to epoch seconds."""
    if value is None or not isinstance(value, (int, float)):
        return None
    return value / 1000.0


def utc_now():
    return datetime.now(timezone.utc).timestamp()
