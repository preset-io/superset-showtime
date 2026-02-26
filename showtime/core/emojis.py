"""
🎪 Emoji mappings and constants for circus tent state management

Central place for all emoji meanings and mappings.
"""

# Core circus tent emoji
CIRCUS_PREFIX = "🎪"

# Meta emoji dictionary
EMOJI_MEANINGS = {
    # Status indicators
    "🚦": "status",  # Traffic light for environment status
    "🏗️": "building",  # Construction for building environments
    "🎯": "active",  # Target for currently active environment
    "🔒": "blocked",  # Lock for blocking all operations
    # Metadata
    "📅": "created_at",  # Calendar for creation timestamp
    "🌐": "ip",  # Globe for IP address
    "⌛": "ttl",  # Hourglass for time-to-live
    "🤡": "requested_by",  # Clown for who requested (circus theme!)
    "🚩": "feature_flag",  # Flag for Superset FEATURE_FLAGS
}

# Reverse mapping for creating labels
MEANING_TO_EMOJI = {v: k for k, v in EMOJI_MEANINGS.items()}

# Status display emojis (for CLI output)
STATUS_DISPLAY = {
    "building": "🏗️",
    "running": "🟢",
    "updating": "🔄",
    "failed": "❌",
    "stopping": "🛑",
}


def create_circus_label(emoji_key: str, value: str) -> str:
    """Create a circus tent label with proper spacing"""
    emoji = MEANING_TO_EMOJI.get(emoji_key)
    if not emoji:
        raise ValueError(f"Unknown emoji key: {emoji_key}")

    return f"{CIRCUS_PREFIX} {emoji} {value}"


def parse_circus_label(label: str) -> tuple[str, str]:
    """
    Parse a circus tent label into emoji meaning and value

    Args:
        label: Label like "🎪 🚦 running"

    Returns:
        Tuple of (meaning, value) like ("status", "running")

    Raises:
        ValueError: If not a valid circus label
    """
    if not label.startswith(f"{CIRCUS_PREFIX} "):
        raise ValueError(f"Not a circus label: {label}")

    parts = label.split(" ", 2)
    if len(parts) < 3:
        raise ValueError(f"Invalid circus label format: {label}")

    emoji, value = parts[1], parts[2]
    meaning = EMOJI_MEANINGS.get(emoji)

    if not meaning:
        raise ValueError(f"Unknown circus emoji: {emoji}")

    return meaning, value


def is_circus_label(label: str) -> bool:
    """Check if label is a circus tent label"""
    return label.startswith(f"{CIRCUS_PREFIX} ")
