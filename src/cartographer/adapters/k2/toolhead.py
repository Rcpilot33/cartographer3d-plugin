from __future__ import annotations

from typing_extensions import override

from cartographer.adapters.klipper.toolhead import KlipperToolhead
from cartographer.interfaces.printer import ChamberFanStatus


class K2Toolhead(KlipperToolhead):
    CHAMBER_FAN_OFF_TARGET = 80.0

    @override
    def calibration_aux_fan_gcode(self, speed: int) -> str:
        return f"M106 P2 S{speed}"

    @override
    def get_calibration_chamber_fan_status(self) -> ChamberFanStatus:
        eventtime = self.mcu.get_current_time()
        chamber_fan = self.printer.lookup_object("temperature_fan chamber_fan", None)
        if chamber_fan is None:
            msg = "K2 chamber fan status is unavailable; cannot verify thermal calibration fan state"
            raise RuntimeError(msg)
        fan_status = chamber_fan.get_status(eventtime)
        direct_fan = self.printer.lookup_object("output_pin fan1", None)
        if direct_fan is None:
            msg = "K2 case fan status is unavailable; cannot verify thermal calibration fan state"
            raise RuntimeError(msg)
        direct_speed = float(direct_fan.get_status(eventtime)["value"])
        chamber_heater = self.printer.lookup_object("heater_generic chamber_heater", None)
        heater_target = float(chamber_heater.get_status(eventtime).get("target", 0.0)) if chamber_heater else 0.0
        return ChamberFanStatus(
            target=float(fan_status["target"]),
            speed=float(fan_status["speed"]),
            direct_speed=direct_speed,
            heater_target=heater_target,
        )

    @override
    def calibration_chamber_fan_off_gcode(self) -> str:
        return (
            f"M107 P1\nSET_TEMPERATURE_FAN_TARGET TEMPERATURE_FAN=chamber_fan TARGET={self.CHAMBER_FAN_OFF_TARGET:.6f}"
        )

    @override
    def calibration_chamber_fan_restore_gcode(self, target: float) -> str:
        return f"SET_TEMPERATURE_FAN_TARGET TEMPERATURE_FAN=chamber_fan TARGET={target:.6f}"
