from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from cartographer.coil.temperature_compensation import CoilTemperatureCompensationModel
from cartographer.interfaces.configuration import CoilCalibrationConfiguration

if TYPE_CHECKING:
    from numpy.typing import NDArray

    from cartographer.interfaces.printer import CoilCalibrationReference, Sample


class _CalibrationReferenceMcu:
    def __init__(self, reference: CoilCalibrationReference) -> None:
        self.reference = reference

    def get_coil_reference(self) -> CoilCalibrationReference:
        return self.reference


def fit_coil_temperature_model(
    data_per_height: dict[float, list[Sample]], ref: CoilCalibrationReference
) -> CoilCalibrationConfiguration:
    """
    Fits a coil temperature compensation model across multiple probe heights.

    For each height, this function:
    1. Processes temperature-frequency sample data to extract quadratic coefficients
    2. Fits linear relationships between frequency and the quadratic coefficients
    3. Returns calibration parameters for temperature compensation

    Args:
        data_per_height: Dictionary mapping probe heights (mm) to lists of temperature/frequency samples
        ref: Reference configuration containing baseline frequency values

    Returns:
        CoilCalibrationConfiguration containing the linear relationship parameters
        for temperature compensation coefficients 'a' and 'b'
    """
    coefficients_a: list[float] = []
    coefficients_b: list[float] = []
    frequencies: list[float] = []

    for _, samples in data_per_height.items():
        (a, b, freq_at_vertex) = _process_samples(samples)
        coefficients_a.append(a)  # 'a' coefficient from quadratic fit
        coefficients_b.append(b)  # 'b' coefficient from quadratic fit
        frequencies.append(freq_at_vertex)  # frequency at quadratic vertex

    # Fit linear relationship: coefficient_a = linear_a * (freq - min_freq) + linear_b
    freq_array: NDArray[np.float_] = np.asarray(frequencies) - ref.min_frequency
    linear_params_a = _least_squares(freq_array, coefficients_a)

    # Fit linear relationship: coefficient_b = linear_a * (freq - min_freq) + linear_b
    linear_params_b = _least_squares(freq_array, coefficients_b)

    return CoilCalibrationConfiguration(
        a_a=linear_params_a[0],  # Slope for 'a' coefficient vs frequency
        a_b=linear_params_a[1],  # Intercept for 'a' coefficient vs frequency
        b_a=linear_params_b[0],  # Slope for 'b' coefficient vs frequency
        b_b=linear_params_b[1],  # Intercept for 'b' coefficient vs frequency
    )


def validate_coil_temperature_model(
    config: CoilCalibrationConfiguration,
    data_per_height: dict[float, list[Sample]],
    reference: CoilCalibrationReference,
) -> None:
    """Reject compensation that breaks probe ordering or fails to reduce measured drift."""
    coefficients = (config.a_a, config.a_b, config.b_a, config.b_b)
    if not all(np.isfinite(coefficient) for coefficient in coefficients):
        msg = "Coil calibration produced non-finite coefficients; no calibration was staged"
        raise RuntimeError(msg)

    samples = [sample for height_samples in data_per_height.values() for sample in height_samples]
    if not samples:
        msg = "Coil calibration has no samples; no calibration was staged"
        raise RuntimeError(msg)

    frequencies = np.asarray([sample.frequency for sample in samples])
    temperatures = np.asarray([sample.temperature for sample in samples])
    if not np.all(np.isfinite(frequencies)) or not np.all(np.isfinite(temperatures)):
        msg = "Coil calibration contains non-finite samples; no calibration was staged"
        raise RuntimeError(msg)

    compensation = CoilTemperatureCompensationModel(config, _CalibrationReferenceMcu(reference))
    frequency_grid = np.linspace(float(frequencies.min()), float(frequencies.max()), 9)
    temperature_grid = np.linspace(float(temperatures.min()), float(temperatures.max()), 5)
    target_temperature = float(np.median(temperatures))

    try:
        for source_temperature in temperature_grid:
            for frequency in frequency_grid:
                same = compensation.compensate(float(frequency), float(source_temperature), float(source_temperature))
                if not np.isfinite(same) or abs(same - frequency) > 1.0:
                    msg = "Coil calibration fails same-temperature identity; no calibration was staged"
                    raise RuntimeError(msg)
            for target in (float(temperature_grid[0]), target_temperature, float(temperature_grid[-1])):
                corrected = [
                    compensation.compensate(float(frequency), float(source_temperature), target)
                    for frequency in frequency_grid
                ]
                if not np.all(np.isfinite(corrected)) or np.any(np.diff(corrected) <= 0.0):
                    msg = "Coil calibration reverses or collapses probe frequency; no calibration was staged"
                    raise RuntimeError(msg)
    except (ArithmeticError, ValueError) as exc:
        msg = "Coil calibration compensation is undefined; no calibration was staged"
        raise RuntimeError(msg) from exc

    raw_error = 0.0
    corrected_error = 0.0
    measured_heights = 0
    for height_samples in data_per_height.values():
        if not height_samples:
            continue
        height_temperatures = np.asarray([sample.temperature for sample in height_samples])
        centers = np.linspace(float(height_temperatures.min()) + 2.0, float(height_temperatures.max()) - 2.0, 8)
        raw_medians: list[float] = []
        corrected_medians: list[float] = []
        for center in centers:
            nearby = [sample for sample in height_samples if abs(sample.temperature - center) <= 1.0]
            if len(nearby) < 3:
                continue
            raw_medians.append(float(np.median([sample.frequency for sample in nearby])))
            try:
                corrected = [
                    compensation.compensate(sample.frequency, sample.temperature, target_temperature)
                    for sample in nearby
                ]
            except (ArithmeticError, ValueError) as exc:
                msg = "Coil calibration compensation is undefined; no calibration was staged"
                raise RuntimeError(msg) from exc
            if not np.all(np.isfinite(corrected)):
                msg = "Coil calibration compensation is non-finite; no calibration was staged"
                raise RuntimeError(msg)
            corrected_medians.append(float(np.median(corrected)))
        if len(raw_medians) < 3:
            msg = "Coil calibration has insufficient temperature coverage; no calibration was staged"
            raise RuntimeError(msg)
        raw_error += (max(raw_medians) - min(raw_medians)) ** 2
        corrected_error += (max(corrected_medians) - min(corrected_medians)) ** 2
        measured_heights += 1

    if measured_heights < 3 or corrected_error >= raw_error:
        msg = "Coil calibration does not reduce measured frequency drift; no calibration was staged"
        raise RuntimeError(msg)


def _least_squares(x: NDArray[np.float_], y: NDArray[np.float_] | list[float]) -> NDArray[np.float_]:
    """Fit a slope and intercept without requiring SciPy."""
    design = np.column_stack((x, np.ones_like(x)))
    coefficients, _, _, _ = np.linalg.lstsq(design, np.asarray(y), rcond=None)
    return coefficients


def _fit_nonnegative_quadratic(
    temperatures: NDArray[np.float_], frequencies: NDArray[np.float_]
) -> tuple[float, float, float]:
    """Fit a quadratic with a >= 0, refitting on the boundary when needed."""
    design = np.column_stack((temperatures**2, temperatures, np.ones_like(temperatures)))
    coefficients, _, _, _ = np.linalg.lstsq(design, frequencies, rcond=None)
    a, b, c = coefficients
    if a < 0:
        b, c = _least_squares(temperatures, frequencies)
        a = 0.0
    return float(a), float(b), float(c)


def _fit_fixed_vertex(
    temperatures: NDArray[np.float_], frequencies: NDArray[np.float_], vertex: float
) -> tuple[float, float]:
    """Fit a quadratic constrained to a vertex at 0 or 120 C and a >= 0."""
    column = temperatures**2 - 2 * vertex * temperatures
    a, c = _least_squares(column, frequencies)
    if a < 0:
        a = 0.0
        c = float(np.mean(frequencies))
    return float(a), float(c)


def _downsample_by_temperature(samples: list[Sample], target_count: int) -> list[Sample]:
    """Downsample samples to ensure even distribution across temperature ranges."""
    if len(samples) <= target_count:
        return samples

    temperatures = [s.temperature for s in samples]
    temp_min, temp_max = min(temperatures), max(temperatures)

    # Create temperature bins
    n_bins = 10  # Adjust based on your needs
    bin_width = (temp_max - temp_min) / n_bins
    samples_per_bin = target_count // n_bins

    binned_samples: list[list[Sample]] = [[] for _ in range(n_bins)]

    # Sort samples into temperature bins
    for sample in samples:
        bin_idx = min(int((sample.temperature - temp_min) / bin_width), n_bins - 1)
        binned_samples[bin_idx].append(sample)

    # Sample evenly from each bin
    downsampled: list[Sample] = []
    for bin_samples in binned_samples:
        if not bin_samples:
            continue
        # Take evenly spaced samples from this bin
        step = max(1, len(bin_samples) // samples_per_bin)
        downsampled.extend(bin_samples[::step][:samples_per_bin])

    return downsampled


def _process_samples(samples: list[Sample]) -> tuple[float, float, float]:
    """
    Processes temperature-frequency samples to extract quadratic relationship coefficients.

    Fits a quadratic function: frequency = a*temperature² + b*temperature + c

    The function handles three cases based on where the quadratic's vertex falls:
    - Normal case (vertex 0-120°C): Uses full quadratic fit
    - Hot case (vertex >120°C): Constrains vertex to 120°C using line120
    - Cold case (vertex <0°C): Constrains vertex to 0°C using line0

    Args:
        samples: List of Sample objects containing temperature and frequency data

    Returns:
        Tuple containing (a_coefficient, b_coefficient, frequency_at_vertex)

    Raises:
        RuntimeError: If fewer than 300 samples provided (insufficient for calibration)
    """
    if len(samples) < 300:
        msg = f"Insufficient samples for calibration: {len(samples)} (need at least 300)"
        raise RuntimeError(msg)

    # Downsample if we have too many samples to improve processing speed
    if len(samples) > 1000:
        samples = _downsample_by_temperature(samples, target_count=800)

    frequencies = np.asarray([s.frequency for s in samples])
    temperatures = np.asarray([s.temperature for s in samples])

    # Fit quadratic: freq = a*temp² + b*temp + c
    a, b, c = _fit_nonnegative_quadratic(temperatures, frequencies)

    # A linear boundary fit has no finite vertex. Route rising frequency to the
    # cold-vertex refit, matching the bounded SciPy fit's near-zero positive a.
    vertex_temperature = -b / (2 * a) if a > 0 else (float("inf") if b < 0 else float("-inf"))

    # Handle constrained cases based on vertex position
    if vertex_temperature > 120:
        # Vertex too hot - constrain to 120°C
        constrained_a, constrained_c = _fit_fixed_vertex(temperatures, frequencies, 120.0)
        return (
            constrained_a,  # a coefficient
            -240 * constrained_a,  # b coefficient (from line120 constraint)
            constrained_c - 120**2 * constrained_a,  # freq at vertex (120°C)
        )

    elif vertex_temperature < 0:
        # Vertex too cold - constrain to 0°C
        constrained_a, constrained_c = _fit_fixed_vertex(temperatures, frequencies, 0.0)
        return (
            constrained_a,  # a coefficient
            0,  # b coefficient (from line0 constraint)
            constrained_c,  # freq at vertex (0°C)
        )

    # Normal case - vertex within reasonable range (0-120°C)
    # Calculate frequency at the vertex and return coefficients
    frequency_at_vertex = a * vertex_temperature**2 + b * vertex_temperature + c
    return (a, b, frequency_at_vertex)
