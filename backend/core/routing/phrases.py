"""Source-controlled routing phrases.

Generated cache files are disposable. Put high-value curated examples under
`core/routing/utterances/` so deleting `.cache/utterances` does not change the
voice-critical router behavior.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_CURATED_DIR = Path(__file__).with_name("utterances")


def load_curated_utterances(plugin_name: str) -> list[str]:
    """Load curated utterances for a plugin, if present."""
    path = _CURATED_DIR / f"{plugin_name}.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text())
    except Exception as exc:
        logger.warning("Failed to read curated utterances for %s: %s", plugin_name, exc)
        return []

    utterances = payload.get("utterances")
    if not isinstance(utterances, list):
        logger.warning("Curated utterances for %s must contain an utterances list", plugin_name)
        return []
    return [item for item in utterances if isinstance(item, str) and item.strip()]
