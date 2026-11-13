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

The values below are OpenAI's list prices (standard tier, short context up to
272K input tokens), checked on https://developers.openai.com/api/docs/pricing
on 2026-11-13. They are NOT fetched from OpenAI and may be outdated: override
them in the integration options when they change. Batch, flex and priority tiers, web search, code
interpreter and image generation tool calls are not priced.
"""

import math
import re
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

# Prices checked against the public price list on 2026-11-13.
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
    "gpt-5.4": {"input": 2.50, "cached_input": 0.25, "output": 15.00},
    "gpt-5.4-mini": {"input": 0.75, "cached_input": 0.075, "output": 4.50},
    "gpt-5.4-nano": {"input": 0.20, "cached_input": 0.02, "output": 1.25},
    "gpt-5.4-pro": {"input": 30.00, "output": 180.00},
    "gpt-5.5": {"input": 5.00, "cached_input": 0.50, "output": 30.00},
    "gpt-5.5-pro": {"input": 30.00, "output": 180.00},
    "gpt-5.6-luna": {"input": 0.20, "cached_input": 0.02, "output": 1.20},
    "gpt-5.6-terra": {"input": 2.00, "cached_input": 0.20, "output": 12.00},
    "gpt-5.6-sol": {"input": 4.00, "cached_input": 0.40, "output": 20.00},
    "gpt-6-luna": {"input": 0.10, "cached_input": 0.01, "output": 0.50},
    "gpt-6-sol": {"input": 2.00, "cached_input": 0.20, "output": 10.00},
    "gpt-6-astra": {"input": 10.00, "cached_input": 1.00, "output": 50.00},
    "gpt-6.1-terra": {"input": 1.00, "cached_input": 0.05, "output": 5.00},
    "gpt-6.1-sol": {"input": 2.00, "cached_input": 0.10, "output": 10.00},
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


# A dated snapshot of a model: "<model>-YYYY-MM-DD".
_DATED: Final = re.compile(r"(?P<base>.+)-\d{4}-\d{2}-\d{2}")


class PriceError(ValueError):
    """An invalid price override table.

    ``reason`` is the translation key of the options flow error, and
    ``placeholders`` the values for its ``{model}`` and ``{key}`` fields.
    """

    def __init__(self, reason: str, model: str = "", key: str = "") -> None:
        """Initialize the error."""
        super().__init__(f"{reason}: {model}.{key}" if key else f"{reason}: {model}")
        self.reason = reason
        self.placeholders = {"model": model, "key": key}


def _valid_number(number: object) -> bool:
    """Return True for a finite, non negative int or float (not a bool)."""
    return (
        not isinstance(number, bool)
        and isinstance(number, (int, float))
        and math.isfinite(number)
        and number >= 0
    )


def merge_prices(
    base: dict[str, ModelPrice], overrides: dict[str, ModelPrice] | None
) -> dict[str, ModelPrice]:
    """Return the price table with per-model overrides applied.

    An override replaces only the keys it contains; a new model is added.
    Values that are not valid prices (for example ``None`` stored by an older
    version for a NaN) are ignored.
    """
    merged = {model: dict(price) for model, price in base.items()}
    for model, price in (overrides or {}).items():
        if not isinstance(price, dict):
            continue
        valid = {
            key: float(number)
            for key, number in price.items()
            if key in PRICE_KEYS and _valid_number(number)
        }
        if valid:
            merged.setdefault(model, {}).update(valid)
    return merged


def find_price(prices: dict[str, ModelPrice], model: str) -> ModelPrice | None:
    """Find the price for a model name, also for dated snapshots.

    ``gpt-4o-mini-2024-07-18`` matches ``gpt-4o-mini``. Only a date suffix is
    stripped: ``gpt-5-pro`` or ``o3-mini`` are other models with other prices,
    so they never fall back to ``gpt-5`` or ``o3``.
    """
    if model in prices:
        return prices[model]
    if (dated := _DATED.fullmatch(model)) is not None:
        return prices.get(dated.group("base"))
    return None


def validate_overrides(value: object) -> dict[str, ModelPrice]:
    """Validate a user supplied override table, raise PriceError if invalid."""
    if not isinstance(value, dict):
        raise PriceError("invalid_prices")
    result: dict[str, ModelPrice] = {}
    for raw_model, price in value.items():
        model = raw_model.strip() if isinstance(raw_model, str) else ""
        if not model:
            raise PriceError("invalid_model")
        if not isinstance(price, dict):
            raise PriceError("invalid_entry", model)
        if not price:
            raise PriceError("empty_entry", model)
        entry: ModelPrice = {}
        for key, number in price.items():
            if key not in PRICE_KEYS:
                raise PriceError("unknown_key", model, str(key))
            if not _valid_number(number):
                raise PriceError("invalid_number", model, key)
            entry[key] = float(number)
        result[model] = entry
    return result
