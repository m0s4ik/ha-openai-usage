"""Built-in OpenAI price table for usage tracking (openai_usage addition).

All prices are in USD.

Token prices are per 1M tokens:
- ``input``: uncached input (text) tokens
- ``cached_input``: input tokens served from the prompt cache
- ``output``: output tokens (reasoning tokens are billed as output tokens)
- ``audio_input``: audio input tokens (transcription models billed by tokens)

Other units:
- ``per_minute``: per minute of audio (transcription billed by duration)
- ``per_1m_characters``: per 1M characters of input text (speech)

The values below are the best known list prices (standard tier) at the time
of writing (2026-11). They are NOT fetched from OpenAI and may be outdated:
check https://openai.com/api/pricing/ and override them in the integration
options when they change. Batch, flex and priority tiers, web search, code
interpreter and image generation tool calls are not priced.
"""

from typing import Final

PRICE_KEYS: Final = (
    "input",
    "cached_input",
    "output",
    "audio_input",
    "per_minute",
    "per_1m_characters",
)

type ModelPrice = dict[str, float]

# Prices checked against the public price list as last known (2026-11).
DEFAULT_PRICES: Final[dict[str, ModelPrice]] = {
    # Chat / Responses API models
    "gpt-4o-mini": {"input": 0.15, "cached_input": 0.075, "output": 0.60},
    "gpt-4o": {"input": 2.50, "cached_input": 1.25, "output": 10.00},
    "gpt-4.1": {"input": 2.00, "cached_input": 0.50, "output": 8.00},
    "gpt-4.1-mini": {"input": 0.40, "cached_input": 0.10, "output": 1.60},
    "gpt-4.1-nano": {"input": 0.10, "cached_input": 0.025, "output": 0.40},
    "gpt-5": {"input": 1.25, "cached_input": 0.125, "output": 10.00},
    "gpt-5-mini": {"input": 0.25, "cached_input": 0.025, "output": 2.00},
    "gpt-5-nano": {"input": 0.05, "cached_input": 0.005, "output": 0.40},
    "gpt-5.1": {"input": 1.25, "cached_input": 0.125, "output": 10.00},
    "o3": {"input": 2.00, "cached_input": 0.50, "output": 8.00},
    "o4-mini": {"input": 1.10, "cached_input": 0.275, "output": 4.40},
    # Transcription (speech-to-text)
    "gpt-4o-mini-transcribe": {
        "input": 1.25,
        "audio_input": 3.00,
        "output": 5.00,
        "per_minute": 0.003,
    },
    "gpt-4o-transcribe": {
        "input": 2.50,
        "audio_input": 6.00,
        "output": 10.00,
        "per_minute": 0.006,
    },
    "whisper-1": {"per_minute": 0.006},
    # Speech (text-to-speech). The API returns no usage for speech, so cost is
    # estimated from the input characters. For gpt-4o-mini-tts the official
    # estimate is about $0.015 per minute of audio; ~1000 characters per minute
    # of speech gives the value below (an approximation).
    "gpt-4o-mini-tts": {"per_1m_characters": 15.00},
    "tts-1": {"per_1m_characters": 15.00},
    "tts-1-hd": {"per_1m_characters": 30.00},
}


def merge_prices(
    base: dict[str, ModelPrice], overrides: dict[str, ModelPrice] | None
) -> dict[str, ModelPrice]:
    """Return the price table with per-model overrides applied.

    An override replaces only the keys it contains; a new model is added.
    """
    merged = {model: dict(price) for model, price in base.items()}
    for model, price in (overrides or {}).items():
        merged.setdefault(model, {}).update(price)
    return merged


def find_price(prices: dict[str, ModelPrice], model: str) -> ModelPrice | None:
    """Find the price for a model name, also for dated snapshots.

    ``gpt-4o-mini-2024-07-18`` matches ``gpt-4o-mini``: the longest table key
    that equals the model or is followed by ``-`` in the model name wins.
    """
    if model in prices:
        return prices[model]
    best: str | None = None
    for key in prices:
        if model.startswith(f"{key}-") and (best is None or len(key) > len(best)):
            best = key
    return prices[best] if best is not None else None


def validate_overrides(value: object) -> dict[str, ModelPrice]:
    """Validate a user supplied override table, raise ValueError if invalid."""
    if not isinstance(value, dict):
        raise ValueError("not an object")
    result: dict[str, ModelPrice] = {}
    for model, price in value.items():
        if not isinstance(model, str) or not model or not isinstance(price, dict):
            raise ValueError(f"invalid entry for {model!r}")
        entry: ModelPrice = {}
        for key, number in price.items():
            if key not in PRICE_KEYS:
                raise ValueError(f"unknown price key {key!r}")
            if (
                isinstance(number, bool)
                or not isinstance(number, (int, float))
                or number < 0
            ):
                raise ValueError(f"invalid price for {model}.{key}")
            entry[key] = float(number)
        result[model] = entry
    return result
