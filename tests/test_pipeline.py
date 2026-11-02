"""Günlük iş hattı: tekrar çalıştırma, eksik kaynak turu ve 90 gün penceresinin geri dolmaması.
Ağ çağrıları taklit edilir. Run: python -m unittest discover -s tests"""
import json
import unittest
from datetime import timedelta
from unittest import mock

import _env  # noqa: F401
from app import (archive, cams, config, db, emit, ingest, mars, numeric, pipeline, quantities, retention, store,
                 weather)

NOW = store.now().replace(microsecond=0)


def mars_obs(k, days_ago, value, source="TUR_S_001"):
    t = NOW - timedelta(days=days_ago)
    return {"id": f"mars:{k}", "providerId": k, "provider": "UNEP IMEO MARS", "sensor": "EnMAP HSI",
            "instrument": "enmap", "observedAt": store.iso(t), "lat": 40.0, "lon": 29.0, "tr": True,
            "locationLabel": "x", "sourceId": f"mars:{source}", "methodVersion": "mars:UNEP IMEO MARS",
            "acquisitionId": f"ENMAP:{k}", "measurement": quantities.measurement(
                quantities.EMISSION_RATE, value, "kg/h", uncertainty=100, uncertainty_kind=quantities.SIGMA1),
            "emission": {"value": value, "unit": "kg/h", "uncertainty": None}}


def run(mars_items):
    with mock.patch.object(ingest, "ingest_plumes", return_value=[]), \
            mock.patch.object(emit, "ingest", return_value=[]), \
            mock.patch.object(cams, "ingest", return_value=[]), \
            mock.patch.object(mars, "ingest", side_effect=lambda protected, now=None: _store_mars(mars_items, protected, now)), \
            mock.patch.object(weather, "observation_weather", return_value={"status": "unavailable", "primary": None}), \
            mock.patch.object(numeric, "analyse", return_value={"status": "not_available", "reason": "test"}), \
            mock.patch.object(archive, "fetch_assets", return_value=[]):
        return pipeline.run(NOW)


def _store_mars(items, protected, now):
    """mars.ingest'in saklama davranışı: yalnızca pencere içi + korunan kayıtlar tabloya yazılır."""
    if items is None:
        return None
    keep = [o for o in items if retention.keep_on_ingest(o, protected, now)]
    with db.tx() as con:
        con.execute("DELETE FROM mars_plumes")
        con.executemany("INSERT INTO mars_plumes(id, observed, lat, lon, tr, obs, fetched_at) VALUES(?,?,?,?,?,?,?)",
                        [(o["id"], o["observedAt"], o["lat"], o["lon"], 1, json.dumps(o), store.iso(NOW)) for o in keep])
    return items


def table_ids():
    with db.read() as con:
        return {r[0] for r in con.execute("SELECT id FROM mars_plumes")}


def active_events():
    with db.read() as con:
        return {r[0] for r in con.execute("SELECT id FROM peak_events WHERE active = 1")}


class PipelineTest(unittest.TestCase):
    def setUp(self):
        if config.DB_PATH.exists():
            config.DB_PATH.unlink()
        db.init()

    def test_rerun_and_unchanged_source_do_not_lose_archive(self):
        items = [mars_obs("old1", 400, 9000), mars_obs("old2", 380, 8000), mars_obs("old3", 300, 100),
                 mars_obs("old4", 200, 7000), mars_obs("new1", 5, 50)]
        r1 = run(items)
        self.assertTrue(r1["backfill"])
        first = active_events()
        self.assertEqual(first, {"mars:old1", "mars:old2", "mars:old4"})
        # Aynı turda arşive giren eski kayıtlar tabloya da yazıldı; normal eski kayıt (old3) yazılmadı
        self.assertEqual(table_ids(), {"mars:old1", "mars:old2", "mars:old4", "mars:new1"})
        r2 = run(None)  # MARS dosyası değişmedi (304)
        self.assertEqual(r2["completeProviders"], ["CAMS (Copernicus)", "Carbon Mapper"])
        self.assertEqual(r2["deactivated"], 0)
        self.assertEqual(active_events(), first)
        r3 = run(items)  # Tekrar tam liste: olay/dosya çoğalmaz
        self.assertEqual(active_events(), first)
        self.assertEqual(r3["archive"].get("created", 0), 0)
        self.assertNotIn("mars:old3", table_ids())  # eski normal kayıt günlük alımla geri dolmadı

    def test_new_higher_record_displaces_reference_only_with_complete_source(self):
        items = [mars_obs("a", 400, 300), mars_obs("b", 390, 200), mars_obs("c", 380, 100)]
        run(items)
        self.assertEqual(active_events(), {"mars:a", "mars:b", "mars:c"})
        run(items + [mars_obs("d", 370, 1000)])
        self.assertEqual(active_events(), {"mars:a", "mars:b", "mars:d"})
        with db.read() as con:
            # Top-N dışına çıkan eski referans pencere dışında olduğu için aynı turda temizlendi
            self.assertIsNone(con.execute("SELECT 1 FROM peak_events WHERE id = 'mars:c'").fetchone())


if __name__ == "__main__":
    unittest.main()
