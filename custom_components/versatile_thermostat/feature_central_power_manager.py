""" Implements a central Power Feature Manager for Versatile Thermostat """

import logging
import time

from typing import Any
from functools import cmp_to_key

from datetime import timedelta

from homeassistant.const import STATE_OFF
from homeassistant.core import HomeAssistant, Event, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    EventStateChangedData,
    async_call_later,
)
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.components.climate import (
    ClimateEntity,
    DOMAIN as CLIMATE_DOMAIN,
)


from .const import *  # pylint: disable=wildcard-import, unused-wildcard-import
from .commons import write_event_log
from .commons_type import ConfigData
from .base_manager import BaseFeatureManager

# circular dependency
# from .base_thermostat import BaseThermostat

MIN_DTEMP_SECS = 20

_LOGGER = logging.getLogger(__name__)


class FeatureCentralPowerManager(BaseFeatureManager):
    """A central Power feature manager"""

    def __init__(self, hass: HomeAssistant, vtherm_api: Any):
        """Init of a featureManager"""
        super().__init__(None, hass, "centralPowerManager")
        self._hass: HomeAssistant = hass
        self._vtherm_api = vtherm_api  # no type due to circular reference
        self._is_configured: bool = False
        self._power_sensor_entity_id: str | None = None
        self._max_power_sensor_entity_id: str | None = None
        self._current_power: float | None = None
        self._current_max_power: float | None = None
        self._power_temp: float | None = None
        self._cancel_calculate_shedding_call = None
        self._started_vtherm_total_power: float = 0
        # Not used now
        self._last_shedding_date = None

    def post_init(self, entry_infos: ConfigData):
        """Gets the configuration parameters"""
        central_config = self._vtherm_api.find_central_configuration()
        if not central_config:
            _LOGGER.info("No central configuration is found. Power management will be deactivated")
            return

        self._power_sensor_entity_id = entry_infos.get(CONF_POWER_SENSOR)
        self._max_power_sensor_entity_id = entry_infos.get(CONF_MAX_POWER_SENSOR)
        self._power_temp = entry_infos.get(CONF_PRESET_POWER)

        self._is_configured = False
        self._current_power = None
        self._current_max_power = None
        if (
            entry_infos.get(CONF_USE_POWER_FEATURE, False)
            and self._max_power_sensor_entity_id
            and self._power_sensor_entity_id
            and self._power_temp
        ):
            self._is_configured = True
            self._started_vtherm_total_power = 0
        else:
            _LOGGER.info("Power management is not fully configured and will be deactivated")

    async def start_listening(self):
        """Start listening the power sensor"""
        if not self._is_configured:
            return

        self.stop_listening()

        self.add_listener(
            async_track_state_change_event(
                self.hass,
                [self._power_sensor_entity_id],
                self._power_sensor_changed,
            )
        )

        self.add_listener(
            async_track_state_change_event(
                self.hass,
                [self._max_power_sensor_entity_id],
                self._max_power_sensor_changed,
            )
        )

    @callback
    async def _power_sensor_changed(self, event: Event[EventStateChangedData]):
        """Handle power changes."""
        write_event_log(_LOGGER, self, f"Receive power sensor state {event.data.get('new_state').state if event.data.get('new_state') else None}")
        # _LOGGER.debug(event)

        self._started_vtherm_total_power = 0
        await self.refresh_state()

    @callback
    async def _max_power_sensor_changed(self, event: Event[EventStateChangedData]):
        """Handle power max changes."""
        write_event_log(_LOGGER, self, f"Receive max power sensor state {event.data.get('new_state').state if event.data.get('new_state') else None}")
        # _LOGGER.debug(event)
        await self.refresh_state()

    @overrides
    async def refresh_state(self) -> bool:
        """Tries to get the last state from sensor
        Returns True if a change has been made"""

        async def _calculate_shedding_internal(_):
            _LOGGER.debug("Do the shedding calculation")
            await self.calculate_shedding()
            if self._cancel_calculate_shedding_call:
                self._cancel_calculate_shedding_call()
                self._cancel_calculate_shedding_call = None

        if not self._is_configured:
            return False

        # Retrieve current power
        new_power = get_safe_float(self._hass, self._power_sensor_entity_id)
        power_changed = new_power is not None and self._current_power != new_power
        if power_changed:
            self._current_power = new_power
            _LOGGER.debug("New current power has been retrieved: %.3f", self._current_power)

        # Retrieve max power
        new_max_power = get_safe_float(self._hass, self._max_power_sensor_entity_id)
        max_power_changed = new_max_power is not None and self._current_max_power != new_max_power
        if max_power_changed:
            self._current_max_power = new_max_power
            _LOGGER.debug("New current max power has been retrieved: %.3f", self._current_max_power)

        # Schedule shedding calculation if there's any change
        if power_changed or max_power_changed:
            if not self._cancel_calculate_shedding_call:
                self._cancel_calculate_shedding_call = async_call_later(self.hass, timedelta(seconds=MIN_DTEMP_SECS), _calculate_shedding_internal)
            return True

        return False

    # For testing purpose only, do an immediate shedding calculation
    async def _do_immediate_shedding(self):
        """Do an immmediate shedding calculation if a timer was programmed.
        Else, do nothing"""
        if self._cancel_calculate_shedding_call:
            self._cancel_calculate_shedding_call()
            self._cancel_calculate_shedding_call = None
        await self.calculate_shedding()

    async def calculate_shedding(self):
        """Do the shedding calculation and set/unset VTherm into overpowering state"""
        if not self.is_configured or self.current_max_power is None or self.current_power is None:
            return

        changed_vtherm = []

        _LOGGER.debug("-------- Start of calculate_shedding")
        # Find all VTherms
        available_power = self.current_max_power - self.current_power
        vtherms_sorted = self.find_all_vtherm_with_power_management_sorted_by_dtemp()

        # shedding only
        if available_power < 0:
            _LOGGER.debug(
                "The available power is is < 0 (%s). Set overpowering only for list: %s",
                available_power,
                vtherms_sorted,
            )
            # we will set overpowering for the nearest target temp first
            total_power_gain = 0

            for vtherm in vtherms_sorted:
                if vtherm.is_device_active and not vtherm.power_manager.is_overpowering_detected:
                    device_power = vtherm.power_manager.device_power
                    total_power_gain += device_power
                    _LOGGER.info("vtherm %s should be in overpowering state (device_power=%.2f)", vtherm.name, device_power)
                    await vtherm.power_manager.set_overpowering(True, device_power)
                    changed_vtherm.append(vtherm)

                _LOGGER.debug("after vtherm %s total_power_gain=%s, available_power=%s", vtherm.name, total_power_gain, available_power)
                if total_power_gain >= -available_power:
                    _LOGGER.debug("We have found enough vtherm to set to overpowering")
                    break
        # unshedding only
        else:
            vtherms_sorted.reverse()
            _LOGGER.debug("The available power is is > 0 (%s). Do a complete shedding/un-shedding calculation for list: %s", available_power, vtherms_sorted)

            total_power_added = 0

            for vtherm in vtherms_sorted:
                # We want to do always unshedding in order to initialize the state
                # so we cannot use is_overpowering_detected which test also UNKNOWN and UNAVAILABLE
                if vtherm.power_manager.overpowering_state == STATE_OFF:
                    continue

                power_consumption_max = device_power = vtherm.power_manager.device_power
                # calculate the power_consumption_max
                if vtherm.on_percent is not None:
                    power_consumption_max = max(
                        device_power / vtherm.nb_underlying_entities,
                        device_power * vtherm.on_percent,
                    )

                _LOGGER.debug("vtherm %s power_consumption_max is %s (device_power=%s, overclimate=%s)", vtherm.name, power_consumption_max, device_power, vtherm.is_over_climate)

                # or not ... is for initializing the overpowering state if not already done
                if total_power_added + power_consumption_max < available_power or not vtherm.power_manager.is_overpowering_detected:
                    # we count the unshedding only if the VTherm was in shedding
                    if vtherm.power_manager.is_overpowering_detected:
                        _LOGGER.info("vtherm %s should not be in overpowering state (power_consumption_max=%.2f)", vtherm.name, power_consumption_max)
                        total_power_added += power_consumption_max

                    await vtherm.power_manager.set_overpowering(False)
                    changed_vtherm.append(vtherm)

                if total_power_added >= available_power:
                    _LOGGER.debug("We have found enough vtherm to set to non-overpowering")
                    break

                _LOGGER.debug("after vtherm %s total_power_added=%s, available_power=%s", vtherm.name, total_power_added, available_power)

        # We have set the evenual new state, fr
        for vtherm in changed_vtherm:
            vtherm.requested_state.force_changed()
            await vtherm.update_states(force=True)
        self._last_shedding_date = self._vtherm_api.now
        _LOGGER.debug("-------- End of calculate_shedding")

    def get_climate_components_entities(self) -> list:
        """Get all VTherms entitites"""
        vtherms = []
        component: EntityComponent[ClimateEntity] = self._hass.data.get(
            CLIMATE_DOMAIN, None
        )
        if component:
            for entity in list(component.entities):
                # A little hack to test if the climate is a VTherm. Cannot use isinstance
                # due to circular dependency of BaseThermostat
                if (
                    entity.device_info
                    and entity.device_info.get("model", None) == DOMAIN
                ):
                    vtherms.append(entity)
        return vtherms

    def find_all_vtherm_with_power_management_sorted_by_dtemp(
        self,
    ) -> list:
        """Returns all the VTherms with power management activated"""
        entities = self.get_climate_components_entities()
        vtherms = [
            vtherm
            for vtherm in entities
            if vtherm.power_manager.is_configured and vtherm.is_on
        ]

        # sort the result with the min temp difference first. A and B should be BaseThermostat class
        def cmp_temps(a, b) -> int:
            diff_a = float("inf")
            diff_b = float("inf")
            a_target = a.target_temperature if not a.power_manager.is_overpowering_detected else a.requested_state.target_temperature
            b_target = b.target_temperature if not b.power_manager.is_overpowering_detected else b.requested_state.target_temperature
            if a.current_temperature is not None and a_target is not None:
                diff_a = a_target - a.current_temperature
            if b.current_temperature is not None and b_target is not None:
                diff_b = b_target - b.current_temperature

            if diff_a == diff_b:
                return 0
            return 1 if diff_a > diff_b else -1

        vtherms.sort(key=cmp_to_key(cmp_temps))
        return vtherms

    def find_all_over_switch_vtherms_active(self) -> list:
        """Returns all active VTherms that are over_switch type with TPI"""
        entities = self.get_climate_components_entities()
        return [
            vtherm
            for vtherm in entities
            if vtherm.is_over_switch
            and vtherm.is_on
            and vtherm.on_percent is not None
            and vtherm.on_percent > 0
        ]

    def calculate_cycle_schedule(self) -> dict:
        """Calculate stagger offsets for all active over_switch VTherms.

        Uses a shared clock so that offsets are deterministic regardless of
        when start_cycle is called (boot, force=True, periodic timer).

        Algorithm:
        1. Collect active over_switch VTherms with on_percent > 0
        2. Calculate power budget (central sensors or fallback)
        3. Shed lowest on_percent VTherms if energy demand exceeds budget
        4. Bin-pack remaining VTherms by power into time slots
        5. Calculate desired_phase for each VTherm
        6. Convert to offset relative to shared clock position

        Returns a dict {vtherm entity_id: desired_phase_sec}.
        """
        vtherms = self.find_all_over_switch_vtherms_active()

        if not vtherms:
            return {}

        # Get cycle duration (use minimum cycle_min across all vtherms)
        cycle_mins = [vt.cycle_min for vt in vtherms if vt.cycle_min > 0]
        if not cycle_mins:
            return {}
        cycle_sec = min(cycle_mins) * 60

        # Shared clock: current position within the cycle
        cycle_position = int(time.time()) % cycle_sec

        # Build list with power and on_percent info
        vtherm_infos = []
        for vt in vtherms:
            device_power = vt.power_manager.device_power if vt.power_manager.is_configured else 0
            on_pct = vt.on_percent or 0
            on_time = on_pct * cycle_sec
            vtherm_infos.append({
                "entity_id": vt.entity_id,
                "vtherm": vt,
                "device_power": device_power,
                "on_percent": on_pct,
                "on_time": on_time,
            })

        # Sort by on_percent descending (most demanding first)
        vtherm_infos.sort(key=lambda x: x["on_percent"], reverse=True)

        # Calculate power budget
        if self._is_configured and self._current_max_power is not None and self._current_power is not None:
            total_heating_power = sum(info["device_power"] for info in vtherm_infos)
            non_heating_power = max(0, (self._current_power or 0) - total_heating_power)
            p_budget = max(0, (self._current_max_power or 0) - non_heating_power)
            has_power_config = True
        else:
            p_budget = 0
            has_power_config = False

        # Energy demand check and shedding (only with power config)
        if has_power_config and p_budget > 0:
            energy_demand = sum(info["on_percent"] * info["device_power"] for info in vtherm_infos)

            if energy_demand > p_budget:
                _LOGGER.info(
                    "CentralTpiScheduler: energy demand %.0fW > budget %.0fW, shedding lowest on_percent VTherms",
                    energy_demand, p_budget,
                )
                # Shed from the lowest on_percent upward until demand fits
                sorted_by_pct_asc = sorted(vtherm_infos, key=lambda x: x["on_percent"])
                for info in sorted_by_pct_asc:
                    if energy_demand <= p_budget:
                        break
                    energy_demand -= info["on_percent"] * info["device_power"]
                    _LOGGER.info(
                        "CentralTpiScheduler: shedding %s (on_pct=%.0f%%, power=%.0fW)",
                        info["entity_id"], info["on_percent"] * 100, info["device_power"],
                    )
                    info["shed"] = True

                # Remove shed VTherms from scheduling (they'll be handled by power manager)
                vtherm_infos = [info for info in vtherm_infos if not info.get("shed")]

        if not vtherm_infos:
            return {}

        # Single VTherm: no stagger needed, phase=0
        if len(vtherm_infos) == 1:
            entity_id = vtherm_infos[0]["entity_id"]
            _LOGGER.debug("CentralTpiScheduler: single VTherm %s, phase=0", entity_id)
            return {entity_id: 0}

        # Phase placement (bin-packing by power layers)
        total_on_time = sum(info["on_time"] for info in vtherm_infos)
        schedule = {}

        if not has_power_config:
            # Fallback without power sensors
            if total_on_time <= cycle_sec:
                # Sequential: one ON at a time
                cursor = 0
                for info in vtherm_infos:
                    desired_phase = int(cursor)
                    schedule[info["entity_id"]] = desired_phase
                    cursor += info["on_time"]
                _LOGGER.info(
                    "CentralTpiScheduler: SEQUENTIAL (no power config) %d VTherms, "
                    "cycle_pos=%ds, phases=%s",
                    len(schedule), cycle_position,
                    {k: f"{v}s" for k, v in schedule.items()},
                )
            else:
                # Uniform distribution
                n = len(vtherm_infos)
                for i, info in enumerate(vtherm_infos):
                    desired_phase = int(i * cycle_sec / n)
                    schedule[info["entity_id"]] = desired_phase
                _LOGGER.info(
                    "CentralTpiScheduler: UNIFORM (no power config) %d VTherms, "
                    "cycle_pos=%ds, phases=%s",
                    len(schedule), cycle_position,
                    {k: f"{v}s" for k, v in schedule.items()},
                )
        else:
            # Bin-packing with power budget
            if p_budget <= 0:
                p_budget = sum(info["device_power"] for info in vtherm_infos)

            # Group VTherms into power slots (items that can run in parallel)
            slots = []  # list of lists of infos
            current_slot = []
            slot_power = 0

            for info in vtherm_infos:
                dp = info["device_power"]
                if slot_power > 0 and slot_power + dp <= p_budget:
                    # Fits in current slot (parallel)
                    current_slot.append(info)
                    slot_power += dp
                else:
                    # New slot
                    if current_slot:
                        slots.append(current_slot)
                    current_slot = [info]
                    slot_power = dp

            if current_slot:
                slots.append(current_slot)

            # Assign phases within each slot using dual-end spreading
            # to minimize temporal overlap when total on_time > cycle_sec
            #
            # Within a power slot, VTherms CAN run simultaneously (power fits).
            # But we WANT to minimize temporal overlap to smooth the power curve.
            # Strategy: first item starts at slot_start, last item ends at
            # slot_start + cycle_sec (wrapping), so overlap is minimized to
            # the incompressible: sum(on_times) - cycle_sec.
            cursor = 0
            for slot_idx, slot in enumerate(slots):
                slot_max_on_time = max(info["on_time"] for info in slot)

                if len(slot) == 1:
                    # Single item in slot
                    desired_phase = int(cursor)
                    schedule[slot[0]["entity_id"]] = desired_phase
                else:
                    # Multiple items sharing a power slot
                    # Sort by on_time descending within the slot
                    slot_sorted = sorted(slot, key=lambda x: x["on_time"], reverse=True)
                    slot_total_on = sum(info["on_time"] for info in slot_sorted)

                    if slot_total_on <= cycle_sec:
                        # All fit sequentially within one cycle — no overlap needed
                        sub_cursor = cursor
                        for info in slot_sorted:
                            desired_phase = int(sub_cursor)
                            schedule[info["entity_id"]] = desired_phase
                            sub_cursor += info["on_time"]
                    else:
                        # Overlap is unavoidable. Spread from both ends to minimize it.
                        # First item starts at cursor, second item ends at cursor + cycle_sec
                        # Overlap = slot_total_on - cycle_sec (incompressible minimum)
                        front_cursor = cursor
                        back_cursor = cursor + cycle_sec  # virtual end of cycle window
                        for i, info in enumerate(slot_sorted):
                            if i % 2 == 0:
                                # Pack from start
                                desired_phase = int(front_cursor) % cycle_sec
                                front_cursor += info["on_time"]
                            else:
                                # Pack from end
                                back_cursor -= info["on_time"]
                                desired_phase = int(back_cursor) % cycle_sec
                            schedule[info["entity_id"]] = desired_phase

                cursor += slot_max_on_time

            _LOGGER.info(
                "CentralTpiScheduler: BIN-PACK %d VTherms, budget=%.0fW, "
                "cycle_pos=%ds, phases=%s",
                len(schedule), p_budget, cycle_position,
                {k: f"{v}s" for k, v in schedule.items()},
            )

            if cursor > cycle_sec:
                _LOGGER.warning(
                    "CentralTpiScheduler: schedule overflows cycle by %.0fs. "
                    "Power manager will handle overflow in real-time.",
                    cursor - cycle_sec,
                )

        return schedule

    def apply_cycle_schedule(self):
        """Calculate and apply desired TPI phases to all active over_switch VTherms.
        Does NOT force-restart — the calling start_cycle handles that."""
        try:
            schedule = self.calculate_cycle_schedule()
            if not schedule:
                # Reset all phases to -1 (disabled) when no schedule applies
                vtherms = self.find_all_over_switch_vtherms_active()
                for vt in vtherms:
                    for under in vt.underlyings:
                        if hasattr(under, 'set_desired_tpi_phase'):
                            under.set_desired_tpi_phase(-1)
                return

            vtherms = self.find_all_over_switch_vtherms_active()
            vtherm_by_id = {vt.entity_id: vt for vt in vtherms}

            for entity_id, phase_sec in schedule.items():
                vt = vtherm_by_id.get(entity_id)
                if vt:
                    for under in vt.underlyings:
                        if hasattr(under, 'set_desired_tpi_phase'):
                            under.set_desired_tpi_phase(phase_sec)
        except Exception as e:
            _LOGGER.error("CentralTpiScheduler: error applying schedule: %s. Heating unaffected.", e)

    def add_started_vtherm_total_power(self, started_power: float):
        """Add the power into the _started_vtherm_total_power which holds all VTherm started after
        the last power measurement"""
        self._started_vtherm_total_power += started_power
        _LOGGER.debug("%s - started_vtherm_total_power is now %s", self, self._started_vtherm_total_power)

    @property
    def is_configured(self) -> bool:
        """True if the FeatureManager is fully configured"""
        return self._is_configured

    @property
    def current_power(self) -> float | None:
        """Return the current power from sensor"""
        return self._current_power

    @property
    def current_max_power(self) -> float | None:
        """Return the current power from sensor"""
        return self._current_max_power

    @property
    def power_temperature(self) -> float | None:
        """Return the power temperature"""
        return self._power_temp

    @property
    def power_sensor_entity_id(self) -> float | None:
        """Return the power sensor entity id"""
        return self._power_sensor_entity_id

    @property
    def max_power_sensor_entity_id(self) -> float | None:
        """Return the max power sensor entity id"""
        return self._max_power_sensor_entity_id

    @property
    def started_vtherm_total_power(self) -> float | None:
        """Return the started_vtherm_total_power"""
        return self._started_vtherm_total_power

    def __str__(self):
        return "CentralPowerManager"
