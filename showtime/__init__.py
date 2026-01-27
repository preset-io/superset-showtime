"""
🎪 Superset Showtime - Smart ephemeral environment management

Circus tent emoji state tracking for Apache Superset ephemeral environments.
"""

__version__ = "0.6.11"
__author__ = "Maxime Beauchemin"
__email__ = "maximebeauchemin@gmail.com"

from .core.github import GitHubInterface
from .core.pull_request import PullRequest
from .core.show import Show

__all__ = [
    "__version__",
    "Show",
    "PullRequest",
    "GitHubInterface",
]
