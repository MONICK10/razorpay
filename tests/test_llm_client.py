import pytest

from engine import ai_client


@pytest.fixture(autouse=True)
def _reset_client_cache(monkeypatch):
    """get_client() caches its result at module scope for the life of the
    process -- reset it (and every env var it reads) around each test so
    tests can't see each other's configuration."""
    for var in ("LLM_PROVIDER", "LLM_API_KEY", "LLM_MODEL",
                "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    ai_client._client = None
    ai_client._client_checked = False
    yield
    ai_client._client = None
    ai_client._client_checked = False


def test_no_credentials_returns_none():
    assert ai_client.get_client() is None


def test_legacy_anthropic_api_key_still_works(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    client = ai_client.get_client()
    assert client is not None
    assert client.provider == "anthropic"
    assert client.model == "claude-opus-5"


def test_legacy_anthropic_auth_token_still_works(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "fake-token")
    client = ai_client.get_client()
    assert client is not None
    assert client.provider == "anthropic"


def test_new_env_anthropic_defaults_model(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_API_KEY", "fake-key")
    client = ai_client.get_client()
    assert client is not None
    assert client.provider == "anthropic"
    assert client.model == "claude-opus-5"


def test_new_env_anthropic_respects_explicit_model(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_API_KEY", "fake-key")
    monkeypatch.setenv("LLM_MODEL", "claude-custom")
    client = ai_client.get_client()
    assert client.model == "claude-custom"


def test_openai_requires_explicit_model(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_API_KEY", "fake-key")
    assert ai_client.get_client() is None


def test_openai_with_model_uses_default_base_url(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_API_KEY", "fake-key")
    monkeypatch.setenv("LLM_MODEL", "gpt-test")
    client = ai_client.get_client()
    assert client is not None
    assert client.provider == "openai"
    assert client.model == "gpt-test"
    assert "x.ai" not in str(client._client.base_url)


def test_xai_uses_xai_base_url(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "xai")
    monkeypatch.setenv("LLM_API_KEY", "fake-key")
    monkeypatch.setenv("LLM_MODEL", "grok-test")
    client = ai_client.get_client()
    assert client is not None
    assert client.provider == "xai"
    assert str(client._client.base_url).rstrip("/") == ai_client._XAI_BASE_URL


def test_unknown_provider_returns_none(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "some-other-provider")
    monkeypatch.setenv("LLM_API_KEY", "fake-key")
    assert ai_client.get_client() is None


def test_result_is_cached_across_calls(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    first = ai_client.get_client()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    second = ai_client.get_client()
    assert first is second
