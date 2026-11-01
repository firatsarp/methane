"""Kanonik gözlem kataloğu, filtreler, EMIT birleştirme ve kaynak özetleri. Ağ çağrısı yok."""
import json
import unittest
from datetime import datetime, timezone

import _env  # noqa: F401
from app import catalog, config, db, emit, observations

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


def cm(pid, t, lat, lon, instrument="tan", emission=500.0, sector="6A", published="2026-10-01T00:00:00Z"):
    o = observations.normalize_cm_plume({
        "plume_id": pid, "scene_timestamp": t, "instrument": instrument, "sector": sector,
        "geometry_json": {"type": "Point", "coordinates": [lon, lat]}, "emission_auto": emission,
        "emission_uncertainty_auto": 100.0 if emission is not None else None, "published_at": published,
        "platform": "Planet Tanager 1" if instrument == "tan" else "ISS",
    })
    return o


# Gerçek EMIT PLMMETA yanıtından (01.11.2026, CH4_PlumeComplex-3551) alan adları ve değerleri
PLM_FEATURE = {
    "type": "Feature",
    "geometry": {"type": "Polygon", "coordinates": [[[40.78, 36.19], [40.79, 36.19], [40.79, 36.20], [40.78, 36.19]]]},
    "properties": {
        "Plume ID": "CH4_PlumeComplex-3551", "UTC Time Observed": "2024-09-30T09:13:14Z",
        "Max Plume Concentration (ppm m)": 3026.0, "Latitude of max concentration": 36.11751,
        "Longitude of max concentration": 40.81004, "Wind Speed (m/s)": 3.2642, "Wind Speed Source": "ERA5",
        "Emissions Rate Estimate (kg/hr)": 2542.6059, "Emissions Rate Estimate Uncertainty (kg/hr)": 164.8604,
        "DAAC Scene Names": ["EMIT_L2B_CH4ENH_V002_20240930T091314_2427406_024"],
    },
}
UMM = {"GranuleUR": "EMIT_L2B_CH4PLM_002_20240930T091314_003551",
       "AdditionalAttributes": [{"Name": "EMIT_GHG_SOFTWARE_BUILD_VERSION", "Values": ["v0.1.2"]}]}


class EmitTest(unittest.TestCase):
    def test_normalize_keeps_provider_values(self):
        o = emit.normalize(PLM_FEATURE, UMM, "C1", "G1", "002", {"png": "https://x/y.png"}, "2026-11-01T00:00:00Z")
        self.assertEqual(o["id"], "emit:CH4_PlumeComplex-3551")
        self.assertEqual(o["observedAt"], "2024-09-30T09:13:14Z")
        self.assertEqual(o["emission"]["value"], 2542.6059)
        self.assertEqual(o["emission"]["uncertainty"]["value"], 164.8604)
        self.assertEqual(o["maxEnhancement"]["unit"], "ppm·m")
        self.assertIsNone(o["publishedAt"])
        self.assertIn("v002", o["processingVersion"])

    def test_missing_emission_is_not_zero(self):
        f = json.loads(json.dumps(PLM_FEATURE))
        f["properties"]["Emissions Rate Estimate (kg/hr)"] = "NaN"
        o = emit.normalize(f, UMM, "C1", "G1", "002", {}, None)
        self.assertIsNone(o["emission"])

    def test_same_emit_scene_from_two_paths_is_one_observation(self):
        c = catalog._cm_item(("emi20240930t091314p06024-C", None, None, json.dumps(
            cm("emi20240930t091314p06024-C", "2024-09-30T09:13:14Z", 36.11739, 40.80998, "emi", 2307.5)), 1), {})
        other = catalog._cm_item(("tan-x", None, None, json.dumps(
            cm("tan-x", "2024-09-30T09:13:14Z", 36.11739, 40.80998, "tan")), 1), {})
        e_obs = emit.normalize(PLM_FEATURE, UMM, "C1", "G1", "002", {}, None)
        e = catalog._emit_item(("x", json.dumps(PLM_FEATURE["geometry"]), json.dumps(e_obs), 1))
        merged = catalog.merge_emit([c, other], [e])
        self.assertEqual(len(merged), 2)
        m = next(x for x in merged if x["id"] == c["id"])
        self.assertEqual([a["provider"] for a in m["accessPaths"]], ["Carbon Mapper", "NASA LP DAAC"])
        # Her sağlayıcının tahmini ayrı kalır; birleştirmede ortalama alınmaz
        self.assertEqual(m["emission"]["value"], 2307.5)
        self.assertEqual(m["alternateEstimates"][0]["emission"]["value"], 2542.6059)
        self.assertEqual(m["footprint"]["type"], "Polygon")

    def test_different_time_is_not_merged(self):
        c = catalog._cm_item(("emi-other", None, None, json.dumps(
            cm("emi-other", "2024-10-30T09:13:14Z", 36.11739, 40.80998, "emi")), 1), {})
        e = catalog._emit_item(("x", None, json.dumps(emit.normalize(PLM_FEATURE, UMM, "C", "G", "002", {}, None)), 1))
        self.assertEqual(len(catalog.merge_emit([c], [e])), 2)


class FilterTest(unittest.TestCase):
    def setUp(self):
        self.items = [
            {**cm("new", "2026-10-20T09:00:00Z", 41.2, 28.1), "sectorCode": "6A"},
            {**cm("old", "2024-03-01T09:00:00Z", 41.2, 28.1, emission=None), "sectorCode": "6A"},
            {**cm("gas", "2026-09-01T09:00:00Z", 37.0, 40.0, sector="1B2", emission=900.0), "sectorCode": "1B2"},
            {**cm("notime", "", 37.0, 40.0), "observedAt": None, "sectorCode": "6A"},
        ]

    def ids(self, **kw):
        return [o["id"] for o in catalog.apply_filters(self.items, now=NOW, **kw)]

    def test_last_90_days_excludes_old_and_unverified_time(self):
        self.assertEqual(self.ids(days=90), ["new", "gas"])

    def test_sector_and_emission_filters(self):
        self.assertEqual(self.ids(sector="1B2"), ["gas"])
        self.assertNotIn("old", self.ids(emission_only=True))

    def test_emission_sort_puts_missing_last_not_as_zero(self):
        self.assertEqual(self.ids(sort="emission")[-1] in ("old", "notime"), True)
        self.assertEqual(self.ids(sort="emission")[0], "gas")

    def test_published_sort_uses_published_field(self):
        self.items[0]["publishedAt"] = "2026-01-01T00:00:00Z"
        self.items[2]["publishedAt"] = "2026-10-30T00:00:00Z"
        self.assertEqual(self.ids(sort="published")[0], "gas")

    def test_facets_from_records(self):
        f = catalog.facets(self.items)
        self.assertEqual(f["sectors"], ["1B2", "6A"])
        self.assertEqual(f["sensors"], ["Planet Tanager-1"])


class SourceSummaryTest(unittest.TestCase):
    def setUp(self):
        if config.DB_PATH.exists():
            config.DB_PATH.unlink()
        db.init()

    def test_source_summary_is_separate_and_links_by_provider_ids(self):
        src = observations.normalize_cm_source({
            "geometry": {"type": "Point", "coordinates": [28.1475, 41.2156]},
            "properties": {"source_name": "CH4_6A_1000m_28.14750_41.21560?bbox=1", "sector": "6A", "gas": "CH4",
                           "plume_ids": ["p1", "p2"], "plume_count": 2, "avg_emission_auto": 900.0,
                           "emission_auto": 300.0, "persistence": 1 / 3, "detection_date_count": 2,
                           "observation_date_count": 6},
        })
        self.assertEqual(src["id"], "cmsrc:CH4_6A_1000m_28.14750_41.21560")
        self.assertEqual(src["scope"], "source_summary")
        self.assertEqual(src["clusterRadiusM"], 1000)
        p1 = cm("p1", "2026-04-26T09:00:00Z", 41.215, 28.147, emission=4000.0)
        with db.tx() as con:
            con.execute("INSERT INTO cm_sources(id, lat, lon, tr, obs) VALUES(?,?,?,?,?)",
                        (src["id"], src["lat"], src["lon"], 1, json.dumps(src)))
            for o in (p1, cm("p3", "2026-04-27T09:00:00Z", 41.0, 28.0)):
                con.execute("INSERT INTO cm_plumes(id, observed, lat, lon, tr, obs) VALUES(?,?,?,?,?,?)",
                            (o["id"], o["observedAt"], o["lat"], o["lon"], 1, json.dumps(o)))
        o, s, related = catalog.get("p1")
        self.assertEqual(o["sourceId"], src["id"])
        # Kaynak ortalaması tek buluta atanmaz
        self.assertEqual(o["emission"]["value"], 4000.0)
        self.assertEqual(s["detectedMean"]["value"], 900.0)
        self.assertEqual(related, [])
        o3, s3, _ = catalog.get("p3")
        self.assertIsNone(o3["sourceId"])
        self.assertIsNone(s3)


class RetiredPageTest(unittest.TestCase):
    def test_old_location_page_redirects_permanently(self):
        from app import main
        r = main.retired_seymen()
        self.assertEqual(r.status_code, 308)
        self.assertEqual(r.headers["location"], "/yuksek-degerleri-yorumlamak.html")
        # Rota statik dosya bağlamasından önce kayıtlı olmalı; yoksa eski dosya sunulur
        paths = [getattr(x, "path", None) for x in main.app.routes]
        self.assertLess(paths.index("/seymen.html"), paths.index(""))


if __name__ == "__main__":
    unittest.main()
