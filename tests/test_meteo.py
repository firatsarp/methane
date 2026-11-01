import unittest
from datetime import datetime, timedelta, timezone

import _env  # noqa: F401  (DATA_DIR import öncesi ayarlanmalı)
from app import meteo


def hourly(year, month, value_fn):
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    n = meteo.hours_in_month(year, month)
    times = [(start + timedelta(hours=k)).strftime("%Y-%m-%dT%H:%M") for k in range(n)]
    return times, [value_fn(k) for k in range(n)]


class WindTest(unittest.TestCase):
    def test_wind_from_west_is_transported_east(self):
        # 270°'den (batıdan) esen rüzgârın u bileşeni pozitiftir (doğuya doğru)
        speed, frm, to = meteo.wind_from_uv(5.0, 0.0)
        self.assertAlmostEqual(speed, 5.0)
        self.assertAlmostEqual(frm, 270.0)
        self.assertAlmostEqual(to, 90.0)

    def test_north_wind(self):
        _, frm, to = meteo.wind_from_uv(0.0, -4.0)
        self.assertAlmostEqual(frm, 0.0)
        self.assertAlmostEqual(to, 180.0)

    def test_calm_wind_has_no_direction(self):
        speed, frm, to = meteo.wind_from_uv(0.1, -0.2)
        self.assertLess(speed, meteo.CALM_MS)
        self.assertIsNone(frm)
        self.assertIsNone(to)

    def test_uv_roundtrip(self):
        u, v = meteo.uv_from_wind(6.0, 225.0)
        speed, frm, _ = meteo.wind_from_uv(u, v)
        self.assertAlmostEqual(speed, 6.0)
        self.assertAlmostEqual(frm, 225.0)

    def test_circular_mean_350_10_is_0_not_180(self):
        m = meteo.mean_direction([350.0, 10.0])
        self.assertTrue(m < 1e-6 or abs(m - 360) < 1e-6, m)

    def test_opposite_directions_have_no_mean(self):
        self.assertIsNone(meteo.mean_direction([90.0, 270.0]))

    def test_missing_values_are_ignored_not_zero(self):
        self.assertIsNone(meteo.wind_from_uv(None, 3.0)[0])
        self.assertAlmostEqual(meteo.mean_direction([80.0, None, 100.0]), 90.0)


class UnitTest(unittest.TestCase):
    def test_conversions_follow_metadata_units(self):
        self.assertAlmostEqual(meteo.convert(273.15, "K")[0], 0.0)
        self.assertEqual(meteo.convert(273.15, "K")[1], "°C")
        self.assertEqual(meteo.convert(101325.0, "Pa"), (1013.25, "hPa"))
        self.assertEqual(meteo.convert(0.25, "(0 - 1)"), (25.0, "%"))
        self.assertEqual(meteo.precip_m_to_mm(0.0012), 1.2)
        # Tanınmayan birim tahmin edilmez
        self.assertEqual(meteo.convert(5.0, "furlong"), (5.0, "furlong"))
        self.assertEqual(meteo.convert(float("nan"), "K"), (None, "K"))

    def test_humidity_from_dewpoint(self):
        self.assertAlmostEqual(meteo.humidity_from_dewpoint(20.0, 20.0), 100.0, places=3)
        self.assertLess(meteo.humidity_from_dewpoint(25.0, 5.0), 30.0)


class TimeMatchTest(unittest.TestCase):
    def test_nearest_hour_and_offset(self):
        times = ["2026-09-26T09:00", "2026-09-26T10:00"]
        target = datetime(2026, 9, 26, 9, 53, 39, tzinfo=timezone.utc)
        idx, off = meteo.nearest_index(times, target)
        self.assertEqual(idx, 1)
        self.assertEqual(off, 6)

    def test_outside_tolerance_is_rejected(self):
        idx, off = meteo.nearest_index(["2026-09-26T06:00"], datetime(2026, 9, 26, 9, 0, tzinfo=timezone.utc))
        self.assertIsNone(idx)
        self.assertEqual(off, -180)

    def test_utc_istanbul_offset(self):
        # Gösterim Europe/Istanbul (UTC+3); eşleştirme UTC'de yapılır
        t = meteo.parse_utc("2026-09-26T09:53:39.050Z")
        self.assertEqual(t.utcoffset(), timedelta(0))
        self.assertEqual((t + timedelta(hours=3)).hour, 12)


class ClimateTest(unittest.TestCase):
    def test_monthly_aggregation_and_leap_year(self):
        t, temp = hourly(2024, 2, lambda k: 10.0)
        _, pr = hourly(2024, 2, lambda k: 0.1)
        _, wind = hourly(2024, 2, lambda k: 2.0 if k % 2 else 4.0)
        m = meteo.monthly_aggregates(t, temp, pr, wind)[(2024, 2)]
        self.assertEqual(len(t), 29 * 24)
        self.assertAlmostEqual(m["temp"], 10.0)
        self.assertAlmostEqual(m["precip"], 0.1 * 29 * 24)
        # Skaler hız ortalaması
        self.assertAlmostEqual(m["wind"], 3.0)

    def test_missing_hours_invalidate_month_not_zero_fill(self):
        t, temp = hourly(2023, 3, lambda k: 5.0)
        temp[10] = None
        _, pr = hourly(2023, 3, lambda k: 0.0)
        _, wind = hourly(2023, 3, lambda k: 1.0)
        m = meteo.monthly_aggregates(t, temp, pr, wind)[(2023, 3)]
        self.assertIsNone(m["temp"])
        self.assertEqual(m["precip"], 0.0)

    def test_reference_counts_valid_years_and_zero_reference_has_no_percent(self):
        monthly = {}
        for y in range(1991, 2021):
            for mo in range(1, 13):
                monthly[(y, mo)] = {"temp": 10.0 + mo, "precip": 0.0 if mo == 7 else 30.0, "wind": 3.0}
        monthly[(1995, 1)]["temp"] = None
        ref = meteo.climate_reference(monthly)
        self.assertEqual(ref[0]["tempYears"], 29)
        self.assertEqual(ref[1]["tempYears"], 30)
        monthly[(2026, 7)] = {"temp": 20.0, "precip": 5.0, "wind": 3.5}
        cmp = meteo.compare_month(monthly, ref, 2026, 7)
        self.assertAlmostEqual(cmp["diff"]["temp"], 3.0)
        self.assertAlmostEqual(cmp["diff"]["precip"], 5.0)
        self.assertIsNone(cmp["precipPercent"])
        monthly[(2026, 8)] = {"temp": 20.0, "precip": 45.0, "wind": 3.0}
        self.assertAlmostEqual(meteo.compare_month(monthly, ref, 2026, 8)["precipPercent"], 50.0)

    def test_incomplete_month_is_not_compared(self):
        monthly = {(2026, 11): {"temp": None, "precip": 3.0, "wind": 2.0}}
        ref = [{"month": m, "temp": 1.0, "precip": 1.0, "wind": 1.0} for m in range(1, 13)]
        self.assertIsNone(meteo.compare_month(monthly, ref, 2026, 11))

    def test_latest_complete_month_skips_current_month(self):
        now = datetime(2026, 11, 1, tzinfo=timezone.utc)
        ok = {"temp": 1.0, "precip": 1.0, "wind": 1.0}
        monthly = {(2026, 9): ok, (2026, 10): {"temp": None, "precip": 1.0, "wind": 1.0}, (2026, 11): ok}
        self.assertEqual(meteo.latest_complete_month(monthly, now), (2026, 9))


if __name__ == "__main__":
    unittest.main()
