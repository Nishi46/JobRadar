import logging

import pytest

from jobradar.llm.pricing import PRICES, cost_usd

MTOK = 1_000_000


def test_anthropic_cache_tokens_are_billed_on_top_of_input() -> None:
    # Anthropic's input_tokens excludes cache tokens, so all four counts add up.
    cost = cost_usd(
        "claude-haiku-5-5",
        input_tokens=MTOK,
        output_tokens=MTOK,
        cache_read_tokens=MTOK,
        cache_write_tokens=MTOK,
    )
    assert cost == pytest.approx(0.10 + 0.50 + 0.01 + 0.125)


def test_anthropic_typical_extraction_call() -> None:
    # 2,000 fresh input, 3,000 read from cache, 800 output.
    cost = cost_usd(
        "claude-haiku-5-5",
        input_tokens=2_000,
        output_tokens=800,
        cache_read_tokens=3_000,
        cache_write_tokens=0,
    )
    assert cost == pytest.approx((2_000 * 0.10 + 800 * 0.50 + 3_000 * 0.01) / MTOK)


def test_openai_cached_tokens_are_a_subset_of_input() -> None:
    # 1M input of which 400k were cached: 600k at full price, 400k at the cached price.
    cost = cost_usd(
        "gpt-6.2-sol",
        input_tokens=MTOK,
        output_tokens=0,
        cache_read_tokens=400_000,
        cache_write_tokens=0,
    )
    assert cost == pytest.approx(0.6 * 2.00 + 0.4 * 0.10)


def test_openai_cache_writes_cost_nothing_extra() -> None:
    # No separate cache-write price for this model: written tokens are ordinary input.
    with_writes = cost_usd(
        "gpt-6.2-sol",
        input_tokens=10_000,
        output_tokens=1_000,
        cache_read_tokens=0,
        cache_write_tokens=10_000,
    )
    assert with_writes == pytest.approx((10_000 * 2.00 + 1_000 * 10.00) / MTOK)


def test_openai_cached_more_than_input_is_clamped(caplog: pytest.LogCaptureFixture) -> None:
    # Impossible counts must not produce a negative charge.
    with caplog.at_level(logging.WARNING):
        cost = cost_usd(
            "gpt-6.2-sol",
            input_tokens=100,
            output_tokens=0,
            cache_read_tokens=500,
            cache_write_tokens=0,
        )
    assert cost is not None
    assert cost >= 0
    assert "exceeds input" in caplog.text


def test_unknown_model_returns_none_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        cost = cost_usd(
            "mystery-model",
            input_tokens=1,
            output_tokens=1,
            cache_read_tokens=0,
            cache_write_tokens=0,
        )
    assert cost is None
    assert "mystery-model" in caplog.text


def test_zero_tokens_cost_zero() -> None:
    for model in PRICES:
        assert (
            cost_usd(
                model, input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0
            )
            == 0.0
        )
