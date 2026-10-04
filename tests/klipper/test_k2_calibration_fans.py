import importlib
import sys
from types import ModuleType
from unittest import TestCase
from unittest.mock import Mock, patch

from cartographer.interfaces.printer import ChamberFanStatus


class TestK2CalibrationFans(TestCase):
    def setUp(self) -> None:
        fake_base_module = ModuleType("cartographer.adapters.klipper.toolhead")

        class FakeKlipperToolhead:
            def __init__(self, config: Mock, mcu: Mock) -> None:
                self.printer = config.get_printer()
                self.mcu = mcu

        fake_base_module.KlipperToolhead = FakeKlipperToolhead  # type: ignore[attr-defined]
        self.module_patch = patch.dict(sys.modules, {"cartographer.adapters.klipper.toolhead": fake_base_module})
        self.module_patch.start()
        sys.modules.pop("cartographer.adapters.k2.toolhead", None)
        self.addCleanup(self.module_patch.stop)
        self.addCleanup(sys.modules.pop, "cartographer.adapters.k2.toolhead", None)
        k2_toolhead = importlib.import_module("cartographer.adapters.k2.toolhead").K2Toolhead
        self.printer = Mock()
        self.objects = {
            "temperature_fan chamber_fan": Mock(),
            "output_pin fan1": Mock(),
            "heater_generic chamber_heater": Mock(),
        }
        self.printer.lookup_object.side_effect = lambda name, default=None: self.objects.get(name, default)
        self.objects["temperature_fan chamber_fan"].get_status.return_value = {"target": 35.0, "speed": 0.0}
        self.objects["output_pin fan1"].get_status.return_value = {"value": 0.0}
        self.objects["heater_generic chamber_heater"].get_status.return_value = {"target": 0.0}
        config = Mock()
        config.get_printer.return_value = self.printer
        mcu = Mock()
        mcu.get_current_time.return_value = 12.5
        self.toolhead = k2_toolhead(config, mcu)

    def test_status_reads_thermostat_direct_fan_and_chamber_heater(self) -> None:
        self.assertEqual(
            self.toolhead.get_calibration_chamber_fan_status(),
            ChamberFanStatus(35.0, 0.0, 0.0, 0.0),
        )
        self.objects["temperature_fan chamber_fan"].get_status.assert_called_once_with(12.5)

    def test_off_target_and_prior_target_restoration(self) -> None:
        self.assertEqual(
            self.toolhead.calibration_chamber_fan_off_gcode(),
            "M107 P1\nSET_TEMPERATURE_FAN_TARGET TEMPERATURE_FAN=chamber_fan TARGET=80.000000",
        )
        self.assertEqual(
            self.toolhead.calibration_chamber_fan_restore_gcode(35.0),
            "SET_TEMPERATURE_FAN_TARGET TEMPERATURE_FAN=chamber_fan TARGET=35.000000",
        )

    def test_missing_case_fan_status_is_not_silently_accepted(self) -> None:
        self.objects.pop("output_pin fan1")
        with self.assertRaisesRegex(RuntimeError, "case fan status is unavailable"):
            self.toolhead.get_calibration_chamber_fan_status()
