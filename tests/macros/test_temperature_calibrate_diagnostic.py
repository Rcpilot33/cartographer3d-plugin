from __future__ import annotations

import csv
from contextlib import ExitStack
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from cartographer.interfaces.printer import Position, Sample, TemperatureStatus, ThermalDiagnosticStatus
from cartographer.macros.temperature_calibrate import TemperatureCalibrateMacro
from cartographer.toolhead import BacklashCompensatingToolhead
from tests.mocks.config import MockConfiguration
from tests.mocks.params import MockParams


class TestTemperatureDiagnostic(TestCase):
    def setUp(self) -> None:
        self.mcu = Mock()
        self.mcu.get_last_sample.return_value = None
        self.toolhead = Mock()
        self.toolhead.is_homed.return_value = True
        self.toolhead.get_axis_limits.return_value = (0.0, 350.0)
        self.toolhead.get_position.return_value = Position(0.0, 0.0, 0.0)
        self.toolhead.get_thermal_diagnostic_status.return_value = ThermalDiagnosticStatus(
            bed=TemperatureStatus(25.0, 0.0),
            extruder=TemperatureStatus(25.0, 0.0),
            heater_fans=(("heater_fan hotend", 0.0),),
        )
        self.config = MockConfiguration()
        self.gcode = Mock()
        self.executor = Mock()
        self.scheduler = Mock()
        self.macro = TemperatureCalibrateMacro(
            self.mcu, self.toolhead, self.config, self.gcode, self.executor, self.scheduler
        )
        self.params = MockParams()
        self.params.params.update(
            {
                "DIAGNOSTIC": "1",
                "MIN_TEMP": "50",
                "MAX_TEMP": "85",
                "BED_TEMP": "110",
                "SOAK_SECONDS": "0",
                "HEIGHT_COUNT": "3",
            }
        )

    def test_diagnostic_holds_heaters_and_never_stages_a_model(self) -> None:
        self.mcu.get_last_sample.return_value = Sample(3097000.0, 1.0, None, 52.0, 1)
        with TemporaryDirectory() as directory:
            paths = iter(f"{directory}/diagnostic_{n}.csv" for n in range(4))
            with ExitStack() as stack:
                stack.enter_context(
                    patch(
                        "cartographer.macros.temperature_calibrate.generate_filepath",
                        side_effect=lambda _: next(paths),
                    )
                )
                stack.enter_context(
                    patch.object(
                        self.macro,
                        "_wait_for_diagnostic_heaters",
                        side_effect=lambda _p, height, record: record("preheat", height),
                    )
                )
                cooldown = stack.enter_context(patch.object(self.macro, "_cool_down_phase"))
                heatup = stack.enter_context(patch.object(self.macro, "_heat_up_phase", return_value=[]))
                fit = stack.enter_context(patch("cartographer.macros.temperature_calibrate.fit_coil_temperature_model"))
                self.macro.run(self.params)

            commands = [call.args[0] for call in self.gcode.run_gcode.call_args_list]
            self.assertEqual(commands[0], "M140 S110\nM104 S60\nM106 S0\nM106 P2 S0")
            self.assertEqual(commands[-1], "M104 S0\nM140 S0\nM106 S0\nM106 P2 S0")
            self.assertEqual(cooldown.call_count, 3)
            self.assertTrue(all(call.kwargs["keep_bed_on"] for call in cooldown.call_args_list))
            self.assertEqual(heatup.call_count, 3)
            self.assertTrue(all(call.kwargs["side_fan_off"] for call in heatup.call_args_list))
            fit.assert_not_called()
            self.executor.run.assert_not_called()
            self.assertIsNone(self.config.coil.calibration)
            with open(f"{directory}/diagnostic_0.csv", newline="") as output:
                rows = list(csv.DictReader(output))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["phase"], "preheat")
            self.assertEqual(rows[0]["coil_frequency_hz"], "3097000.0")
            self.assertEqual(rows[0]["bed_target_c"], "0.0")
            self.assertEqual(rows[0]["heater_fans"], "heater_fan hotend=0.000")

    def test_rejects_active_heater_before_moving(self) -> None:
        self.toolhead.get_thermal_diagnostic_status.return_value = ThermalDiagnosticStatus(
            bed=TemperatureStatus(60.0, 60.0),
            extruder=TemperatureStatus(25.0, 0.0),
            heater_fans=(),
        )
        with self.assertRaisesRegex(RuntimeError, "idle printer"):
            self.macro.run(self.params)
        self.toolhead.move.assert_not_called()
        self.gcode.run_gcode.assert_not_called()

    def test_backlash_wrapper_passes_through_thermal_status(self) -> None:
        self.macro.toolhead = BacklashCompensatingToolhead(self.toolhead, 0.05)
        with patch.object(self.macro, "_run_diagnostic") as diagnostic:
            self.macro.run(self.params)
        self.toolhead.get_thermal_diagnostic_status.assert_called_once_with()
        diagnostic.assert_called_once()

    def test_missing_thermal_status_raises_command_error_without_moving(self) -> None:
        self.toolhead.get_thermal_diagnostic_status.return_value = None
        with self.assertRaisesRegex(RuntimeError, "status is unavailable"):
            self.macro.run(self.params)
        self.toolhead.move.assert_not_called()
        self.gcode.run_gcode.assert_not_called()

    def test_turns_heaters_off_if_a_phase_fails(self) -> None:
        with TemporaryDirectory() as directory:
            with ExitStack() as stack:
                stack.enter_context(
                    patch(
                        "cartographer.macros.temperature_calibrate.generate_filepath",
                        return_value=f"{directory}/thermal.csv",
                    )
                )
                stack.enter_context(patch.object(self.macro, "_wait_for_diagnostic_heaters"))
                stack.enter_context(patch.object(self.macro, "_cool_down_phase", side_effect=RuntimeError("stalled")))
                with self.assertRaisesRegex(RuntimeError, "stalled"):
                    self.macro.run(self.params)
            self.assertEqual(self.gcode.run_gcode.call_args_list[-1].args[0], "M104 S0\nM140 S0\nM106 S0\nM106 P2 S0")

    def test_cooldown_keeps_bed_target_in_diagnostic_mode(self) -> None:
        with patch.object(self.macro, "_wait_for_temperature"):
            self.macro._cool_down_phase(200.0, 50, 5, keep_bed_on=True)
        self.gcode.run_gcode.assert_called_once_with("M106 S255\nM106 P2 S255")

    def test_normal_cooldown_leaves_side_fan_control_unchanged(self) -> None:
        with patch.object(self.macro, "_wait_for_temperature"):
            self.macro._cool_down_phase(200.0, 50, 5)
        self.gcode.run_gcode.assert_called_once_with("M140 S0\nM106 S255")

    def test_heating_turns_side_fan_off_only_in_diagnostic_mode(self) -> None:
        with patch.object(self.macro, "_wait_for_temperature"):
            self.macro._heat_up_phase(1.0, 110, 50, 85, 5, side_fan_off=True)
        self.gcode.run_gcode.assert_called_once_with("M140 S110\nM106 S0\nM106 P2 S0")

    def test_normal_heating_leaves_side_fan_control_unchanged(self) -> None:
        with patch.object(self.macro, "_wait_for_temperature"):
            self.macro._heat_up_phase(1.0, 110, 50, 85, 5)
        self.gcode.run_gcode.assert_called_once_with("M140 S110\nM106 S0")
