"""Date and time utilities for consistent timestamp handling."""

from datetime import datetime
from typing import Optional

# Custom timestamp format used in circus labels
# Format: YYYY-MM-DDTHH-MM (using dashes instead of colons for GitHub label compatibility)
CIRCUS_TIME_FORMAT = "%Y-%m-%dT%H-%M"


def format_utc_now() -> str:
    """Get current UTC time formatted for circus labels."""
    return datetime.utcnow().strftime(CIRCUS_TIME_FORMAT)


def parse_circus_time(timestamp: str) -> Optional[datetime]:
    """Parse a circus timestamp string into a datetime object.

    Args:
        timestamp: String in format "YYYY-MM-DDTHH-MM"

    Returns:
        datetime object or None if parsing fails
    """
    if not timestamp:
        return None

    try:
        return datetime.strptime(timestamp, CIRCUS_TIME_FORMAT)
    except (ValueError, AttributeError):
        return None


def age_display(created_at: str) -> str:
    """Convert a circus timestamp to human-readable age.

    Args:
        created_at: Timestamp string in circus format

    Returns:
        Human-readable age like "2d 5h" or "45m"
    """
    created_dt = parse_circus_time(created_at)
    if not created_dt:
        return "-"

    # Compare UTC to UTC for accurate age
    age = datetime.utcnow() - created_dt

    # Format age nicely
    days = age.days
    hours = age.seconds // 3600
    minutes = (age.seconds % 3600) // 60

    if days > 0:
        return f"{days}d {hours}h"
    elif hours > 0:
        return f"{hours}h {minutes}m"
    else:
        return f"{minutes}m"


def is_expired(created_at: str, max_age_hours: int) -> bool:
    """Check if a timestamp is older than the specified hours.

    Args:
        created_at: Timestamp string in circus format
        max_age_hours: Maximum age in hours

    Returns:
        True if timestamp is older than max_age_hours
    """
    created_dt = parse_circus_time(created_at)
    if not created_dt:
        return False

    from datetime import timedelta

    expiry_time = created_dt + timedelta(hours=max_age_hours)
    return datetime.utcnow() > expiry_time


def ttl_to_hours(ttl: str) -> Optional[int]:
    """Convert a TTL string to hours.

    Supports formats:
        - "24h", "48h", "72h" - hours
        - "1d", "2d", "7d" - days
        - "1w" - weeks
        - "close" - returns None (never expires automatically, only on PR close)

    Args:
        ttl: TTL string like "48h", "1w", "close"

    Returns:
        Number of hours, or None if TTL means "never expire" (e.g., "close")
    """
    if not ttl:
        return None

    ttl = ttl.strip().lower()

    # "close" means expire only when PR closes, not time-based
    if ttl == "close":
        return None

    import re

    match = re.match(r"^(\d+)([hdw])$", ttl)
    if not match:
        return None

    value = int(match.group(1))
    unit = match.group(2)

    if unit == "h":
        return value
    elif unit == "d":
        return value * 24
    elif unit == "w":
        return value * 24 * 7

    return None
