#
# Showtime config overlay — injected during ephemeral environment Docker builds.
#
# This file is imported at the end of superset_config.py via:
#   from superset_config_docker import *
#
# It reads SUPERSET_FEATURE_* env vars and overrides FEATURE_FLAGS entries,
# giving GitHub PR labels the final say over feature flag values.
#
import os
import re

# Merge SUPERSET_FEATURE_* env vars into FEATURE_FLAGS (overriding any defaults)
_feature_env_re = re.compile(r"^SUPERSET_FEATURE_(\w+)$")

for _key, _val in os.environ.items():
    _m = _feature_env_re.match(_key)
    if _m:
        _flag_name = _m.group(1)
        FEATURE_FLAGS[_flag_name] = _val.lower() in ("true", "1", "yes", "on", "t", "y")  # noqa: F821
