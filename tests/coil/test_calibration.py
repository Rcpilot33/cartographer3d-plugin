from __future__ import annotations

import unittest
from importlib.util import find_spec
from unittest.mock import patch

import numpy as np

from cartographer.coil.calibration import (
    _downsample_by_temperature,
    _process_samples,
    fit_coil_temperature_model,
    validate_coil_temperature_model,
)
from cartographer.coil.helpers import line0, line120, line_fit
from cartographer.interfaces.configuration import CoilCalibrationConfiguration
from cartographer.interfaces.printer import CoilCalibrationReference, Sample


def _samples(a: float, b: float, c: float, count: int = 300, *, noise: float = 0.0) -> list[Sample]:
    rng = np.random.default_rng(7)
    temperatures = np.linspace(40.0, 70.0, count)
    frequencies = a * temperatures**2 + b * temperatures + c
    if noise:
        frequencies += rng.normal(0.0, noise, count)
    return [
        Sample(frequency=float(freq), time=float(i), position=None, temperature=float(temp), raw_count=0)
        for i, (temp, freq) in enumerate(zip(temperatures, frequencies))
    ]


class TestCoilCalibration(unittest.TestCase):
    def test_normal_vertex_and_minimum_sample_count(self) -> None:
        a, b, frequency = _process_samples(_samples(0.02, -1.6, 3_000_000.0))
        self.assertAlmostEqual(a, 0.02, places=7)
        self.assertAlmostEqual(b, -1.6, places=7)
        self.assertAlmostEqual(frequency, 2_999_968.0, places=5)

    def test_hot_vertex_is_constrained_to_120(self) -> None:
        a, b, frequency = _process_samples(_samples(0.02, -6.0, 3_000_000.0))
        self.assertGreater(a, 0.0)
        self.assertAlmostEqual(b, -240.0 * a, places=7)
        self.assertTrue(np.isfinite(frequency))

    def test_cold_vertex_is_constrained_to_zero(self) -> None:
        a, b, frequency = _process_samples(_samples(0.02, 0.2, 3_000_000.0))
        self.assertGreaterEqual(a, 0.0)
        self.assertEqual(b, 0.0)
        self.assertTrue(np.isfinite(frequency))

    def test_negative_curvature_uses_nonnegative_boundary(self) -> None:
        a, b, frequency = _process_samples(_samples(-0.0001, -1.0, 3_000_000.0))
        self.assertGreaterEqual(a, 0.0)
        self.assertTrue(np.isfinite(b))
        self.assertTrue(np.isfinite(frequency))

    def test_negative_curvature_with_rising_frequency_uses_cold_vertex(self) -> None:
        a, b, frequency = _process_samples(_samples(-1.3, 235.0, 3_056_000.0))
        self.assertGreater(a, 0.0)
        self.assertEqual(b, 0.0)
        self.assertTrue(np.isfinite(frequency))

    def test_three_height_downward_curves_do_not_produce_linear_only_model(self) -> None:
        # These curves approximate the three measured heights that exposed the boundary bug.
        data = {
            1.0: _samples(-4.7, 771.0, 3_068_223.0),
            2.0: _samples(-1.3, 235.0, 3_056_031.0),
            3.0: _samples(-1.5, 198.0, 3_039_918.0),
        }
        model = fit_coil_temperature_model(data, CoilCalibrationReference(2_943_053.8, 25.0))
        self.assertNotEqual((model.a_a, model.a_b), (0.0, 0.0))
        self.assertAlmostEqual(model.b_a, 0.0, places=10)
        self.assertAlmostEqual(model.b_b, 0.0, places=7)

    def test_zero_curvature_does_not_divide_by_zero(self) -> None:
        a, b, frequency = _process_samples(_samples(0.0, 0.0, 3_000_000.0))
        self.assertGreaterEqual(a, 0.0)
        self.assertTrue(np.isfinite(b))
        self.assertAlmostEqual(frequency, 3_000_000.0, places=5)

    def test_noisy_downsampled_three_height_fit(self) -> None:
        data = {
            height: _samples(
                0.02 + height * 0.001,
                -1.6 - height * 0.05,
                3_000_000 + height * 1000,
                1200,
                noise=0.01,
            )
            for height in (1.0, 2.0, 3.0)
        }
        model = fit_coil_temperature_model(data, CoilCalibrationReference(2_900_000.0, 25.0))
        for coefficient in (model.a_a, model.a_b, model.b_a, model.b_b):
            self.assertTrue(np.isfinite(coefficient))

    def test_three_height_coefficients_have_unchanged_format(self) -> None:
        data = {}
        for height in (1.0, 2.0, 3.0):
            a = 0.02 + 0.001 * height
            b = -80 * a
            vertex_frequency = 3_000_000 + 1000 * height
            data[height] = _samples(a, b, vertex_frequency + 1600 * a)

        model = fit_coil_temperature_model(data, CoilCalibrationReference(3_000_000.0, 25.0))
        self.assertAlmostEqual(model.a_a, 1e-6, places=11)
        self.assertAlmostEqual(model.a_b, 0.02, places=7)
        self.assertAlmostEqual(model.b_a, -80e-6, places=9)
        self.assertAlmostEqual(model.b_b, -1.6, places=7)

    def test_rejects_fewer_than_300_samples(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "need at least 300"):
            _process_samples(_samples(0.02, -1.6, 3_000_000.0, 299))

    def test_rejects_frequency_collapsing_calibration(self) -> None:
        data = {float(height): _samples(-1.3, 235.0, 3_056_000.0 - 20_000.0 * height) for height in (1, 2, 3)}
        bad = CoilCalibrationConfiguration(0.0, 0.0, 0.003721662845787226, -377.8946595557704)
        with self.assertRaisesRegex(RuntimeError, "same-temperature identity"):
            validate_coil_temperature_model(bad, data, CoilCalibrationReference(2_943_053.841590951, 25.0))

    def test_rejects_calibration_that_does_not_reduce_drift(self) -> None:
        data = {float(height): _samples(0.02, -1.6, 3_000_000.0 - 20_000.0 * height) for height in (1, 2, 3)}
        no_correction = CoilCalibrationConfiguration(1e-8, 1e-5, 2e-8, 1e-4)
        with patch(
            "cartographer.coil.calibration.CoilTemperatureCompensationModel.compensate",
            side_effect=lambda frequency, source, target: frequency,
        ), self.assertRaisesRegex(RuntimeError, "does not reduce measured frequency drift"):
            validate_coil_temperature_model(no_correction, data, CoilCalibrationReference(2_900_000.0, 25.0))

    @unittest.skipUnless(find_spec("scipy") is not None, "SciPy is optional")
    def test_matches_scipy_across_fit_paths(self) -> None:
        from scipy.optimize import curve_fit

        cases = (
            (0.02, -1.6, 300, 0.0),
            (0.02, -6.0, 500, 0.01),
            (0.02, 0.2, 500, 0.01),
            (0.02, -1.6, 1200, 0.01),
            (-1.3, 235.0, 500, 0.01),
        )
        for a_input, b_input, count, noise in cases:
            with self.subTest(a=a_input, b=b_input, count=count):
                samples = _samples(a_input, b_input, 3_000_000.0, count, noise=noise)
                if count > 1000:
                    samples = _downsample_by_temperature(samples, target_count=800)
                temperatures = [sample.temperature for sample in samples]
                frequencies = [sample.frequency for sample in samples]
                (a, b, c), _ = curve_fit(
                    line_fit,
                    temperatures,
                    frequencies,
                    bounds=([0, -np.inf, -np.inf], [np.inf, np.inf, np.inf]),
                    maxfev=100000,
                    ftol=1e-10,
                    xtol=1e-10,
                )
                vertex = -b / (2 * a)
                if vertex > 120:
                    (a, c), _ = curve_fit(
                        line120,
                        temperatures,
                        frequencies,
                        bounds=([0, -np.inf], [np.inf, np.inf]),
                        maxfev=100000,
                        ftol=1e-10,
                        xtol=1e-10,
                    )
                    expected = (a, -240 * a, line120(120, a, c))
                elif vertex < 0:
                    (a, c), _ = curve_fit(
                        line0,
                        temperatures,
                        frequencies,
                        bounds=([0, -np.inf], [np.inf, np.inf]),
                        maxfev=100000,
                        ftol=1e-10,
                        xtol=1e-10,
                    )
                    expected = (a, 0.0, line0(0, a, c))
                else:
                    expected = (a, b, line_fit(vertex, a, b, c))
                np.testing.assert_allclose(_process_samples(samples), expected, rtol=1e-3, atol=0.01)


if __name__ == "__main__":
    unittest.main()
