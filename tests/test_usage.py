"""Tests for the usage and cost tracking of openai_usage."""

from collections.abc import AsyncIterable, Generator
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, patch

from freezegun.api import FrozenDateTimeFactory
from openai.types import ResponseFormatText
from openai.types.audio import Transcription
from openai.types.decision import Decision
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseTextConfig,
    ResponseUsage,
)
from openai.types.responses.response_usage import (
    InputTokensDetails,
    OutputTokensDetails,
)
import pytest
from pytest_homeassistant.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
    mock_restore_cache_with_extra_data,
)

from custom_components.openai_usage.const import DOMAIN
from custom_components.openai_usage.prices import (
    DEFAULT_PRICES,
    find_price,
    validate_overrides,
)
from custom_components.openai_usage.usage import (
    CONF_PRICES,
    EVENT_USAGE,
    UsageRecord,
    compute_cost,
)
from homeassistant.components import conversation, stt
from homeassistant.components.tts import DATA_COMPONENT as TTS_COMPONENT
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context, HomeAssistant, State
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from . import create_message_item

COST_TODAY = "sensor.openai_usage_cost_today"
COST_MONTH = "sensor.openai_usage_cost_this_month"
TOKENS_TODAY = "sensor.openai_usage_tokens_today"
REQUESTS_TODAY = "sensor.openai_usage_requests_today"
COST_TODAY_STT = "sensor.openai_usage_speech_to_text_cost_today"


def _usage(
    input_tokens: int, cached: int, output_tokens: int, reasoning: int
) -> ResponseUsage:
    return ResponseUsage(
        input_tokens=input_tokens,
        input_tokens_details=InputTokensDetails(
            cached_tokens=cached, cache_write_tokens=0
        ),
        output_tokens=output_tokens,
        output_tokens_details=OutputTokensDetails(reasoning_tokens=reasoning),
        total_tokens=input_tokens + output_tokens,
    )


@pytest.fixture
def mock_stream_with_usage() -> Generator[AsyncMock]:
    """Mock the Responses API: one message, then a completed event with usage."""
    usages: list[tuple[str, ResponseUsage]] = []

    async def generator(**kwargs: Any):
        model, usage = usages.pop(0)
        for event in create_message_item(id="msg_A", text="Hello", output_index=0):
            yield event
        yield ResponseCompletedEvent(
            response=Response(
                id="resp_A",
                created_at=1700000000,
                model=model,
                object="response",
                output=[],
                parallel_tool_calls=True,
                tool_choice="auto",
                tools=[],
                status="completed",
                text=ResponseTextConfig(format=ResponseFormatText(type="text")),
                usage=usage,
            ),
            sequence_number=99,
            type="response.completed",
        )

    with patch(
        "openai.resources.responses.AsyncResponses.create", AsyncMock()
    ) as mock_create:
        mock_create.side_effect = lambda **kwargs: generator(**kwargs)
        mock_create.usages = usages
        yield mock_create


async def _converse(hass: HomeAssistant) -> None:
    await conversation.async_converse(
        hass, "hello", None, Context(), agent_id="conversation.openai_conversation"
    )


# --- price table


def test_find_price_matches_dated_snapshots() -> None:
    """Dated model names match the longest table key."""
    assert find_price(DEFAULT_PRICES, "gpt-4o-mini") is DEFAULT_PRICES["gpt-4o-mini"]
    assert (
        find_price(DEFAULT_PRICES, "gpt-4o-mini-2024-07-18")
        is DEFAULT_PRICES["gpt-4o-mini"]
    )
    assert find_price(DEFAULT_PRICES, "gpt-4o-2024-08-06") is DEFAULT_PRICES["gpt-4o"]
    assert (
        find_price(DEFAULT_PRICES, "gpt-5-mini-2025-08-07")
        is (DEFAULT_PRICES["gpt-5-mini"])
    )
    assert find_price(DEFAULT_PRICES, "unknown-model") is None
    assert find_price(DEFAULT_PRICES, "gpt-4oo") is None


def test_compute_cost_tokens() -> None:
    """Uncached, cached and output tokens use their own price."""
    record = UsageRecord(
        config_entry_id="x",
        entity_id=None,
        subentry_id=None,
        kind="conversation",
        model="m",
        input_tokens=1_000_000,
        cached_input_tokens=400_000,
        output_tokens=100_000,
        reasoning_tokens=50_000,
    )
    price = {"input": 1.0, "cached_input": 0.1, "output": 10.0}
    # 600k * 1 + 400k * 0.1 + 100k * 10 (reasoning is part of output)
    assert compute_cost(price, record) == pytest.approx(0.6 + 0.04 + 1.0)


def test_compute_cost_audio_and_characters() -> None:
    """Per-minute, audio token and per-character prices."""
    base = {"config_entry_id": "x", "entity_id": None, "subentry_id": None}
    minutes = UsageRecord(**base, kind="stt", model="whisper-1", audio_seconds=90)
    assert compute_cost({"per_minute": 0.006}, minutes) == pytest.approx(0.009)
    tokens = UsageRecord(
        **base,
        kind="stt",
        model="m",
        input_tokens=1000,
        audio_input_tokens=900,
        output_tokens=50,
        audio_seconds=10,
    )
    price = {"input": 1.0, "audio_input": 3.0, "output": 5.0, "per_minute": 0.003}
    assert compute_cost(price, tokens) == pytest.approx(
        (100 * 1.0 + 900 * 3.0 + 50 * 5.0) / 1_000_000
    )
    speech = UsageRecord(**base, kind="tts", model="tts-1", characters=2000)
    assert compute_cost({"per_1m_characters": 15.0}, speech) == pytest.approx(0.03)


def test_validate_overrides() -> None:
    """Override tables are validated."""
    assert validate_overrides({"m": {"input": 1, "output": 2.5}}) == {
        "m": {"input": 1.0, "output": 2.5}
    }
    for bad in (
        [],
        {"m": 1},
        {"m": {"bogus": 1}},
        {"m": {"input": -1}},
        {"m": {"input": "1"}},
        {"m": {"input": True}},
    ):
        with pytest.raises(ValueError):
            validate_overrides(bad)


# --- events and sensors


@pytest.mark.usefixtures("mock_init_component")
async def test_conversation_usage_event_and_sensors(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_stream_with_usage: AsyncMock,
    entity_registry: er.EntityRegistry,
) -> None:
    """A conversation turn fires an event and updates the sensors."""
    events = async_capture_events(hass, EVENT_USAGE)
    assert hass.states.get(COST_TODAY).state == "0.0"
    assert hass.states.get(REQUESTS_TODAY).state == "0"

    mock_stream_with_usage.usages.append(
        ("gpt-4o-mini-2024-07-18", _usage(1000, 200, 300, 0))
    )
    await _converse(hass)
    await hass.async_block_till_done()

    subentry = next(
        sub
        for sub in mock_config_entry.subentries.values()
        if sub.subentry_type == "conversation"
    )
    expected_cost = (800 * 0.15 + 200 * 0.075 + 300 * 0.60) / 1_000_000
    assert len(events) == 1
    data = events[0].data
    assert data == {
        "config_entry_id": mock_config_entry.entry_id,
        "entity_id": "conversation.openai_conversation",
        "subentry_id": subentry.subentry_id,
        "kind": "conversation",
        "model": "gpt-4o-mini-2024-07-18",
        "input_tokens": 1000,
        "cached_input_tokens": 200,
        "output_tokens": 300,
        "reasoning_tokens": 0,
        "audio_seconds": 0.0,
        "characters": 0,
        "cost_usd": pytest.approx(expected_cost),
    }

    assert float(hass.states.get(COST_TODAY).state) == pytest.approx(expected_cost)
    assert float(hass.states.get(COST_MONTH).state) == pytest.approx(expected_cost)
    assert hass.states.get(TOKENS_TODAY).state == "1300"
    assert hass.states.get(REQUESTS_TODAY).state == "1"

    # A second request accumulates.
    mock_stream_with_usage.usages.append(("gpt-5-mini", _usage(100, 0, 1000, 800)))
    await _converse(hass)
    await hass.async_block_till_done()
    assert len(events) == 2
    assert events[1].data["reasoning_tokens"] == 800
    second = (100 * 0.25 + 1000 * 2.0) / 1_000_000
    assert float(hass.states.get(COST_TODAY).state) == pytest.approx(
        expected_cost + second
    )
    assert hass.states.get(TOKENS_TODAY).state == "2400"
    assert hass.states.get(REQUESTS_TODAY).state == "2"

    state = hass.states.get(COST_TODAY)
    assert state.attributes["unit_of_measurement"] == "USD"
    assert state.attributes["state_class"] == "total"
    assert state.attributes["device_class"] == "monetary"
    assert "last_reset" in state.attributes

    # Per-kind sensors exist but are disabled by default.
    entry = entity_registry.async_get(COST_TODAY_STT)
    assert entry is not None
    assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION


@pytest.mark.usefixtures("mock_init_component")
async def test_unknown_model_costs_zero_and_warns_once(
    hass: HomeAssistant,
    mock_stream_with_usage: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unknown model is counted with cost 0 and a single warning."""
    events = async_capture_events(hass, EVENT_USAGE)
    for _ in range(2):
        mock_stream_with_usage.usages.append(("brand-new-model", _usage(10, 0, 10, 0)))
        await _converse(hass)
    await hass.async_block_till_done()
    assert [event.data["cost_usd"] for event in events] == [0.0, 0.0]
    assert caplog.text.count("No price known for OpenAI model brand-new-model") == 1
    assert hass.states.get(TOKENS_TODAY).state == "40"
    assert hass.states.get(COST_TODAY).state == "0.0"


async def test_daily_and_monthly_reset(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_stream_with_usage: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Daily counters reset at local midnight, monthly ones on the 1st."""
    await hass.config.async_set_time_zone("Europe/Rome")
    freezer.move_to(
        datetime(2026, 11, 30, 23, 50, tzinfo=dt_util.get_default_time_zone())
    )
    with patch("openai.resources.models.AsyncModels.list", new_callable=AsyncMock):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    mock_stream_with_usage.usages.append(("gpt-4o-mini", _usage(1000, 0, 1000, 0)))
    await _converse(hass)
    await hass.async_block_till_done()
    cost = (1000 * 0.15 + 1000 * 0.60) / 1_000_000
    assert float(hass.states.get(COST_TODAY).state) == pytest.approx(cost)
    assert float(hass.states.get(COST_MONTH).state) == pytest.approx(cost)

    # Local midnight of December 1st: both reset, without any request.
    freezer.move_to(
        datetime(2026, 12, 1, 0, 0, 0, tzinfo=dt_util.get_default_time_zone())
    )
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(COST_TODAY).state == "0.0"
    assert hass.states.get(COST_MONTH).state == "0.0"
    assert hass.states.get(REQUESTS_TODAY).state == "0"
    assert hass.states.get(COST_TODAY).attributes["last_reset"] == (
        "2026-12-01T00:00:00+01:00"
    )

    # Next day: daily resets, monthly keeps counting.
    mock_stream_with_usage.usages.append(("gpt-4o-mini", _usage(1000, 0, 1000, 0)))
    await _converse(hass)
    freezer.move_to(
        datetime(2026, 12, 2, 0, 0, 0, tzinfo=dt_util.get_default_time_zone())
    )
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(COST_TODAY).state == "0.0"
    assert float(hass.states.get(COST_MONTH).state) == pytest.approx(cost)


async def test_restore_after_restart(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Counters are restored in the same period and dropped in a new one."""
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to("2026-11-09 12:00:00+00:00")
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(COST_TODAY, "0.25", {"last_reset": "2026-11-09T00:00:00+00:00"}),
                {"native_value": 0.25, "native_unit_of_measurement": "USD"},
            ),
            (
                State(COST_MONTH, "3.5", {"last_reset": "2026-11-01T00:00:00+00:00"}),
                {"native_value": 3.5, "native_unit_of_measurement": "USD"},
            ),
            (
                # From yesterday: must not be restored.
                State(TOKENS_TODAY, "999", {"last_reset": "2026-11-08T00:00:00+00:00"}),
                {"native_value": 999, "native_unit_of_measurement": "tokens"},
            ),
        ],
    )
    with patch("openai.resources.models.AsyncModels.list", new_callable=AsyncMock):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert hass.states.get(COST_TODAY).state == "0.25"
    assert hass.states.get(COST_MONTH).state == "3.5"
    assert hass.states.get(TOKENS_TODAY).state == "0"


@pytest.mark.usefixtures("mock_init_component")
async def test_stt_usage(
    hass: HomeAssistant,
    mock_create_transcription: AsyncMock,
) -> None:
    """Transcriptions record audio duration and token usage."""
    events = async_capture_events(hass, EVENT_USAGE)
    entity = hass.data[stt.DOMAIN].get_entity("stt.openai_stt")
    mock_create_transcription.return_value = Transcription.model_validate(
        {
            "text": "Turn on the lights",
            "usage": {
                "type": "tokens",
                "input_tokens": 120,
                "input_token_details": {"audio_tokens": 100, "text_tokens": 20},
                "output_tokens": 8,
                "total_tokens": 128,
            },
        }
    )
    metadata = stt.SpeechMetadata(
        language="en-US",
        format=stt.AudioFormats.WAV,
        codec=stt.AudioCodecs.PCM,
        bit_rate=stt.AudioBitRates.BITRATE_16,
        sample_rate=stt.AudioSampleRates.SAMPLERATE_16000,
        channel=stt.AudioChannels.CHANNEL_MONO,
    )

    async def audio() -> AsyncIterable[bytes]:
        yield b"\0" * 64000  # 2 seconds

    result = await entity.async_process_audio_stream(metadata, audio())
    await hass.async_block_till_done()
    assert result.text == "Turn on the lights"
    assert len(events) == 1
    data = events[0].data
    assert data["kind"] == "stt"
    assert data["entity_id"] == "stt.openai_stt"
    assert data["model"] == "gpt-4o-mini-transcribe"
    assert data["audio_seconds"] == 2.0
    assert data["input_tokens"] == 120
    assert data["output_tokens"] == 8
    assert data["cost_usd"] == pytest.approx((20 * 1.25 + 100 * 3.0 + 8 * 5.0) / 1e6)


@pytest.mark.usefixtures("mock_init_component")
async def test_stt_usage_by_duration(
    hass: HomeAssistant,
    mock_create_transcription: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Duration-billed transcription (e.g. whisper-1) uses the per-minute price."""
    subentry = next(
        sub
        for sub in mock_config_entry.subentries.values()
        if sub.subentry_type == "stt"
    )
    hass.config_entries.async_update_subentry(
        mock_config_entry, subentry, data={"chat_model": "whisper-1"}
    )
    await hass.async_block_till_done()
    events = async_capture_events(hass, EVENT_USAGE)
    entity = hass.data[stt.DOMAIN].get_entity("stt.openai_stt")
    mock_create_transcription.return_value = Transcription.model_validate(
        {"text": "ok", "usage": {"type": "duration", "seconds": 30}}
    )
    metadata = stt.SpeechMetadata(
        language="en-US",
        format=stt.AudioFormats.OGG,
        codec=stt.AudioCodecs.OPUS,
        bit_rate=stt.AudioBitRates.BITRATE_16,
        sample_rate=stt.AudioSampleRates.SAMPLERATE_16000,
        channel=stt.AudioChannels.CHANNEL_MONO,
    )

    async def audio() -> AsyncIterable[bytes]:
        yield b"opus"

    await entity.async_process_audio_stream(metadata, audio())
    await hass.async_block_till_done()
    assert events[0].data["model"] == "whisper-1"
    assert events[0].data["audio_seconds"] == 30.0
    assert events[0].data["cost_usd"] == pytest.approx(0.003)


@pytest.mark.usefixtures("mock_init_component")
async def test_tts_usage(
    hass: HomeAssistant,
    mock_create_speech: AsyncMock,
) -> None:
    """Speech is priced by input characters."""
    events = async_capture_events(hass, EVENT_USAGE)
    entity = hass.data[TTS_COMPONENT].get_entity("tts.openai_tts")
    mock_create_speech.return_value = [b"audio"]
    message = "There is a person at the front door."
    await entity.async_get_tts_audio(
        message, "en", {"voice": "marin", "preferred_format": "mp3"}
    )
    await hass.async_block_till_done()
    assert len(events) == 1
    data = events[0].data
    assert data["kind"] == "tts"
    assert data["model"] == "gpt-4o-mini-tts"
    assert data["characters"] == len(message)
    assert data["cost_usd"] == pytest.approx(len(message) * 15.0 / 1e6)


@pytest.mark.usefixtures("mock_init_component")
async def test_ai_task_evaluate_usage(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Decisions API usage is recorded as ai_task, with the price override."""
    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={CONF_PRICES: {"gpt-6-luna": {"input": 2.0, "output": 8.0}}},
    )
    await hass.async_block_till_done()
    events = async_capture_events(hass, EVENT_USAGE)
    decision = Decision.model_validate(
        {
            "model": "gpt-6-luna",
            "answers": [{"type": "predicate", "name": "q", "probability": 0.5}],
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 10,
                "total_tokens": 1010,
                "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 4},
            },
        }
    )
    from homeassistant.components import ai_task  # noqa: PLC0415

    with patch(
        "openai.resources.decisions.AsyncDecisions.create", return_value=decision
    ):
        await ai_task.async_evaluate(
            hass,
            task_name="test",
            entity_id="ai_task.openai_evaluation",
            state="x",
            questions={"q": {"type": "noul", "instructions": "Is it?"}},
        )
    await hass.async_block_till_done()
    assert len(events) == 1
    data = events[0].data
    assert data["kind"] == "ai_task"
    assert data["reasoning_tokens"] == 4
    assert data["cost_usd"] == pytest.approx((1000 * 2.0 + 10 * 8.0) / 1e6)


# --- options flow


@pytest.mark.usefixtures("mock_init_component")
async def test_options_flow(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """The options flow validates and stores price overrides."""
    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_PRICES: "{not json"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_PRICES: "invalid_prices"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_PRICES: '{"gpt-4o-mini": {"output": 1}}'}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert mock_config_entry.options == {CONF_PRICES: {"gpt-4o-mini": {"output": 1.0}}}
    assert mock_config_entry.state is ConfigEntryState.LOADED

    # Empty text clears the overrides.
    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_PRICES: ""}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options == {CONF_PRICES: {}}


@pytest.mark.usefixtures("mock_init_component")
async def test_override_changes_cost(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_stream_with_usage: AsyncMock,
) -> None:
    """An override only replaces the given keys of a model."""
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_PRICES: {"gpt-4o-mini": {"output": 1.0}}}
    )
    await hass.async_block_till_done()
    events = async_capture_events(hass, EVENT_USAGE)
    mock_stream_with_usage.usages.append(("gpt-4o-mini", _usage(1000, 0, 1000, 0)))
    await _converse(hass)
    await hass.async_block_till_done()
    assert events[0].data["cost_usd"] == pytest.approx((1000 * 0.15 + 1000 * 1.0) / 1e6)


async def test_unload_removes_tracker(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Unloading the entry forgets the tracker."""
    from custom_components.openai_usage.usage import DATA_TRACKERS  # noqa: PLC0415

    with patch("openai.resources.models.AsyncModels.list", new_callable=AsyncMock):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    assert mock_config_entry.entry_id in hass.data[DATA_TRACKERS]
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    assert mock_config_entry.entry_id not in hass.data[DATA_TRACKERS]
    assert DOMAIN == "openai_usage"
