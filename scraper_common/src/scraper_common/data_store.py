"""
Centralized loader for scraper_common/data/*.json files.

All JSON files in the data directory are read once on first access and
cached for the lifetime of the process.  Consumers call get(name) where
name is the filename stem (no .json extension).
"""

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("scraper_common.data_store")

_DATA_DIR = Path(__file__).parent.parent.parent / "data"
_store: dict[str, Any] = {}
_loaded: bool = False


def _load_all() -> None:
    global _loaded
    if _loaded:
        return
    logger.info("Loading interaction data from %s", _DATA_DIR)
    for path in sorted(_DATA_DIR.glob("*.json")):
        with open(path) as f:
            _store[path.stem] = json.load(f)
    _loaded = True
    logger.info(
        "Loaded %d data files: %s",
        len(_store),
        ", ".join(sorted(_store)),
    )


def get(name: str) -> Any:
    """Return the parsed JSON for *name*.json from the data directory."""
    _load_all()
    return _store[name]
