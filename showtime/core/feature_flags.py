"""
🎪 Feature flag utilities for circus tent label management

Parses and creates feature flag labels: 🎪 🚩 FLAG_NAME=value
Converts to AWS ECS environment variable format.
"""

import re
from typing import Dict, List, Optional, Set, Tuple

from .constants import FEATURE_FLAG_ENV_PREFIX, FEATURE_FLAG_LABEL_PREFIX

# Regex for valid flag names: uppercase letters, digits, underscores, starting with a letter
_FLAG_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def parse_feature_flag_label(label: str) -> Optional[Tuple[str, str]]:
    """Parse a feature flag label into (flag_name, value).

    Args:
        label: Label like "🎪 🚩 EMBEDDED_SUPERSET=true"

    Returns:
        Tuple of (flag_name, value) like ("EMBEDDED_SUPERSET", "true")
        or None if not a valid feature flag label.
    """
    if not label.startswith(FEATURE_FLAG_LABEL_PREFIX):
        return None

    flag_part = label[len(FEATURE_FLAG_LABEL_PREFIX) :].strip()

    if "=" not in flag_part:
        return None

    flag_name, value = flag_part.split("=", 1)
    flag_name = flag_name.strip().upper()
    value = value.strip().lower()

    if not _FLAG_NAME_RE.match(flag_name):
        return None

    if value not in ("true", "false"):
        return None

    return (flag_name, value)


def is_feature_flag_label(label: str) -> bool:
    """Check if a label is a feature flag label."""
    return label.startswith(FEATURE_FLAG_LABEL_PREFIX)


def create_feature_flag_label(flag_name: str, value: str = "true") -> str:
    """Create a feature flag label string.

    Args:
        flag_name: Flag name like "EMBEDDED_SUPERSET"
        value: "true" or "false"

    Returns:
        Label string like "🎪 🚩 EMBEDDED_SUPERSET=true"
    """
    return f"{FEATURE_FLAG_LABEL_PREFIX}{flag_name.upper()}={value.lower()}"


def extract_feature_flags_from_labels(labels: Set[str]) -> Dict[str, bool]:
    """Extract all feature flags from a set of PR labels.

    Args:
        labels: Set of all PR labels

    Returns:
        Dict mapping flag name to boolean value.
        Example: {"EMBEDDED_SUPERSET": True, "DASHBOARD_NATIVE_FILTERS": False}
    """
    flags: Dict[str, bool] = {}
    for label in labels:
        parsed = parse_feature_flag_label(label)
        if parsed:
            flag_name, value = parsed
            flags[flag_name] = value == "true"
    return flags


def feature_flags_to_aws_env(flags: Dict[str, bool]) -> List[Dict[str, str]]:
    """Convert feature flags dict to AWS ECS environment variable format.

    Args:
        flags: Dict like {"EMBEDDED_SUPERSET": True}

    Returns:
        List like [{"name": "SUPERSET_FEATURE_EMBEDDED_SUPERSET", "value": "True"}]
        This format matches what aws.create_environment() expects.
    """
    env_vars: List[Dict[str, str]] = []
    for flag_name, enabled in sorted(flags.items()):
        env_vars.append(
            {
                "name": f"{FEATURE_FLAG_ENV_PREFIX}{flag_name}",
                "value": "True" if enabled else "False",
            }
        )
    return env_vars


def feature_flags_to_prefixed_dict(flags: Dict[str, bool]) -> Dict[str, bool]:
    """Convert feature flags dict to SUPERSET_FEATURE_ prefixed dict.

    This format matches what aws.update_feature_flags() expects.

    Args:
        flags: Dict like {"EMBEDDED_SUPERSET": True}

    Returns:
        Dict like {"SUPERSET_FEATURE_EMBEDDED_SUPERSET": True}
    """
    return {f"{FEATURE_FLAG_ENV_PREFIX}{name}": enabled for name, enabled in flags.items()}
