"""
Tests for agents/claude_client — cost_usd calculation.
The complete() function is not tested here since it makes live API calls;
it is exercised indirectly via mocked calls in test_scorer.py.
"""

import pytest
from agents.claude_client import cost_usd, DEFAULT_MODEL, PRICING


def test_cost_usd_sonnet_one_million_each():
    cost = cost_usd("claude-sonnet-4-6", 1_000_000, 1_000_000)
    assert cost == pytest.approx(3.00 + 15.00)


def test_cost_usd_haiku_one_million_each():
    cost = cost_usd("claude-haiku-4-5-20251001", 1_000_000, 1_000_000)
    assert cost == pytest.approx(1.00 + 5.00)


def test_cost_usd_opus_one_million_each():
    cost = cost_usd("claude-opus-4-8", 1_000_000, 1_000_000)
    assert cost == pytest.approx(5.00 + 25.00)


def test_cost_usd_zero_tokens():
    assert cost_usd("claude-sonnet-4-6", 0, 0) == 0.0


def test_cost_usd_output_only():
    cost = cost_usd("claude-sonnet-4-6", 0, 1_000_000)
    assert cost == pytest.approx(15.00)


def test_cost_usd_input_only():
    cost = cost_usd("claude-sonnet-4-6", 1_000_000, 0)
    assert cost == pytest.approx(3.00)


def test_cost_usd_unknown_model_falls_back_to_default():
    unknown = cost_usd("unknown-model-xyz", 1_000_000, 1_000_000)
    default = cost_usd(DEFAULT_MODEL, 1_000_000, 1_000_000)
    assert unknown == default


def test_cost_usd_scales_linearly():
    half = cost_usd("claude-sonnet-4-6", 500_000, 500_000)
    full = cost_usd("claude-sonnet-4-6", 1_000_000, 1_000_000)
    assert half == pytest.approx(full / 2)


def test_pricing_table_has_all_models():
    assert "claude-sonnet-4-6" in PRICING
    assert "claude-haiku-4-5-20251001" in PRICING
    assert "claude-opus-4-8" in PRICING


def test_pricing_table_has_input_and_output():
    for model, prices in PRICING.items():
        assert "input" in prices, f"{model} missing input price"
        assert "output" in prices, f"{model} missing output price"
