"""
🎪 Circus tent label color scheme and definitions

Centralized color map for all GitHub labels with descriptions.
"""

# Color Palette - Bright Yellow Circus Theme
COLORS = {
    # Theme Colors
    "circus_yellow": "FFD700",  # Bright yellow - primary circus theme
    "metadata_yellow": "FFF9C4",  # Light yellow - metadata labels
    # Status Colors (Semantic)
    "status_running": "28a745",  # Green - healthy/running
    "status_building": "FFD700",  # Bright yellow - in progress
    "status_failed": "dc3545",  # Red - error/failed
    "status_updating": "fd7e14",  # Orange - updating/transitioning
}

# Label Definitions with Colors and Descriptions
LABEL_DEFINITIONS = {
    # Action/Trigger Labels (Bright Yellow - User-facing, namespaced)
    "🎪 ⚡ showtime-trigger-start": {
        "color": COLORS["circus_yellow"],
        "description": "Create new ephemeral environment for this PR",
    },
    "🎪 🛑 showtime-trigger-stop": {
        "color": COLORS["circus_yellow"],
        "description": "Destroy ephemeral environment and clean up AWS resources",
    },
    "🎪 🧊 showtime-freeze": {
        "color": "FFE4B5",  # Light orange
        "description": "Freeze PR - prevent auto-sync on new commits",
    },
    "🎪 🔒 showtime-blocked": {
        "color": "dc3545",  # Red - blocking/danger
        "description": "Block all Showtime operations - maintenance mode",
    },
    # TTL Override Labels (PR-level, reusable)
    "🎪 ⌛ 24h": {
        "color": "FFE4B5",  # Light orange
        "description": "Environment expires after 24 hours",
    },
    "🎪 ⌛ 48h": {
        "color": "FFE4B5",  # Light orange
        "description": "Environment expires after 48 hours (default)",
    },
    "🎪 ⌛ 72h": {
        "color": "FFE4B5",  # Light orange
        "description": "Environment expires after 72 hours",
    },
    "🎪 ⌛ 1w": {
        "color": "FFE4B5",  # Light orange
        "description": "Environment expires after 1 week",
    },
    "🎪 ⌛ close": {
        "color": "FFE4B5",  # Light orange
        "description": "Environment expires only when PR is closed",
    },
}

# Status-specific label patterns (generated dynamically)
STATUS_LABEL_COLORS = {
    "running": COLORS["status_running"],  # 🎪 abc123f 🚦 running
    "building": COLORS["status_building"],  # 🎪 abc123f 🚦 building
    "failed": COLORS["status_failed"],  # 🎪 abc123f 🚦 failed
    "updating": COLORS["status_updating"],  # 🎪 abc123f 🚦 updating
}

# Metadata label color (for all other circus tent labels)
METADATA_LABEL_COLOR = COLORS["metadata_yellow"]  # 🎪 abc123f 📅 ..., 🎪 abc123f 🌐 ..., etc.


def get_label_color(label_text: str) -> str:
    """Get appropriate color for any circus tent label"""

    # Check for exact matches in definitions
    if label_text in LABEL_DEFINITIONS:
        return LABEL_DEFINITIONS[label_text]["color"]

    # Check for status labels with dynamic SHA
    if " 🚦 " in label_text:
        status = label_text.split(" 🚦 ")[-1]
        return STATUS_LABEL_COLORS.get(status, COLORS["circus_yellow"])

    # All other metadata labels (timestamps, IPs, TTL, users, pointers)
    if label_text.startswith("🎪 "):
        return METADATA_LABEL_COLOR

    # Fallback
    return COLORS["circus_yellow"]


def get_label_description(label_text: str) -> str:
    """Get appropriate description for any circus tent label"""

    # Check for exact matches
    if label_text in LABEL_DEFINITIONS:
        return LABEL_DEFINITIONS[label_text]["description"]

    # Dynamic descriptions for SHA-based labels
    if " 🚦 " in label_text:
        sha, status = label_text.replace("🎪 ", "").split(" 🚦 ")
        return f"Environment {sha} status: {status}"

    if " 📅 " in label_text:
        sha, timestamp = label_text.replace("🎪 ", "").split(" 📅 ")
        return f"Environment {sha} created at {timestamp}"

    if " 🌐 " in label_text:
        sha, url = label_text.replace("🎪 ", "").split(" 🌐 ")
        return f"Environment {sha} URL: http://{url} (click to visit)"

    if " ⌛ " in label_text:
        parts = label_text.replace("🎪 ", "").split(" ⌛ ")
        if len(parts) == 1:
            # PR-level TTL: "🎪 ⌛ 1w" -> parts = ["1w"] after split
            ttl = parts[0]
            return f"Environment expires after {ttl}"
        else:
            # Per-SHA TTL (legacy): "🎪 abc123f ⌛ 1w"
            sha, ttl = parts
            return f"Environment {sha} expires after {ttl}"

    if " 🤡 " in label_text:
        sha, user = label_text.replace("🎪 ", "").split(" 🤡 ")
        return f"Environment {sha} requested by {user}"

    if "🎪 🎯 " in label_text:
        sha = label_text.replace("🎪 🎯 ", "")
        return f"Active environment pointer - {sha} is receiving traffic"

    if "🎪 🏗️ " in label_text:
        sha = label_text.replace("🎪 🏗️ ", "")
        return f"Building environment - {sha} deployment in progress"

    # Fallback
    return "Circus tent showtime label"
