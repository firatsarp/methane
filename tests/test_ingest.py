"""Carbon Mapper alma akışı: geçici veritabanı, ağ çağrısı yok. Run: python -m unittest discover -s tests"""
import copy
import io
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="metan-test-")

from app import alerts, config, db, ingest, sources  # noqa: E402  (DATA_DIR import öncesi ayarlanmalı)
from test_observations import CM  # noqa: E402

SILIVRI = [28.1488, 41.2135]


def plume(pid, coords=None, **changes):
    p = copy.deepcopy(CM)
    p.update(plume_id=pid, **changes)
    if coords:
        p["geometry_json"] = {"type": "Point", "coordinates": coords}
    return p


class IngestTest(unittest.TestCase):
    def setUp(self):
        if config.DB_PATH.exists():
            config.DB_PATH.unlink()
        db.init()

    def run_ingest(self, items, token_state="unset"):
        with mock.patch.object(ingest, "_fetch_carbonmapper", return_value=(items, token_state)):
            ingest.ingest_plumes()

    def test_first_fill_is_baseline_and_later_record_alerts(self):
        self.run_ingest([plume("a", SILIVRI)])
        self.assertEqual([a for a in alerts.alerts()["items"] if a["kind"] == "new_plume"], [])

        self.run_ingest([plume("a", SILIVRI), plume("b", [28.15, 41.22], scene_timestamp="2026-10-01T09:00:00Z")])
        new = [a for a in alerts.alerts()["items"] if a["kind"] == "new_plume"]
        self.assertEqual([a["plumeId"] for a in new], ["b"])
        # Gözlem ile yayımlama arasındaki fark uyarının gerekçesi olarak verilir
        self.assertEqual(new[0]["observedAt"], "2026-10-01T09:00:00Z")
        self.assertIsNotNone(new[0]["publicationLagDays"])
        self.assertIsNotNone(new[0]["ingestedAt"])

    def test_duplicates_invalid_and_status(self):
        self.run_ingest([plume("a", SILIVRI), plume("a", SILIVRI), plume("dup-scene", SILIVRI),
                         plume(None, SILIVRI)])
        st = sources.get("carbonmapper")
        self.assertEqual((st["records"], st["duplicates"], st["invalid"]), (1, 2, 1))
        self.assertEqual(st["state"], "ok")
        self.assertEqual(st["last_observation"], "2026-09-27T09:13:55.440000Z")
        self.assertEqual(st["last_published"], "2026-10-27T09:19:14.956000Z")

    def test_api_feature_carries_normalized_observation(self):
        from app.main import plumes
        self.run_ingest([plume("a", SILIVRI, emission_auto="N/A")])
        f = plumes(scope="tr")["features"][0]
        self.assertIsNone(f["properties"]["emission"])
        self.assertIsNone(f["properties"]["observation"]["emission"])
        self.assertEqual(f["properties"]["observation"]["scope"], "plume")

    def test_outside_turkey_not_alerted(self):
        self.run_ingest([plume("a", SILIVRI)])
        self.run_ingest([plume("a", SILIVRI), plume("far", [44.5, 35.6])])
        self.assertNotIn("far", [a.get("plumeId") for a in alerts.alerts()["items"]])

    def test_fetch_failure_is_reported_and_keeps_old_data(self):
        self.run_ingest([plume("a", SILIVRI)])
        with mock.patch.object(ingest, "_fetch_carbonmapper", side_effect=OSError("timeout")):
            ingest.ingest_plumes()
        st = sources.get("carbonmapper")
        self.assertEqual(st["state"], "error")
        self.assertIn("timeout", st["error"])
        self.assertIsNotNone(st["last_ok"])
        self.assertIn("source_error", [a["kind"] for a in alerts.alerts()["items"]])
        from app.main import plumes
        self.assertEqual(len(plumes(scope="tr")["features"]), 1)

    def test_rejected_token_falls_back_to_public_catalog(self):
        calls = []

        def fake_pages(token):
            calls.append(token)
            if token:
                raise urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO())
            return [plume("a", SILIVRI)]

        with mock.patch.object(ingest, "CARBON_MAPPER_API_TOKEN", "secret-value"), \
                mock.patch.object(ingest, "_fetch_carbonmapper_pages", side_effect=fake_pages):
            ingest.ingest_plumes()
        self.assertEqual(calls, ["secret-value", None])
        st = sources.get("carbonmapper")
        self.assertEqual(st["token"], "rejected")
        self.assertNotIn("secret-value", repr(st))
        self.assertIn("token_rejected", [a["kind"] for a in alerts.alerts()["items"]])

    def test_pending_credentials_report_presence_only(self):
        with mock.patch.dict(os.environ, {"CDSE_CLIENT_ID": "id", "CDSE_CLIENT_SECRET": "s3cr3t"}):
            items = {s["id"]: s for s in alerts.source_status()}
        self.assertTrue(items["cdse"]["configured"])
        self.assertEqual(items["cdse"]["state"], "not_connected")
        self.assertNotIn("s3cr3t", repr(items))


if __name__ == "__main__":
    unittest.main()
