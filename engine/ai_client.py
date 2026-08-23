"""Shared lazy Anthropic client accessor for every AI-backed layer (L3
reference repair/shortlist, L6 action drafting). Returns None (permanently,
for this process) when the SDK is missing or no credentials resolve, so
each caller falls back to its own deterministic stub without repeating
this check. A transient API error on an actual call is each caller's own
concern -- this module only handles "is there a client to try at all."
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_client = None
_client_checked = False


def get_client():
    """Lazily construct an Anthropic client. Returns None (permanently, for
    this process) if the SDK is missing or no credentials resolve."""
    global _client, _client_checked
    if _client_checked:
        return _client
    _client_checked = True
    try:
        import anthropic
        client = anthropic.Anthropic()
        # Constructing the client does not validate credentials by itself;
        # we only find out on first real call. Skip eager validation here --
        # a bad key degrades to the stub on the first failed call instead.
        if not (os.environ.get("ANTHROPIC_API_KEY")
                or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            logger.info("AI client: no API credentials found, using deterministic fallback")
            return None
        _client = client
    except Exception as exc:  # pragma: no cover - import/env issues
        logger.info("AI client: unavailable (%s), using deterministic fallback", exc)
        _client = None
    return _client
