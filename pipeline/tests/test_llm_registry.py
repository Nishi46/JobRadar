from collections.abc import Iterator

import pytest

from jobradar.llm.anthropic_provider import AnthropicProvider
from jobradar.llm.openai_provider import OpenAIProvider
from jobradar.llm.registry import MissingAPIKeyError, get_provider


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    get_provider.cache_clear()
    yield
    get_provider.cache_clear()


@pytest.mark.parametrize(
    ("name", "env_var"), [("anthropic", "ANTHROPIC_API_KEY"), ("openai", "OPENAI_API_KEY")]
)
def test_missing_key_names_the_env_var(
    monkeypatch: pytest.MonkeyPatch, name: str, env_var: str
) -> None:
    monkeypatch.delenv(env_var, raising=False)
    with pytest.raises(MissingAPIKeyError, match=env_var):
        get_provider(name)


def test_empty_key_counts_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    # .env.example ships the variable with an empty value.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    with pytest.raises(MissingAPIKeyError, match="ANTHROPIC_API_KEY"):
        get_provider("anthropic")


def test_builds_each_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    # Constructing a client makes no network call, so a dummy key is safe.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    assert isinstance(get_provider("anthropic"), AnthropicProvider)
    assert isinstance(get_provider("openai"), OpenAIProvider)


def test_provider_is_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    assert get_provider("anthropic") is get_provider("anthropic")


def test_unknown_provider() -> None:
    with pytest.raises(ValueError, match="mystery"):
        get_provider("mystery")
