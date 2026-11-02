"""Bilimsel kabul testleri: çekim kimliği, nicelik/birim, karşılaştırma modları, meteoroloji kuralları, yorum motoru,
pik sınıflandırması ve sayısal raster okuyucu. Run: python -m unittest discover -s tests"""
import math
import struct
import unittest
import zlib
from datetime import datetime, timedelta, timezone

import numpy as np

import _env  # noqa: F401
from app import acquisition, catalog, comparison, interpret, meteo, peaks, quantities, raster

T0 = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def kgh(v, unc=None, kind=quantities.UNSPECIFIED, scope="plume"):
    return quantities.measurement(quantities.EMISSION_RATE, v, "kg/h", uncertainty=unc, uncertainty_kind=kind,
                                  scope=scope)


def obs(oid, t, value=1000.0, sensor="Planet Tanager-1", source="cmsrc:S1", method="cm:mfa:v3e", lat=37.0,
        lon=35.0, acq=None, provider="Carbon Mapper", **kw):
    o = {"id": oid, "observedAt": iso(t), "lat": lat, "lon": lon, "sensor": sensor, "sourceId": source,
         "methodVersion": method, "provider": provider, "acquisitionId": acq or f"ACQ:{oid}",
         "measurement": kgh(value) if value is not None else None, "alternateEstimates": [],
         "accessPaths": [{"provider": provider, "id": oid}], "tr": True}
    o.update(kw)
    return o


class AcquisitionTest(unittest.TestCase):
    def test_same_emit_scene_from_three_providers_has_one_key(self):
        k_cm = acquisition.from_cm("emi20220827t074030p05003-A", "emi", "2f39a2fb")
        k_nasa = acquisition.from_emit_scenes(["EMIT_L2B_CH4ENH_V002_20220827T074030_2223905_003"])
        k_mars = acquisition.from_tile("EMIT_L1B_RAD_001_20220827T074030_2223905_003")
        self.assertEqual(k_cm, "EMIT:20220827T074030:003")
        self.assertEqual(k_cm, k_nasa)
        self.assertEqual(k_cm, k_mars)

    def test_s5p_product_and_cams_id_share_orbit(self):
        a = acquisition.from_tile("S5P_OFFL_L2__CH4____20250605T101347_20250605T115517_39612_03_020800_20250607T072920")
        b = acquisition.from_tile("39612_32_20_2368_80_S5P")
        self.assertEqual(a, "S5P:orbit39612")
        self.assertEqual(a, b)

    def test_merge_keeps_estimates_and_counts_one_acquisition(self):
        cm = obs("emi20220827t074030p05003-A", T0, 2378, sensor="NASA EMIT (ISS)", acq="EMIT:20220827T074030:003",
                 instrument="emi")
        nasa = obs("emit:CH4_PlumeComplex-542", T0, None, sensor="NASA EMIT (ISS)", acq="EMIT:20220827T074030:003",
                   provider="NASA LP DAAC", lat=37.0003, instrument="emi")
        mars = obs("mars:ce36", T0, 2400, sensor="NASA EMIT (ISS)", acq="EMIT:20220827T074030:003",
                   provider="UNEP IMEO MARS", lat=37.0005, source="mars:TUR_S_020", method="mars:UNEP IMEO MARS",
                   measurement=kgh(2400, 700, quantities.SIGMA1))
        items = catalog.compose([cm], [nasa], [mars])
        self.assertEqual(len(items), 1)
        merged = items[0]
        self.assertEqual({a["provider"] for a in merged["alternateEstimates"]}, {"NASA LP DAAC", "UNEP IMEO MARS"})
        self.assertEqual(merged["measurement"]["value"], 2378)
        self.assertTrue(all(a["matchConfidence"] == "strong" for a in merged["alternateEstimates"]))
        self.assertEqual(merged["matchConfidence"], "strong")
        # Seri hesabında aynı çekim tek örnektir
        series = peaks.build_series(items)
        self.assertTrue(all(len(v) == 1 for v in series.values()))

    def test_different_sensors_close_in_time_and_space_are_not_merged(self):
        a = obs("tan-1", T0, sensor="Planet Tanager-1", acq="TAN:x", instrument="tan")
        b = obs("mars:enmap", T0 + timedelta(minutes=2), sensor="EnMAP HSI", acq="ENMAP:y:003", lat=37.0001,
                provider="UNEP IMEO MARS", instrument="enmap")
        self.assertEqual(len(catalog.compose([a], [], [b])), 2)

    def test_emit_without_ids_matches_only_as_probable(self):
        cm = obs("emi-x", T0, sensor="NASA EMIT (ISS)", acq=None, instrument="emi")
        cm["acquisitionId"] = None
        nasa = obs("emit:y", T0 + timedelta(seconds=30), None, sensor="NASA EMIT (ISS)", provider="NASA LP DAAC",
                   lat=37.002, instrument="emi")
        nasa["acquisitionId"] = None
        items = catalog.compose([cm], [nasa])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["matchConfidence"], "probable")


class QuantityTest(unittest.TestCase):
    def test_tonnes_per_hour(self):
        self.assertEqual(quantities.to_canonical(quantities.EMISSION_RATE, 1, "t/h"), (1000.0, "kg/h"))
        m = quantities.measurement(quantities.EMISSION_RATE, 16, "t/h", uncertainty=6)
        self.assertEqual((m["value"], m["unit"], m["uncertainty"]["value"]), (16000.0, "kg/h", 6000.0))

    def test_cross_quantity_ratio_is_refused(self):
        ppb = quantities.measurement(quantities.XCH4, 1950, "ppb")
        res, why = quantities.difference(kgh(1000), ppb)
        self.assertIsNone(res)
        self.assertEqual(why, "quantity_mismatch")

    def test_source_mean_versus_plume_is_refused(self):
        _, why = quantities.difference(kgh(1000), kgh(800, scope="source_summary"))
        self.assertEqual(why, "scope_mismatch")

    def test_unknown_plus_minus_is_not_sigma(self):
        comb, why = quantities.combined_sigma(kgh(1000, 300), kgh(1500, 400), independent=True)
        self.assertIsNone(comb)
        self.assertEqual(why, "uncertainty_definition_unknown")
        comb, _ = quantities.combined_sigma(kgh(1000, 300, quantities.SIGMA1), kgh(1500, 400, quantities.SIGMA1), True)
        self.assertAlmostEqual(comb["sigma"], 500.0)
        self.assertAlmostEqual(comb["z"], 1.0)

    def test_missing_value_is_not_zero(self):
        self.assertIsNone(quantities.measurement(quantities.EMISSION_RATE, None, "kg/h")["value"])
        self.assertIsNone(quantities.to_canonical(quantities.EMISSION_RATE, float("nan"), "kg/h")[0])


class ComparisonTest(unittest.TestCase):
    def test_near_time_boundary_30_minutes(self):
        a = obs("a", T0, sensor="Planet Tanager-1")
        b = obs("b", T0 + timedelta(minutes=30), sensor="EnMAP HSI", source="x")
        c = obs("c", T0 + timedelta(minutes=31), sensor="EnMAP HSI", source="x")
        self.assertEqual(comparison.compare(a, b)["mode"], "near_time_independent")
        self.assertNotEqual(comparison.compare(a, c)["mode"], "near_time_independent")

    def test_same_acquisition_is_never_independent(self):
        a = obs("a", T0, acq="EMIT:1:001")
        b = obs("b", T0, acq="EMIT:1:001", provider="UNEP IMEO MARS")
        d = comparison.compare(a, b)
        self.assertFalse(d["independent"])
        self.assertEqual(d["rejectedNumeric"], "same_acquisition")
        self.assertIsNone(d["numeric"])

    def test_historical_change_same_series_gives_difference_without_agreement_claim(self):
        a = obs("a", T0, 1000)
        b = obs("b", T0 + timedelta(days=20), 1500)
        d = comparison.compare(a, b)
        self.assertEqual(d["mode"], "historical_change")
        self.assertEqual(d["numeric"]["difference"], 500)
        self.assertNotIn("agree", str(d).lower())
        self.assertIsNone(d["combined"])  # tanımı bilinmeyen ±

    def test_different_method_version_is_context_only(self):
        a = obs("a", T0, 1000, method="cm:mfa:v3")
        b = obs("b", T0 + timedelta(days=20), 1500, method="cm:mfa:v3e")
        self.assertEqual(comparison.compare(a, b)["mode"], "context_only")

    def test_ppb_vs_kgh_rejected(self):
        a = obs("a", T0)
        b = obs("b", T0, measurement=quantities.measurement(quantities.XCH4, 1900, "ppb"), sensor="S5P")
        d = comparison.compare(a, b)
        self.assertEqual(d["rejectedNumeric"], "quantity_mismatch")
        self.assertIsNone(d["numeric"])

    def test_shared_reanalysis_wind_is_not_independent_for_combined_sigma(self):
        w = {"source": "ERA5"}
        a = obs("a", T0, 1000, sensor="EnMAP HSI", providerWind=w, measurement=kgh(1000, 100, quantities.SIGMA1))
        b = obs("b", T0 + timedelta(minutes=10), 1400, sensor="PRISMA", providerWind=w, source="y",
                measurement=kgh(1400, 100, quantities.SIGMA1))
        d = comparison.compare(a, b)
        self.assertEqual(d["mode"], "near_time_independent")
        self.assertIsNone(d["combined"])


class MeteorologyRuleTest(unittest.TestCase):
    def test_from_270_transports_east(self):
        self.assertEqual(meteo.to_direction(270), 90)
        s, frm, to = meteo.wind_from_uv(5.0, 0.0)  # batıdan esen (u>0)
        self.assertAlmostEqual(frm, 270)
        self.assertAlmostEqual(to, 90)

    def test_circular_mean_350_10(self):
        self.assertAlmostEqual(meteo.mean_direction([350, 10]) % 360, 0, places=6)

    def test_uv_interpolation_across_north(self):
        times = ["2026-09-01T09:00", "2026-09-01T10:00"]
        r = meteo.interpolate_wind(times, [4.0, 4.0], [350.0, 10.0], T0 + timedelta(minutes=30))
        self.assertTrue(min(r["from"], 360 - r["from"]) < 1e-6)
        self.assertNotAlmostEqual(r["from"], 180)

    def test_sample_outside_60_minutes_is_not_used(self):
        idx, off = meteo.nearest_index(["2026-09-01T07:00"], T0 + timedelta(minutes=1))
        self.assertIsNone(idx)
        self.assertEqual(off, -121)
        self.assertIsNone(meteo.interpolate_wind(["2026-09-01T07:00", "2026-09-01T12:00"], [3, 3], [0, 0], T0))

    def test_calm_wind_has_no_direction(self):
        s, frm, to = meteo.wind_from_uv(0.2, 0.1)
        self.assertIsNone(frm)
        self.assertIsNone(to)

    def test_precip_prior_missing_hour_is_none_not_zero(self):
        times = [iso(T0 - timedelta(hours=k))[:16] for k in range(23, -1, -1)]
        vals = [0.1] * 24
        self.assertAlmostEqual(meteo.precip_prior(times, vals, T0, 24), 2.4)
        vals[5] = None
        self.assertIsNone(meteo.precip_prior(times, vals, T0, 24))
        self.assertIsNone(meteo.precip_prior(times, [0.1] * 24, T0, 72))


def weather_block(speed=1.5, frm=270.0, blh=200.0, offset=5):
    return {"status": "ready", "primary": {
        "provider": "Open-Meteo", "dataset": "ERA5", "accessPath": "ERA5 — Open-Meteo üzerinden", "model": "era5",
        "spatialResolution": "0,25°", "validAt": "2026-09-01T09:00Z", "timeOffsetMinutes": offset,
        "resolvedGridPoint": {"lat": 37.0, "lon": 35.0, "elevationM": 100},
        "wind10": {"speed": speed, "from": frm, "to": (frm + 180) % 360, "calm": speed < 0.5},
        "variables": {"boundary_layer_height": {"value": blh}}, "precipPrior": {"1h": 0.0, "24h": 12.0, "72h": None}}}


class InterpretationTest(unittest.TestCase):
    def rules(self, res):
        return {f["ruleId"]: f for f in res["findings"]}

    def test_missing_weather_and_png_only_give_no_numbers(self):
        o = obs("a", T0)
        res = interpret.interpret(o, {"status": "empty", "primary": None}, {"status": "not_available",
                                  "reason": "yalnızca PNG"}, None, [])
        r = self.rules(res)
        self.assertEqual(r["MET-01"]["status"], "insufficient_data")
        self.assertEqual(r["MORPH-01"]["status"], "insufficient_data")
        self.assertIsNone(res["confidenceScore"])
        self.assertFalse(res["llmUsed"])
        self.assertNotIn("IME", " ".join(f["claim"] for f in res["findings"] if f["status"] == "supported"))

    def test_no_emission_is_not_zero(self):
        o = obs("a", T0, None)
        r = self.rules(interpret.interpret(o, None, None, None, []))
        self.assertEqual(r["OBS-01"]["status"], "insufficient_data")
        self.assertIn("sıfır salım anlamına gelmez", r["OBS-01"]["claim"])

    def test_weak_wind_shallow_pbl_and_precip_not_causal(self):
        o = obs("a", T0)
        r = self.rules(interpret.interpret(o, weather_block(), None, None, []))
        self.assertEqual(r["MET-02"]["status"], "limited")
        self.assertEqual(r["MET-06"]["status"], "limited")
        self.assertIn("nedeni olarak yorumlanmaz", r["MET-07"]["claim"])
        self.assertIn("72h", r["MET-07"]["missingInputs"])

    def test_morphology_mismatch_is_review_flag(self):
        o = obs("a", T0)
        num = {"status": "ok", "sha256": "x", "maskNote": "m", "morphology": {"transportDeg": 270.0, "axisDeg": 90.0}}
        r = self.rules(interpret.interpret(o, weather_block(speed=4.0, frm=270.0), num, None, []))
        # Rüzgâr 270'ten -> taşınma 90; raster 270 yönünü gösteriyor: 180° fark
        self.assertEqual(r["MORPH-01"]["status"], "limited")
        self.assertIn("inceleme işareti", r["MORPH-01"]["claim"])
        self.assertTrue(any("kanıtlamaz" in x for x in r["MORPH-01"]["limitations"]))

    def test_provider_q_is_not_corrected_with_new_wind(self):
        o = obs("a", T0, providerWind={"speed": 3.0, "from": 10, "source": "provider"})
        r = self.rules(interpret.interpret(o, weather_block(speed=6.0), None, None, []))
        self.assertIn("düzeltilmez", " ".join(r["MET-08"]["limitations"]))
        self.assertEqual(r["OBS-01"]["facts"]["value"], 1000.0)


class PeakTest(unittest.TestCase):
    def series(self, n, days, value=1000.0, start=T0):
        return [obs(f"r{k:02d}", start + timedelta(days=k % days, hours=k), value + (k % 5) * 10) for k in range(n)]

    def test_candidate_with_enough_reference(self):
        ref = self.series(12, 6)
        hi = obs("hi", T0 + timedelta(days=20), 5000)
        cls, _ = peaks.classify(ref + [hi])
        self.assertEqual(cls["hi"]["state"], peaks.CANDIDATE)
        base = next(iter(cls["hi"]["groups"].values()))["baseline"]
        self.assertTrue(base["thresholdsAreOperationalDefaults"])

    def test_small_n_and_few_days_give_no_z(self):
        cls, _ = peaks.classify(self.series(9, 6) + [obs("hi", T0 + timedelta(days=20), 5000)])
        self.assertEqual(next(iter(cls["hi"]["groups"].values()))["baseline"]["status"], "insufficient_reference")
        cls, _ = peaks.classify(self.series(12, 4) + [obs("hi", T0 + timedelta(days=20), 5000)])
        self.assertEqual(next(iter(cls["hi"]["groups"].values()))["baseline"]["status"], "insufficient_reference")

    def test_zero_mad(self):
        ref = [obs(f"r{k}", T0 + timedelta(days=k), 1000.0) for k in range(12)]
        cls, _ = peaks.classify(ref + [obs("hi", T0 + timedelta(days=20), 9000)])
        b = next(iter(cls["hi"]["groups"].values()))["baseline"]
        self.assertEqual(b["status"], "zero_mad")
        self.assertIsNone(b["z"])

    def test_no_future_leakage(self):
        later = [obs(f"f{k}", T0 + timedelta(days=30 + k), 1000 + k) for k in range(15)]
        cls, _ = peaks.classify([obs("early", T0, 5000)] + later)
        self.assertEqual(next(iter(cls["early"]["groups"].values()))["baseline"]["n"], 0)

    def test_duplicates_from_same_acquisition_do_not_inflate_reference(self):
        ref = [obs(f"r{k}", T0 + timedelta(days=k % 6), 1000 + k, acq=f"A{k % 3}") for k in range(12)]
        series = peaks.build_series(ref)
        self.assertEqual(sum(len(v) for v in series.values()), 3)

    def test_z_boundary_inclusive(self):
        # medyan 100, MAD 10 -> ölçek 14.826; z = 3,5 sınırda aday
        ref = [obs(f"r{k}", T0 + timedelta(days=k), v) for k, v in enumerate([90, 90, 90, 100, 100, 100, 100, 110,
                                                                              110, 110, 100])]
        x = 100 + 3.5 * 1.4826 * 10
        b = peaks.robust_baseline(peaks.build_series(ref)[next(iter(peaks.build_series(ref)))],
                                  T0 + timedelta(days=30), x)
        self.assertAlmostEqual(b["z"], 3.5)
        self.assertEqual(b["status"], "candidate")

    def test_top_n_per_series_and_groups_do_not_mix(self):
        a = [obs(f"a{k}", T0 + timedelta(days=k), 100 * (k + 1)) for k in range(5)]
        b = [obs(f"b{k}", T0 + timedelta(days=k), 10 * (k + 1), sensor="EnMAP HSI") for k in range(5)]
        cls, groups = peaks.classify(a + b)
        self.assertEqual(len(groups), 2)
        self.assertEqual({k for k, v in cls.items() if v["state"] == peaks.REFERENCE},
                         {"a4", "a3", "a2", "b4", "b3", "b2"})

    def test_method_version_change_starts_new_series(self):
        a = [obs(f"a{k}", T0 + timedelta(days=k), 100.0, method="cm:mfa:v3") for k in range(4)]
        b = [obs(f"b{k}", T0 + timedelta(days=k + 10), 100.0, method="cm:mfa:v3e") for k in range(2)]
        _, groups = peaks.classify(a + b)
        self.assertEqual(len(groups), 2)

    def test_order_independent(self):
        items = self.series(14, 7) + [obs("hi", T0 + timedelta(days=20), 5000)]
        c1, _ = peaks.classify(items)
        c2, _ = peaks.classify(list(reversed(items)))
        self.assertEqual({k: v["state"] for k, v in c1.items()}, {k: v["state"] for k, v in c2.items()})

    def test_rejected_artifact_excluded_from_top_n(self):
        a = [obs(f"a{k}", T0 + timedelta(days=k), 100 * (k + 1)) for k in range(5)]
        cls, _ = peaks.classify(a, {"a4": peaks.REJECTED})
        self.assertEqual(cls["a4"]["state"], peaks.REJECTED)
        self.assertEqual(cls["a1"]["state"], peaks.REFERENCE)

    def test_unassociated_has_no_series(self):
        cls, groups = peaks.classify([obs("x", T0, 5000, source=None)])
        self.assertEqual(cls["x"]["state"], peaks.ORDINARY)
        self.assertFalse(groups)


def make_tiff(arr, x0, y0, px, epsg=32636, predictor=2):
    """Tek döşemeli, DEFLATE + yatay öngörücülü float32 GeoTIFF (GDAL'ın ürettiği biçim)."""
    h, w = arr.shape
    words = arr.astype("<f4").view("<u4")
    if predictor == 2:
        words = np.concatenate([words[:, :1], np.diff(words, axis=1)], axis=1).astype("<u4")
    tile = zlib.compress(words.tobytes())
    entries, extra = [], b""
    base = 8

    def add(tag, typ, vals):
        entries.append((tag, typ, vals))
    add(256, 3, [w]); add(257, 3, [h]); add(258, 3, [32]); add(259, 3, [8]); add(262, 3, [1])
    add(277, 3, [1]); add(317, 3, [predictor]); add(322, 3, [w]); add(323, 3, [h]); add(339, 3, [3])
    add(33550, 12, [px, px, 0.0]); add(33922, 12, [0, 0, 0, x0, y0, 0])
    add(34735, 3, [1, 1, 0, 2, 1024, 0, 1, 1, 3072, 0, 1, epsg])
    add(324, 4, [0]); add(325, 4, [len(tile)])
    entries.sort()
    n = len(entries)
    ifd_size = 2 + 12 * n + 4
    data_off = base + ifd_size
    blobs, out_entries = b"", []
    fmt = {3: "H", 4: "I", 12: "d"}
    for tag, typ, vals in entries:
        packed = struct.pack("<" + fmt[typ] * len(vals), *vals)
        if len(packed) <= 4:
            out_entries.append(struct.pack("<HHI", tag, typ, len(vals)) + packed.ljust(4, b"\0"))
        else:
            out_entries.append(struct.pack("<HHII", tag, typ, len(vals), data_off + len(blobs)))
            blobs += packed
    tile_off = data_off + len(blobs)
    out_entries = [e if struct.unpack("<H", e[:2])[0] != 324 else struct.pack("<HHII", 324, 4, 1, tile_off)
                   for e in out_entries]
    return b"II*\0" + struct.pack("<I", base) + struct.pack("<H", n) + b"".join(out_entries) + b"\0\0\0\0" + blobs + tile


class RasterTest(unittest.TestCase):
    def test_utm_round_trip(self):
        x, y = raster.utm_forward(37.05, 35.39, 36)
        lat, lon = raster.utm_inverse(x, y, 36)
        self.assertAlmostEqual(lat, 37.05, places=6)
        self.assertAlmostEqual(lon, 35.39, places=6)

    def test_reader_and_eastward_plume_morphology(self):
        x0, y0 = raster.utm_forward(37.0, 35.0, 36)
        arr = np.full((9, 21), np.nan)
        arr[3:6, 2:20] = np.linspace(50, 500, 18)[::-1]  # kaynak batıda, bulut doğuya uzanıyor
        buf = make_tiff(arr, x0, y0, 50.0)
        a, geo, meta = raster.read_geotiff(buf)
        self.assertTrue(np.allclose(np.nan_to_num(a), np.nan_to_num(arr)))
        self.assertEqual(geo["epsg"], 32636)
        self.assertIsNone(meta["unitInFile"])
        olat, olon = raster.utm_inverse(x0 + 2.5 * 50, y0 - 4.5 * 50, 36)
        m = raster.morphology(a, geo, origin=(olat, olon))
        self.assertEqual(m["pixels"], 54)
        self.assertAlmostEqual(m["maskAreaM2"], 54 * 2500)
        self.assertLess(raster.angle_diff(m["transportDeg"], 90), 5)
        self.assertLess(raster.axial_diff(m["axisDeg"], 90), 5)

    def test_rgb_is_refused(self):
        buf = make_tiff(np.zeros((2, 2)), 0, 0, 1)
        buf = buf.replace(struct.pack("<HHI", 277, 3, 1) + struct.pack("<H", 1) + b"\0\0",
                          struct.pack("<HHI", 277, 3, 1) + struct.pack("<H", 4) + b"\0\0")
        with self.assertRaises(raster.RasterError):
            raster.read_geotiff(buf)

    def test_axis_ambiguity(self):
        self.assertAlmostEqual(raster.axial_diff(10, 190), 0)
        self.assertAlmostEqual(raster.angle_diff(350, 10), 20)
        self.assertTrue(math.isclose(raster.axial_diff(0, 90), 90))


if __name__ == "__main__":
    unittest.main()
