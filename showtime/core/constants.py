"""Constants used throughout the showtime codebase."""

# Default time-to-live for new environments
DEFAULT_TTL = "48h"

# TTL options for environments
TTL_OPTIONS = ["24h", "48h", "72h", "1w", "close"]

# Maximum age for considering environments stale/orphaned
DEFAULT_CLEANUP_AGE = "48h"

# Hidden HTML marker embedded in every Showtime PR comment so the bot can
# identify (and delete) its own superseded comments when posting new ones
SHOWTIME_COMMENT_MARKER = "<!-- showtime-comment -->"

# Header prefix used by every comment posted before SHOWTIME_COMMENT_MARKER
# existed, for recognizing legacy Showtime comments
LEGACY_COMMENT_PREFIX = "🎪 [Showtime](https://github.com/mistercrunch/superset-showtime)"
