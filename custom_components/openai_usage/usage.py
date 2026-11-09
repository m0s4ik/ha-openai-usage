"""Usage and cost tracking for OpenAI requests (openai_usage addition).

The copied upstream modules call into this module through small hooks marked
``# openai_usage:``. Every OpenAI response that carries usage information is
turned into a :class:`UsageRecord`, priced with the price table, fired as an
``openai_usage`` event and sent to the usage sensors of the config entry.

Nothing here talks to the network: the numbers come from the responses the
integration already receives.
"""

from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
import logging
from typing import TYPE_CHECKING, Any, Final

from openai.types.responses import (
    ResponseCompletedEvent,
    ResponseFailedEvent,
    ResponseIncompleteEvent,
)

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util.hass_dict import HassKey

from .const import CONF_CHAT_MODEL, DOMAIN
from .prices import DEFAULT_PRICES, ModelPrice, find_price, merge_prices

if TYPE_CHECKING:
    from homeassistant.components import stt
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.helpers.entity import Entity

_LOGGER = logging.getLogger(__name__)

EVENT_USAGE: Final = "openai_usage"
CONF_PRICES: Final = "prices"

KIND_CONVERSATION: Final = "conversation"
KIND_AI_TASK: Final = "ai_task"
KIND_STT: Final = "stt"
KIND_TTS: Final = "tts"
KINDS: Final = (KIND_CONVERSATION, KIND_AI_TASK, KIND_STT, KIND_TTS)

_SUBENTRY_KIND: Final = {
    "conversation": KIND_CONVERSATION,
    "ai_task_data": KIND_AI_TASK,
    "ai_task_evaluate": KIND_AI_TASK,
    "stt": KIND_STT,
    "tts": KIND_TTS,
}

DATA_TRACKERS: HassKey[dict[str, UsageTracker]] = HassKey(f"{DOMAIN}_usage")


def signal_usage(entry_id: str) -> str:
    """Return the dispatcher signal used for a config entry."""
    return f"{DOMAIN}_usage_{entry_id}"


@dataclass(slots=True, kw_only=True)
class UsageRecord:
    """Usage of a single OpenAI request."""

    config_entry_id: str
    entity_id: str | None
    subentry_id: str | None
    kind: str
    model: str
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    audio_input_tokens: int = 0
    audio_seconds: float = 0.0
    characters: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        """Return input plus output tokens."""
        return self.input_tokens + self.output_tokens

    def as_event_data(self) -> dict[str, Any]:
        """Return the data of the ``openai_usage`` event."""
        data = asdict(self)
        data.pop("audio_input_tokens")
        data["cost_usd"] = round(self.cost_usd, 8)
        return data


def compute_cost(price: ModelPrice, record: UsageRecord) -> float:
    """Compute the cost in USD of a request from a model price entry."""
    cost = 0.0
    if record.input_tokens or record.output_tokens:
        if any(key in price for key in ("input", "output", "audio_input")):
            input_price = price.get("input", 0.0)
            cached = min(record.cached_input_tokens, record.input_tokens)
            audio = min(record.audio_input_tokens, record.input_tokens - cached)
            text = record.input_tokens - cached - audio
            cost += text * input_price
            cost += cached * price.get("cached_input", input_price)
            cost += audio * price.get("audio_input", input_price)
            cost += record.output_tokens * price.get("output", 0.0)
            cost /= 1_000_000
        elif record.audio_seconds and "per_minute" in price:
            cost += record.audio_seconds / 60 * price["per_minute"]
    elif record.audio_seconds and "per_minute" in price:
        cost += record.audio_seconds / 60 * price["per_minute"]
    if record.characters and "per_1m_characters" in price:
        cost += record.characters * price["per_1m_characters"] / 1_000_000
    return cost


class UsageTracker:
    """Price usage records of one config entry and publish them."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        overrides: dict[str, ModelPrice] | None = None,
    ) -> None:
        """Initialize the tracker."""
        self.hass = hass
        self.entry_id = entry_id
        self.prices = merge_prices(DEFAULT_PRICES, overrides)
        self._warned: set[str] = set()

    @callback
    def async_record(self, record: UsageRecord) -> None:
        """Price a record, fire the event and update the sensors."""
        price = find_price(self.prices, record.model)
        if price is None:
            if record.model not in self._warned:
                self._warned.add(record.model)
                _LOGGER.warning(
                    "No price known for OpenAI model %s: its cost is counted as 0. "
                    "Add it in the integration options",
                    record.model,
                )
        else:
            record.cost_usd = compute_cost(price, record)
        _LOGGER.debug("OpenAI usage: %s", record)
        self.hass.bus.async_fire(EVENT_USAGE, record.as_event_data())
        async_dispatcher_send(self.hass, signal_usage(self.entry_id), record)


# The entry's title, to tell it from the official integration's "ChatGPT".
ENTRY_TITLE = "ChatGPT with Token & Cost Tracking"


@callback
def async_setup_tracker(hass: HomeAssistant, entry: ConfigEntry) -> UsageTracker:
    """Create the tracker of a config entry."""
    # Entries made by 0.1.0/0.1.1 were titled "ChatGPT" like the official one.
    if entry.title == "ChatGPT":
        hass.config_entries.async_update_entry(entry, title=ENTRY_TITLE)
    tracker = UsageTracker(hass, entry.entry_id, entry.options.get(CONF_PRICES))
    hass.data.setdefault(DATA_TRACKERS, {})[entry.entry_id] = tracker
    return tracker


@callback
def async_unload_tracker(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Forget the tracker of a config entry."""
    hass.data.get(DATA_TRACKERS, {}).pop(entry.entry_id, None)


def _new_record(entity: Entity, model: str) -> UsageRecord | None:
    """Return an empty record for an entity, or None if not tracked."""
    entry = getattr(entity, "entry", None)
    subentry = getattr(entity, "subentry", None)
    if entry is None:
        return None
    return UsageRecord(
        config_entry_id=entry.entry_id,
        entity_id=entity.entity_id,
        subentry_id=subentry.subentry_id if subentry is not None else None,
        kind=_SUBENTRY_KIND.get(
            subentry.subentry_type if subentry is not None else "", "conversation"
        ),
        model=model,
    )


@callback
def _async_submit(entity: Entity, record: UsageRecord) -> None:
    """Send a record to the tracker of the entity's config entry."""
    if entity.hass is None:
        return
    tracker = entity.hass.data.get(DATA_TRACKERS, {}).get(record.config_entry_id)
    if tracker is not None:
        tracker.async_record(record)


def _fill_token_usage(record: UsageRecord, usage: Any) -> None:
    """Copy Responses/Decisions style usage into a record."""
    record.input_tokens = getattr(usage, "input_tokens", 0) or 0
    record.output_tokens = getattr(usage, "output_tokens", 0) or 0
    if (details := getattr(usage, "input_tokens_details", None)) is not None:
        record.cached_input_tokens = getattr(details, "cached_tokens", 0) or 0
    if (details := getattr(usage, "output_tokens_details", None)) is not None:
        record.reasoning_tokens = getattr(details, "reasoning_tokens", 0) or 0


@callback
def async_record_response_usage(
    entity: Entity, model: str | None, usage: Any, default_model: str = ""
) -> None:
    """Record the usage of a Responses or Decisions API call."""
    if usage is None:
        return
    try:
        if (record := _new_record(entity, model or default_model)) is None:
            return
        _fill_token_usage(record, usage)
        _async_submit(entity, record)
    except Exception:
        _LOGGER.exception("Failed to record OpenAI usage")


async def wrap_response_stream(entity: Entity, stream: Any) -> AsyncIterator[Any]:
    """Pass a Responses API event stream through, recording its usage."""
    default_model = getattr(entity, "subentry", None)
    default = default_model.data.get(CONF_CHAT_MODEL, "") if default_model else ""
    async for event in stream:
        if isinstance(
            event,
            (ResponseCompletedEvent, ResponseIncompleteEvent, ResponseFailedEvent),
        ):
            async_record_response_usage(
                entity, event.response.model, event.response.usage, default
            )
        yield event


@callback
def async_record_decision(entity: Entity, response: Any) -> None:
    """Record the usage of a Decisions API call (AI task evaluation)."""
    async_record_response_usage(
        entity, getattr(response, "model", None), getattr(response, "usage", None)
    )


@callback
def async_record_transcription(
    entity: Entity,
    model: str,
    response: Any,
    metadata: stt.SpeechMetadata,
    audio_length: int,
) -> None:
    """Record the usage of a transcription (speech-to-text) call.

    ``audio_length`` is the number of raw audio bytes before any container
    header; the duration is computed from it for PCM audio.
    """
    try:
        if (record := _new_record(entity, model)) is None:
            return
        if getattr(metadata.codec, "value", metadata.codec) == "pcm":
            bytes_per_second = (
                metadata.sample_rate.value
                * metadata.channel.value
                * metadata.bit_rate.value
                // 8
            )
            if bytes_per_second:
                record.audio_seconds = round(audio_length / bytes_per_second, 3)
        usage = getattr(response, "usage", None)
        if usage is not None and getattr(usage, "type", None) == "tokens":
            record.input_tokens = usage.input_tokens or 0
            record.output_tokens = usage.output_tokens or 0
            details = getattr(usage, "input_token_details", None)
            if details is not None and details.audio_tokens is not None:
                record.audio_input_tokens = details.audio_tokens
            else:
                record.audio_input_tokens = record.input_tokens
        elif usage is not None and getattr(usage, "type", None) == "duration":
            record.audio_seconds = float(usage.seconds)
        _async_submit(entity, record)
    except Exception:
        _LOGGER.exception("Failed to record OpenAI usage")


@callback
def async_record_speech(entity: Entity, model: str, message: str) -> None:
    """Record a speech (text-to-speech) call, priced by input characters."""
    try:
        if (record := _new_record(entity, model)) is None:
            return
        record.characters = len(message)
        _async_submit(entity, record)
    except Exception:
        _LOGGER.exception("Failed to record OpenAI usage")
