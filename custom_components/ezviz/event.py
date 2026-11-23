"""Doorbell ring events for EZVIZ doorbells.

The cloud data only holds the latest event of a device. A doorbell press is
usually followed by a motion event within seconds, so between two polls the
ring is overwritten. When the latest event changes, read the recent ring
history from pyezvizapi and fire an event for every ring not seen before.
"""

import asyncio
import logging
from typing import override

from pyezvizapi.constants import DeviceCatagories
from pyezvizapi.devices.intercom_events import get_intercom_events
from pyezvizapi.exceptions import HTTPError, PyEzvizError

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import EzvizConfigEntry, EzvizDataUpdateCoordinator
from .entity import EzvizEntity

_LOGGER = logging.getLogger(__name__)

DOORBELL_CATEGORIES = {
    DeviceCatagories.DOORBELL_DEVICE_CATEGORY.value,
    DeviceCatagories.CAT_EYE_CATEGORY.value,
}
EVENT_RING = "ring"
# Forget old ring IDs once the set grows past this size.
MAX_SEEN_IDS = 500


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzvizConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up doorbell events based on a config entry."""
    coordinator = entry.runtime_data
    async_add_entities(
        EzvizDoorbellEvent(entry, coordinator, serial)
        for serial, data in coordinator.data.items()
        if data.get("device_category") in DOORBELL_CATEGORIES
    )


class EzvizDoorbellEvent(EzvizEntity, EventEntity):
    """Fires when someone presses the doorbell."""

    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_event_types = [EVENT_RING]
    _attr_translation_key = "doorbell"

    def __init__(
        self,
        entry: EzvizConfigEntry,
        coordinator: EzvizDataUpdateCoordinator,
        serial: str,
    ) -> None:
        """Initialize the doorbell event entity."""
        super().__init__(coordinator, serial)
        self._entry = entry
        self._attr_unique_id = f"{serial}_doorbell"
        # None until the first successful read, which only records the rings
        # that already happened.
        self._seen_ids: set[str] | None = None
        self._last_alarm_time = None
        self._lock = asyncio.Lock()

    @override
    async def async_added_to_hass(self) -> None:
        """Record the existing rings without firing events for them."""
        await super().async_added_to_hass()
        self._last_alarm_time = self.data.get("last_alarm_time")
        self._async_schedule_check()

    @callback
    @override
    def _handle_coordinator_update(self) -> None:
        """Check the ring history when the latest event of the device changes."""
        alarm_time = self.data.get("last_alarm_time")
        if alarm_time != self._last_alarm_time:
            self._last_alarm_time = alarm_time
            self._async_schedule_check()
        super()._handle_coordinator_update()

    @callback
    def _async_schedule_check(self) -> None:
        self._entry.async_create_background_task(
            self.hass, self._async_check_rings(), f"ezviz {self._serial} rings"
        )

    async def _async_check_rings(self) -> None:
        async with self._lock:
            try:
                events = await self.hass.async_add_executor_job(
                    get_intercom_events, self.coordinator.ezviz_client, (self._serial,)
                )
            except (HTTPError, PyEzvizError) as err:
                _LOGGER.debug("%s: cannot read ring history: %s", self._serial, err)
                return

            rings = [event for event in events if event.event_type == EVENT_RING]
            ring_ids = {event.event_id for event in rings}
            if self._seen_ids is None:
                self._seen_ids = ring_ids
                return

            new_rings = [
                event for event in rings if event.event_id not in self._seen_ids
            ]
            if len(self._seen_ids) > MAX_SEEN_IDS:
                self._seen_ids = set()
            self._seen_ids |= ring_ids
            if not new_rings:
                return

            _LOGGER.debug("%s: %s new ring(s)", self._serial, len(new_rings))
            self._trigger_event(EVENT_RING, {"rings": len(new_rings)})
            self.async_write_ha_state()
