import math
import unittest
from pathlib import Path

from t01m_host.math_oracle import (
    amplitude_percent,
    canonical_svd_first,
    frozen_period,
)
from t01m_host.measure import (
    measure_character_contrast,
    validate_frame_timestamps,
    validate_inside_hero_attribution,
    validate_silhouette_measurements,
)
from t01m_host.png import PngImage
from t01m_host.numeric_reference import srgb_lookup, validate_numeric_reference
from t01m_host.util import OracleError


class MathOracleTests(unittest.TestCase):
    def test_period_fixture_resolves_4_8_seconds(self):
        values = [
            math.sin(2.0 * math.pi * index / 24.0)
            + 0.1 * math.sin(4.0 * math.pi * index / 24.0)
            for index in range(60)
        ]
        result = frozen_period(values)
        self.assertAlmostEqual(result.period_seconds, 4.8, delta=0.08)
        self.assertGreaterEqual(result.peak_correlation, 0.60)

    def test_amplitude_formula_uses_frozen_normalizers(self):
        values = [(100.0, 200.0, 50.0, 70.0)] * 59 + [
            (101.0, 198.0, 51.0, 69.0)
        ]
        self.assertAlmostEqual(amplitude_percent(values), 1.0)

    def test_timestamp_tolerance(self):
        validate_frame_timestamps(
            [1000.0 + index * 200.0 + (5.0 if index % 2 else 0.0) for index in range(60)],
            count=60,
            interval_ms=200.0,
        )

    def test_black_glyph_on_white_has_measurable_contrast(self):
        rows = []
        for y in range(24):
            row = []
            for x in range(24):
                if 8 <= x < 16 and 7 <= y < 17:
                    row.append((0, 0, 0))
                else:
                    row.append((255, 255, 255))
            rows.append(tuple(row))
        image = PngImage(24, 24, tuple(rows))
        report = measure_character_contrast(image, (7, 6, 17, 18), [(7, 6, 17, 18)])
        self.assertTrue(report["pass"])
        self.assertAlmostEqual(report["minimum_contrast_ratio"], 21.0, places=5)
        self.assertEqual(report["core_count"], 8)
        self.assertEqual(len(report["selected_actual_pixels"]), 8)
        self.assertEqual(len(report["background_coefficients_raw_bits"]), 3)
        self.assertEqual(
            len(report["selected_actual_pixels"][0]["contrast_ratio_raw_bits"]),
            16,
        )

    def test_jbr_numeric_reference_lookup_is_raw_bit_bound(self):
        values, raw_bits = srgb_lookup()
        self.assertEqual(len(values), 256)
        self.assertEqual(raw_bits[0], "0000000000000000")
        self.assertEqual(raw_bits[255], "3ff0000000000000")
        self.assertEqual(validate_numeric_reference()["runtime"].split()[0], "JBR")
        source = (
            Path(__file__).resolve().parent.parent
            / "numeric-reference/NumericReference.java"
        ).read_text(encoding="utf-8")
        self.assertNotIn("java.lang.Math", source)
        self.assertIn("iteration < 10", source)
        self.assertIn("StrictMath.pow(b, 2.4d)", source)

    def test_canonical_svd_sign_uses_smallest_max_loading_index(self):
        matrix = [
            [-2.0, 0.0],
            [-1.0, 0.0],
            [1.0, 0.0],
            [2.0, 0.0],
        ]
        result = canonical_svd_first(matrix)
        self.assertEqual(result.canonical_loading_index, 0)
        self.assertGreater(result.first_right_vector[0], 0.0)
        self.assertGreater(
            sum(
                score * row[0]
                for score, row in zip(result.score, matrix)
            ),
            0.0,
        )

    def test_silhouette_period_and_attached_glow_are_fail_closed(self):
        signal = [
            math.sin(2.0 * math.pi * index / 24.0) for index in range(60)
        ]
        values = [
            (
                100.0 + 0.3 * value,
                200.0 + 0.4 * value,
                50.0 + 0.1 * value,
                70.0 + 0.1 * value,
            )
            for value in signal
        ]
        report = validate_silhouette_measurements(values, signal)
        self.assertTrue(report["pass"])
        self.assertLessEqual(report["amplitude_percent"], 1.5)
        with self.assertRaisesRegex(OracleError, "detached"):
            validate_inside_hero_attribution(
                [], {(100, 100)}, [{(0, 0)}], []
            )


if __name__ == "__main__":
    unittest.main()
