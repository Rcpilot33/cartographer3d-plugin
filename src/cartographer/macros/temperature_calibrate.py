from __future__ import annotations

import csv
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, final

from typing_extensions import override

from cartographer.coil.calibration import fit_coil_temperature_model, validate_coil_temperature_model
from cartographer.interfaces.printer import (
    ChamberFanStatus,
    GCodeDispatch,
    Macro,
    MacroParams,
    Mcu,
    Sample,
    ThermalDiagnosticStatus,
    Toolhead,
)
from cartographer.lib.csv import generate_filepath, write_samples_to_csv
from cartographer.lib.log import log_duration
from cartographer.macros.fields import param, parse

if TYPE_CHECKING:
    from cartographer.interfaces.configuration import Configuration
    from cartographer.interfaces.multiprocessing import Scheduler, TaskExecutor

logger = logging.getLogger(__name__)

# Temperature monitoring constants
TEMP_CHECK_INTERVAL = 1.0  # Check temperature every second
PROGRESS_LOG_INTERVAL = 30.0  # Log progress every 30 seconds
STALL_WARNING_TIME = 60.0  # Warn after 60 seconds of no progress
STALL_ABORT_TIME = 300.0  # Abort after 5 minutes of no progress
MAX_PHASE_TIME = 5400.0  #  Abort after 90 minutes for any single phase


class TemperatureStallError(RuntimeError):
    """Raised when temperature stops making progress toward the target."""


@dataclass(frozen=True)
class TemperatureCalibrateParams:
    """Parameters for CARTOGRAPHER_CALIBRATE_TEMPERATURE."""

    min_temp: int = param("Minimum coil temperature", default=40, min=40, max=50)
    max_temp: int = param("Maximum coil temperature", default=60, min=60, max=90)
    bed_temp: int = param("Bed temperature target", default=90, min=90, max=120)
    z_speed: int = param("Z movement speed", default=5, min=1)
    diagnostic: int = param("Collect thermal diagnostics without saving a model (0 or 1)", default=0, min=0, max=1)
    extruder_temp: int = param("Extruder target during calibration", default=60, min=50, max=100)
    soak_seconds: int = param("Heater soak before sampling", default=120, min=0, max=600)
    height_count: int = param("Number of diagnostic heights to sample", default=1, min=1, max=3)


@final
class TemperatureCalibrateMacro(Macro):
    description = "Calibrate temperature compensation for frequency drift"

    def __init__(
        self,
        mcu: Mcu,
        toolhead: Toolhead,
        config: Configuration,
        gcode: GCodeDispatch,
        task_executor: TaskExecutor,
        scheduler: Scheduler,
    ) -> None:
        self.mcu = mcu
        self.toolhead = toolhead
        self.config = config
        self.gcode = gcode
        self.task_executor = task_executor
        self.scheduler = scheduler

    @override
    def run(self, params: MacroParams) -> None:
        p = parse(TemperatureCalibrateParams, params)

        if p.max_temp < p.min_temp + 20:
            msg = f"MAX_TEMP ({p.max_temp}) must be at least MIN_TEMP + 20 ({p.min_temp + 20})"
            raise RuntimeError(msg)
        if p.bed_temp < p.max_temp:
            msg = f"BED_TEMP ({p.bed_temp}) must be at least MAX_TEMP ({p.max_temp})"
            raise RuntimeError(msg)

        if not self.toolhead.is_homed("x") or not self.toolhead.is_homed("y") or not self.toolhead.is_homed("z"):
            msg = "Must home axes before temperature calibration"
            raise RuntimeError(msg)

        initial = self._thermal_status()
        if initial.bed.target or initial.extruder.target:
            msg = "Temperature calibration requires an idle printer with bed and extruder targets at zero"
            raise RuntimeError(msg)
        chamber_fan = self.toolhead.get_calibration_chamber_fan_status()
        if chamber_fan is not None and chamber_fan.heater_target > 0.0:
            msg = "Temperature calibration requires the chamber heater target at zero"
            raise RuntimeError(msg)

        _, max_z = self.toolhead.get_axis_limits("z")
        cooling_height = max_z * 2 / 3
        logger.info(
            "Starting temperature calibration sequence... (bed=%d°C range=%d-%d°C, cooling height=%.1fmm)",
            p.bed_temp,
            p.min_temp,
            p.max_temp,
            cooling_height,
        )
        self.toolhead.move(z=cooling_height, speed=p.z_speed)
        self.toolhead.move(
            x=self.config.bed_mesh.zero_reference_position[0],
            y=self.config.bed_mesh.zero_reference_position[1],
            speed=self.config.general.travel_speed,
        )

        self._run_controlled(p, cooling_height, chamber_fan)

    def _run_controlled(
        self, p: TemperatureCalibrateParams, cooling_height: float, chamber_fan: ChamberFanStatus | None
    ) -> None:
        """Collect a thermally controlled run; only validated normal runs stage a model."""
        telemetry_name = "temp_calib_thermal_diagnostic" if p.diagnostic else "temp_calib_thermal_telemetry"
        path = generate_filepath(telemetry_name)
        csv_files: list[str] = []
        data_per_height: dict[float, list[Sample]] = {}
        with open(path, "w", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(
                [
                    "monotonic_time",
                    "phase",
                    "height_mm",
                    "coil_temperature_c",
                    "coil_frequency_hz",
                    "bed_temperature_c",
                    "bed_target_c",
                    "extruder_temperature_c",
                    "extruder_target_c",
                    "heater_fans",
                    "chamber_fan_target_c",
                    "chamber_fan_speed",
                    "direct_case_fan_speed",
                ]
            )

            def record(phase: str, height: float) -> None:
                sample = self.mcu.get_last_sample()
                thermal = self._thermal_status()
                current_chamber_fan = self.toolhead.get_calibration_chamber_fan_status()
                writer.writerow(
                    [
                        time.monotonic(),
                        phase,
                        height,
                        sample.temperature if sample else "",
                        sample.frequency if sample else "",
                        thermal.bed.current,
                        thermal.bed.target,
                        thermal.extruder.current,
                        thermal.extruder.target,
                        ";".join(f"{name}={speed:.3f}" for name, speed in thermal.heater_fans),
                        current_chamber_fan.target if current_chamber_fan else "",
                        current_chamber_fan.speed if current_chamber_fan else "",
                        current_chamber_fan.direct_speed if current_chamber_fan else "",
                    ]
                )
                output.flush()
                if (
                    phase != "preheat"
                    and current_chamber_fan is not None
                    and (current_chamber_fan.speed > 0.01 or current_chamber_fan.direct_speed > 0.01)
                ):
                    msg = "K2 chamber fan turned on during calibration; no calibration was staged"
                    raise RuntimeError(msg)

            logger.info(
                "Keeping bed at %d°C and extruder at %d°C through all phases; diagnostic=%d. Telemetry: %s",
                p.bed_temp,
                p.extruder_temp,
                p.diagnostic,
                path,
            )
            try:
                commands = [f"M140 S{p.bed_temp}", f"M104 S{p.extruder_temp}", "M106 S0"]
                aux_off = self.toolhead.calibration_aux_fan_gcode(0)
                if aux_off:
                    commands.append(aux_off)
                chamber_off = self.toolhead.calibration_chamber_fan_off_gcode()
                if chamber_off:
                    commands.append(chamber_off)
                self.gcode.run_gcode("\n".join(commands))
                self._wait_for_diagnostic_heaters(p, cooling_height, record)
                heights = (1, 2, 3)[: p.height_count] if p.diagnostic else (1, 2, 3)
                for phase, height in enumerate(heights, 1):
                    logger.info("Starting phase %d of %d (height=%dmm)", phase, len(heights), height)
                    self._cool_down_phase(
                        cooling_height,
                        p.min_temp,
                        p.z_speed,
                        record=lambda h=cooling_height: record("cooldown", h),
                        keep_bed_on=True,
                    )
                    samples = self._heat_up_phase(
                        height,
                        p.bed_temp,
                        p.min_temp,
                        p.max_temp,
                        p.z_speed,
                        record=lambda h=height: record("heating", h),
                        side_fan_off=True,
                    )
                    data_per_height[float(height)] = samples
                    prefix = "temp_calib_diagnostic" if p.diagnostic else "temp_calib"
                    sample_path = generate_filepath(f"{prefix}_h{height}mm")
                    write_samples_to_csv(samples, sample_path)
                    csv_files.append(sample_path)
                    logger.info("Phase %d: %d samples written to %s", phase, len(samples), sample_path)
            finally:
                try:
                    self.toolhead.move(z=cooling_height, speed=p.z_speed)
                    self.toolhead.wait_moves()
                except Exception:
                    logger.exception("Could not lift probe after calibration; check printer status")
                try:
                    commands = ["M104 S0", "M140 S0", "M106 S0"]
                    aux_off = self.toolhead.calibration_aux_fan_gcode(0)
                    if aux_off:
                        commands.append(aux_off)
                    self.gcode.run_gcode("\n".join(commands))
                except Exception:
                    logger.exception("Could not turn off calibration heaters and fans; check printer status")
                if chamber_fan is not None:
                    try:
                        restore = self.toolhead.calibration_chamber_fan_restore_gcode(chamber_fan.target)
                        if restore:
                            self.gcode.run_gcode(restore)
                    except Exception:
                        logger.exception("Could not restore the prior chamber-fan target; check printer status")

        if p.diagnostic:
            logger.info(
                "Thermal diagnostic complete; no calibration staged. Telemetry: %s\nRaw samples:\n%s",
                path,
                "\n".join(csv_files),
            )
            return

        reference = self.mcu.get_coil_reference()
        model = self.task_executor.run(fit_coil_temperature_model, data_per_height, reference)
        self.task_executor.run(validate_coil_temperature_model, model, data_per_height, reference)
        self.config.save_coil_model(model)
        logger.info(
            "Temperature calibration complete!\n"
            "The SAVE_CONFIG command will update the printer config file and restart the printer.\n"
            "Telemetry: %s\nRaw samples:\n%s",
            path,
            "\n".join(csv_files),
        )

    def _wait_for_diagnostic_heaters(
        self,
        p: TemperatureCalibrateParams,
        height: float,
        record: Callable[[str, float], None],
    ) -> None:
        start = time.monotonic()
        ready_since: float | None = None
        while True:
            self.scheduler.sleep(TEMP_CHECK_INTERVAL)
            record("preheat", height)
            thermal = self._thermal_status()
            now = time.monotonic()
            ready = (
                thermal.bed.current >= p.bed_temp - 1
                and p.extruder_temp - 2 <= thermal.extruder.current <= p.extruder_temp + 2
            )
            chamber_fan = self.toolhead.get_calibration_chamber_fan_status()
            if chamber_fan is not None:
                ready = ready and chamber_fan.speed <= 0.01 and chamber_fan.direct_speed <= 0.01
                if (chamber_fan.speed > 0.01 or chamber_fan.direct_speed > 0.01) and now - start >= 60:
                    msg = "K2 chamber fan did not turn off within 60 seconds; no calibration was staged"
                    raise TemperatureStallError(msg)
            if ready:
                if ready_since is None:
                    ready_since = now
                if now - ready_since >= p.soak_seconds:
                    return
            else:
                ready_since = None
            if now - start >= 3600:
                msg = "Calibration bed/extruder targets did not stabilize within 60 minutes"
                raise TemperatureStallError(msg)

    def _thermal_status(self) -> ThermalDiagnosticStatus:
        try:
            status = self.toolhead.get_thermal_diagnostic_status()
        except (AttributeError, KeyError, TypeError) as exc:
            msg = f"Unable to read diagnostic heater/fan status: {exc}"
            raise RuntimeError(msg) from exc
        if not isinstance(status, ThermalDiagnosticStatus):
            msg = "Diagnostic heater/fan status is unavailable from the active toolhead"
            raise RuntimeError(msg)
        return status

    @log_duration("Cooldown phase")
    def _cool_down_phase(
        self,
        height: float,
        min_temp: int,
        z_speed: int,
        *,
        record: Callable[[], None] | None = None,
        keep_bed_on: bool = False,
    ) -> None:
        """Cool down the probe to minimum temperature."""
        logger.info("Cooling probe to %d°C, moving to z %.1f", min_temp, height)

        self.toolhead.move(z=height, speed=z_speed)
        self.toolhead.wait_moves()
        commands = ["M106 S255"] if keep_bed_on else ["M140 S0", "M106 S255"]
        if keep_bed_on:
            aux_on = self.toolhead.calibration_aux_fan_gcode(255)
            if aux_on:
                commands.append(aux_on)
        self.gcode.run_gcode("\n".join(commands))

        logger.info("Waiting for coil temperature to reach %d°C", min_temp)
        self._wait_for_temperature(target_temp=min_temp, cooling=True, record=record)

    @log_duration("Heat up phase")
    def _heat_up_phase(
        self,
        height: float,
        bed_temp: int,
        min_temp: int,
        max_temp: int,
        z_speed: int,
        *,
        record: Callable[[], None] | None = None,
        side_fan_off: bool = False,
    ) -> list[Sample]:
        """Heat up and collect samples during temperature rise."""
        logger.info("Starting heaters: bed=%d°C, moving to z %.1f", bed_temp, height)
        commands = [f"M140 S{bed_temp}", "M106 S0"]
        if side_fan_off:
            aux_off = self.toolhead.calibration_aux_fan_gcode(0)
            if aux_off:
                commands.append(aux_off)
        self.gcode.run_gcode("\n".join(commands))

        self.toolhead.move(z=height, speed=z_speed)
        self.toolhead.wait_moves()

        self._wait_for_temperature(target_temp=min_temp - 1, cooling=False, record=record)

        logger.info("Collecting data for height %.1f", height)
        samples: list[Sample] = []

        self.mcu.register_callback(samples.append)
        try:
            self._wait_for_temperature(target_temp=max_temp, cooling=False, record=record)
        finally:
            self.mcu.unregister_callback(samples.append)

        return samples

    def _get_current_temperature(self) -> float | None:
        """Get the current coil temperature from the last sample."""
        sample = self.mcu.get_last_sample()
        return sample.temperature if sample is not None else None

    def _wait_for_temperature(self, target_temp: int, cooling: bool, record: Callable[[], None] | None = None) -> None:
        """
        Wait for coil temperature with progress monitoring.

        Tracks the closest distance to target and detects stalls when
        no progress is made for too long.

        Parameters
        ----------
        target_temp
            The target temperature to reach.
        cooling
            True if waiting for temperature to decrease, False for increase.
        """
        best_remaining: float | None = None
        last_progress_time: float = time.monotonic()
        last_log_time: float = 0.0
        warning_logged = False
        phase = "cool to" if cooling else "heat to"
        phase_start_time: float = time.monotonic()

        while True:
            self.scheduler.sleep(TEMP_CHECK_INTERVAL)
            if record is not None:
                record()

            current_temp = self._get_current_temperature()
            if current_temp is None:
                continue

            # Check if we've reached the target
            if cooling and current_temp <= target_temp:
                logger.info("Reached target temperature: %.1f°C", current_temp)
                return
            if not cooling and current_temp >= target_temp:
                logger.info("Reached target temperature: %.1f°C", current_temp)
                return

            current_time = time.monotonic()

            elapsed = current_time - phase_start_time
            if elapsed >= MAX_PHASE_TIME:
                action = "cooling" if cooling else "heating"
                msg = (
                    f"Temperature {action} phase exceeded maximum time "
                    f"({MAX_PHASE_TIME / 60:.0f} minutes). "
                    f"Current: {current_temp:.1f}°C, target: {target_temp}°C."
                )
                raise TemperatureStallError(msg)

            remaining = abs(current_temp - target_temp)

            # Check if we made progress (got closer to target)
            if best_remaining is None or remaining < best_remaining:
                best_remaining = remaining
                last_progress_time = current_time
                warning_logged = False

            # Log progress at intervals
            if current_time - last_log_time >= PROGRESS_LOG_INTERVAL:
                logger.info(
                    "Temperature: %.1f°C (%s %d°C, %.1f°C remaining)",
                    current_temp,
                    phase,
                    target_temp,
                    remaining,
                )
                last_log_time = current_time

            # Check for stall (no new progress for too long)
            stall_duration = current_time - last_progress_time
            if stall_duration >= STALL_WARNING_TIME:
                warning_logged = self._handle_stall(
                    stall_duration,
                    current_temp,
                    best_remaining,
                    target_temp,
                    cooling,
                    warning_logged,
                )

    def _handle_stall(
        self,
        stall_duration: float,
        current_temp: float,
        best_remaining: float,
        target_temp: int,
        cooling: bool,
        warning_logged: bool,
    ) -> bool:
        """
        Handle a temperature stall condition.

        Returns whether a warning has been logged.
        """
        action = "cooling" if cooling else "heating"
        if stall_duration >= STALL_ABORT_TIME:
            if cooling:
                suggestion = "If you have an enclosure, try opening the chamber door to improve airflow."
            else:
                suggestion = "If you have an enclosure, try closing the chamber door to retain heat."

            msg = (
                f"Temperature {action} stalled for "
                f"{stall_duration / 60:.0f} minutes: "
                f"stuck at {current_temp:.1f}°C, "
                f"need to reach {target_temp}°C "
                f"({best_remaining:.1f}°C remaining). "
                f"{suggestion}"
            )
            raise TemperatureStallError(msg)

        if not warning_logged:
            if cooling:
                hint = "Consider opening the chamber door if enclosed."
            else:
                hint = "Consider closing the chamber door if enclosed."

            time_until_abort = (STALL_ABORT_TIME - stall_duration) / 60
            logger.warning(
                "Temperature %s appears stalled at %.1f°C "
                "(%.1f°C from target) for %.0fs. %s "
                "Will abort if no progress in %.0f minutes.",
                action,
                current_temp,
                best_remaining,
                stall_duration,
                hint,
                time_until_abort,
            )
            return True

        return warning_logged
