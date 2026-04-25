"""Shared utilities used across multiple modules."""
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def parse_json(text: str) -> Any:
    """Extract and parse the first JSON object or array from a string."""
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = text.find(open_ch)
        end = text.rfind(close_ch) + 1
        if start >= 0 and end > start:
            return json.loads(text[start:end])
    raise ValueError(f"No JSON found in response: {text[:300]}")
