"""Usage sensors for OpenAI (openai_usage addition).

One device per config entry with cost and token counters. Daily counters
reset at local midnight, monthly ones at local midnight of the first day of
the month. Values survive restarts (restored when still in the same period).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .usage import (
    KIND_AI_TASK,
    KIND_CONVERSATION,
    KIND_STT,
    KIND_TTS,
    UsageRecord,
    signal_usage,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

PARALLEL_UPDATES = 0

type Period = Literal["day", "month"]


@dataclass(frozen=True, kw_only=True)
class OpenAIUsageSensorDescription(SensorEntityDescription):
    """Describes an OpenAI usage sensor."""

    period: Period
    value_fn: Callable[[UsageRecord], float]
    kind: str | None = None


def _cost(record: UsageRecord) -> float:
    return record.cost_usd


def _cost_description(
    key: str, period: Period, kind: str | None = None, enabled: bool = True
) -> OpenAIUsageSensorDescription:
    return OpenAIUsageSensorDescription(
        key=key,
        translation_key=key,
        period=period,
        kind=kind,
        value_fn=_cost,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement="USD",
        suggested_display_precision=4,
        entity_registry_enabled_default=enabled,
    )


SENSORS: tuple[OpenAIUsageSensorDescription, ...] = (
    _cost_description("cost_today", "day"),
    _cost_description("cost_month", "month"),
    OpenAIUsageSensorDescription(
        key="tokens_today",
        translation_key="tokens_today",
        period="day",
        value_fn=lambda record: record.total_tokens,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement="tokens",
    ),
    OpenAIUsageSensorDescription(
        key="requests_today",
        translation_key="requests_today",
        period="day",
        value_fn=lambda record: 1,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement="requests",
    ),
    *(
        _cost_description(f"cost_today_{kind}", "day", kind, enabled=False)
        for kind in (KIND_CONVERSATION, KIND_AI_TASK, KIND_STT, KIND_TTS)
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the usage sensors."""
    async_add_entities(
        OpenAIUsageSensor(config_entry, description) for description in SENSORS
    )


def period_start(period: Period, now: datetime) -> datetime:
    """Return the local start of the period containing ``now``."""
    local = dt_util.as_local(now)
    if period == "month":
        return dt_util.start_of_local_day(local.date().replace(day=1))
    return dt_util.start_of_local_day(local.date())


class OpenAIUsageSensor(RestoreSensor):
    """A cost/token/request counter for one period."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    entity_description: OpenAIUsageSensorDescription

    def __init__(
        self, entry: ConfigEntry, description: OpenAIUsageSensorDescription
    ) -> None:
        """Initialize the sensor."""
        self.entity_description = description
        self._entry_id = entry.entry_id
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"{entry.title} usage",
            manufacturer="OpenAI",
            model="Usage",
            entry_type=dr.DeviceEntryType.SERVICE,
        )
        self._total: float = 0.0
        self._attr_last_reset = period_start(description.period, dt_util.utcnow())

    @property
    def native_value(self) -> float | int:
        """Return the counter value."""
        if self.entity_description.device_class is SensorDeviceClass.MONETARY:
            return round(self._total, 6)
        return int(self._total)

    async def async_added_to_hass(self) -> None:
        """Restore the counter and subscribe to usage records."""
        await super().async_added_to_hass()
        start = self._attr_last_reset
        last_state = await self.async_get_last_state()
        last_data = await self.async_get_last_sensor_data()
        if last_state is not None and last_data is not None:
            restored_reset = last_state.attributes.get("last_reset")
            if isinstance(restored_reset, str):
                restored_reset = dt_util.parse_datetime(restored_reset)
            if restored_reset == start and last_data.native_value is not None:
                try:
                    self._total = float(last_data.native_value)
                except TypeError, ValueError:
                    self._total = 0.0

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_usage(self._entry_id), self._async_on_record
            )
        )
        self.async_on_remove(
            async_track_time_change(
                self.hass, self._async_on_midnight, hour=0, minute=0, second=0
            )
        )

    @callback
    def _async_roll_period(self) -> bool:
        """Reset the counter if a new period started; return True if reset."""
        start = period_start(self.entity_description.period, dt_util.utcnow())
        if start == self._attr_last_reset:
            return False
        self._attr_last_reset = start
        self._total = 0.0
        return True

    @callback
    def _async_on_midnight(self, now: datetime) -> None:
        """Reset at the start of a new period even without requests."""
        if self._async_roll_period():
            self.async_write_ha_state()

    @callback
    def _async_on_record(self, record: UsageRecord) -> None:
        """Add a usage record to the counter."""
        description = self.entity_description
        if description.kind is not None and record.kind != description.kind:
            return
        self._async_roll_period()
        self._total += description.value_fn(record)
        self.async_write_ha_state()
