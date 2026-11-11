"""File based feature flags: edit the JSON file and the next request sees it (chaos and deploy tooling use this)."""

import json
import os
import zlib

FLAG_FILE = os.environ.get("FLAG_FILE", "/flags/flags.json")


def enabled(name: str) -> bool:
    try:
        with open(FLAG_FILE) as f:
            return bool(json.load(f).get(name, False))
    except (OSError, ValueError):
        return False


def rollout_percent(name: str) -> int:
    """A flag may be true/false or a rollout percentage (0-100)."""
    try:
        with open(FLAG_FILE) as f:
            v = json.load(f).get(name, False)
    except (OSError, ValueError):
        return 0
    if v is True:
        return 100
    if isinstance(v, int | float) and not isinstance(v, bool):
        return max(0, min(100, int(v)))
    return 0


def in_rollout(name: str, key: str) -> bool:
    pct = rollout_percent(name)
    return pct > 0 and (zlib.crc32(key.encode()) % 100) < pct
