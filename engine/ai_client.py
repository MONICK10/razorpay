"""Shared, provider-agnostic LLM client accessor for every AI-backed call
site in the repo (L3 reference repair/shortlist, L6 action drafting,
simulator/ask_ledger.py). Returns None (permanently, for this process) when
no credentials resolve, so each caller falls back to its own deterministic
stub without repeating this check. A transient API error on an actual call
is each caller's own concern for complete() (propagates, same as before);
complete_structured() catches its own errors and returns None, since a
failed structured call has one obvious fallback everywhere it's used:
"the model didn't answer -- go do what you'd do with no client at all."

Configuration (.env, via python-dotenv):
  LLM_PROVIDER   anthropic | openai | xai
  LLM_API_KEY
  LLM_MODEL      required for openai/xai; defaults to claude-opus-5 for
                 anthropic if unset
  LLM_BASE_URL   optional, provider=openai only -- overrides the SDK's
                 default endpoint (api.openai.com) for an OpenAI-
                 compatible provider such as Groq
                 (https://api.groq.com/openai/v1). Without it,
                 provider=openai always talks to OpenAI itself
                 regardless of whose API key is configured.
xAI's API is OpenAI-compatible (same request shape), so both go through the
openai SDK, xai always using its own fixed base_url (api.x.ai/v1).

Legacy fallback: if LLM_PROVIDER/LLM_API_KEY aren't set, falls back to the
original ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN env vars (provider=
anthropic, model=claude-opus-5) -- preserves this project's original
zero-config behavior for anyone who already had those set.
"""
from __future__ import annotations

import logging
import os
from typing import TypeVar

from dotenv import load_dotenv
from pydantic import BaseModel

logger = logging.getLogger(__name__)

load_dotenv()

_client = None
_client_checked = False

_PROVIDERS = {"anthropic", "openai", "xai"}
_DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"
_XAI_BASE_URL = "https://api.x.ai/v1"

T = TypeVar("T", bound=BaseModel)


class LLMClient:
    """Uniform wrapper over Anthropic / OpenAI / xAI. Two operations only --
    everything any call site in this repo needs: a plain-text completion,
    and a schema-validated structured one."""

    def __init__(self, provider: str, api_key: str, model: str, base_url: str | None = None):
        self.provider = provider
        self.model = model
        self.base_url = base_url
        if provider == "anthropic":
            import anthropic
            self._client = anthropic.Anthropic(api_key=api_key)
        else:  # openai, xai -- same SDK, base_url is what picks the actual provider
            import openai
            self._client = openai.OpenAI(api_key=api_key, base_url=base_url)

    def complete(self, prompt: str, max_tokens: int = 500) -> str:
        """Plain-text completion. Raises on API error -- callers already
        wrap their own call site in try/except and fall back to a stub,
        same contract as before this refactor."""
        if self.provider == "anthropic":
            response = self._client.messages.create(
                model=self.model, max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}])
            return response.content[0].text.strip()
        response = self._client.chat.completions.create(
            model=self.model, max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}])
        return response.choices[0].message.content.strip()

    def complete_structured(self, prompt: str, schema: type[T],
                             max_tokens: int = 1024) -> T | None:
        """Schema-validated completion. Returns None (never raises) on any
        failure -- API error, malformed response, schema mismatch -- since
        every call site treats "couldn't get a confident structured
        answer" as one outcome: fall back to the deterministic stub."""
        try:
            if self.provider == "anthropic":
                response = self._client.messages.parse(
                    model=self.model, max_tokens=max_tokens,
                    messages=[{"role": "user", "content": prompt}],
                    output_format=schema)
                return response.parsed_output
            # openai/xai: prefer the stable chat.completions.parse() path,
            # fall back to the older beta namespace for SDK versions where
            # structured outputs haven't graduated out of beta yet.
            parse_fn = getattr(self._client.chat.completions, "parse", None)
            if parse_fn is None:
                parse_fn = self._client.beta.chat.completions.parse
            response = parse_fn(
                model=self.model, max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
                response_format=schema)
            return response.choices[0].message.parsed
        except Exception as exc:
            logger.warning("LLMClient.complete_structured failed (%s)", exc)
            return None


def get_client() -> LLMClient | None:
    """Lazily construct an LLMClient. Returns None (permanently, for this
    process) if no credentials resolve or the SDK for the chosen provider
    is missing."""
    global _client, _client_checked
    if _client_checked:
        return _client
    _client_checked = True

    provider = os.environ.get("LLM_PROVIDER")
    api_key = os.environ.get("LLM_API_KEY")
    model = os.environ.get("LLM_MODEL")

    if not (provider and api_key):
        legacy_key = (os.environ.get("ANTHROPIC_API_KEY")
                      or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
        if not legacy_key:
            logger.info("AI client: no API credentials found, using deterministic fallback")
            return None
        provider, api_key = "anthropic", legacy_key
        model = model or _DEFAULT_ANTHROPIC_MODEL

    provider = provider.lower()
    if provider not in _PROVIDERS:
        logger.warning("AI client: unknown LLM_PROVIDER=%r, using deterministic fallback", provider)
        return None
    base_url = None
    if provider == "anthropic":
        model = model or _DEFAULT_ANTHROPIC_MODEL
    elif not model:
        logger.warning("AI client: LLM_MODEL is required for provider=%s, using deterministic fallback", provider)
        return None
    elif provider == "xai":
        base_url = _XAI_BASE_URL
    else:  # openai
        base_url = os.environ.get("LLM_BASE_URL")

    print(f"AI client: provider={provider} model={model} base_url={base_url!r}")

    try:
        _client = LLMClient(provider, api_key, model, base_url=base_url)
    except Exception as exc:  # pragma: no cover - import/env issues
        logger.info("AI client: unavailable (%s), using deterministic fallback", exc)
        _client = None
    return _client
