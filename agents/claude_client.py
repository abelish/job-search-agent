"""
Shared Claude API client wrapper.

All agents that need language judgment (scoring rationale, tailoring, drafting,
prep generation) should call through this module rather than instantiating
their own Anthropic client. This keeps model choice and prompt conventions
in one place.
"""

import os
from anthropic import Anthropic

DEFAULT_MODEL = "claude-sonnet-4-6"

# Prices in USD per million tokens.
PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
    "claude-opus-4-8": {"input": 5.00, "output": 25.00},
}


def get_client() -> Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Add it to your .env file."
        )
    return Anthropic(api_key=api_key)


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    prices = PRICING.get(model, PRICING[DEFAULT_MODEL])
    return (input_tokens / 1_000_000 * prices["input"]) + (output_tokens / 1_000_000 * prices["output"])


def complete(prompt: str, system: str = "", model: str = DEFAULT_MODEL, max_tokens: int = 2000) -> dict:
    """
    Single shot completion. Returns:
      {
        "text":          str  -- generated text
        "input_tokens":  int  -- prompt token count
        "output_tokens": int  -- completion token count
        "model":         str  -- model used (for pricing)
      }

    For structured output, instruct the model in the prompt to return JSON only
    and parse result["text"].
    """
    client = get_client()
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(block.text for block in response.content if block.type == "text")
    return {
        "text": text,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "model": model,
    }
