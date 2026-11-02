"""90 gün saklama, kalıcı yüksek olay arşivi ve iş kuyruğu kimlik/geri çekilme davranışı.
Run: python -m unittest discover -s tests"""
import json
import os
import unittest
from datetime import timedelta
from unittest import mock

import _env  # noqa: F401
from app import archive, config, db, imagery, net, peaks, retention, sources, store

NOW = store.now().replace(microsecond=0)


def cm_row(oid, observed, tr=1):
    o = {"id": oid, "observedAt": store.iso(observed), "lat": 37.0, "lon": 35.0, "provider": "Carbon Mapper",
         "sensor": "Planet Tanager-1", "accessPaths": [{"provider": "Carbon Mapper", "id": oid}]}
    return (oid, store.iso(observed), 37.0, 35.0, tr, None, None, None, None, None, None, None, json.dumps(o))


def insert_cm(*rows):
    with db.tx() as con:
        con.executemany("INSERT INTO cm_plumes(id, observed, lat, lon, tr, emission, uncertainty, sector, platform, "
                        "wind_speed, png, bounds, obs) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)


def ids(table="cm_plumes"):
    with db.read() as con:
        return {r[0] for r in con.execute(f"SELECT id FROM {table}")}


def archive_event(oid, observed, state=peaks.REFERENCE, assets=None):
    o = {"id": oid, "observedAt": store.iso(observed), "lat": 37.0, "lon": 35.0, "tr": True, "provider": "Carbon Mapper",
         "measurement": {"quantityKind": "emission_rate_kg_h", "value": 5000.0, "unit": "kg/h"},
         "accessPaths": [{"provider": "Carbon Mapper", "id": oid}]}
    cls = {"state": state, "reasons": [{"rule": "sparse_top_n"}], "groups": {}}
    return archive.upsert(o, cls, assets=assets)


class RetentionTest(unittest.TestCase):
    def setUp(self):
        if config.DB_PATH.exists():
            config.DB_PATH.unlink()
        db.init()

    def test_exact_boundary_kept_one_second_older_deleted(self):
        edge = NOW - timedelta(days=90)
        insert_cm(cm_row("edge", edge), cm_row("older", edge - timedelta(seconds=1)), cm_row("new", NOW))
        retention.run(NOW)
        self.assertEqual(ids(), {"edge", "new"})

    def test_high_event_and_its_assets_survive(self):
        old = NOW - timedelta(days=400)
        insert_cm(cm_row("peak", old), cm_row("normal", old))
        asset = [{"role": "numeric_raster", "status": "archived", "url": "https://x/y.tif", "data": b"II*\0abc",
                  "ext": "tif", "media": "image/tiff", "license": "test"}]
        archive_event("peak", old, assets=asset)
        retention.run(NOW)
        self.assertEqual(ids(), {"peak"})
        with db.read() as con:
            sha, path = con.execute("SELECT a.sha256, a.path FROM archive_assets a").fetchone()
        self.assertTrue((retention.ARCHIVE_DIR / path).exists())
        self.assertEqual(retention.file_sha256(retention.ARCHIVE_DIR / path), sha)

    def test_idempotent(self):
        old = NOW - timedelta(days=200)
        insert_cm(cm_row("a", old), cm_row("b", NOW))
        first = retention.run(NOW)
        second = retention.run(NOW)
        self.assertEqual(first["deleted"]["cm_plumes"], 1)
        self.assertEqual(second["deleted"]["cm_plumes"], 0)
        self.assertEqual(ids(), {"b"})

    def test_keep_on_ingest_prevents_refill(self):
        old = {"id": "old", "observedAt": store.iso(NOW - timedelta(days=91))}
        self.assertFalse(retention.keep_on_ingest(old, frozenset(), NOW))
        self.assertTrue(retention.keep_on_ingest(old, frozenset({"old"}), NOW))
        self.assertTrue(retention.keep_on_ingest({"id": "n", "observedAt": store.iso(NOW)}, frozenset(), NOW))

    def test_active_job_defers_cleanup(self):
        old = NOW - timedelta(days=200)
        insert_cm(cm_row("busy", old))
        store.job_enqueue("img:busy", "imagery", {"obsId": "busy"})
        r = retention.run(NOW)
        self.assertEqual(r["skippedActiveJobs"], 1)
        self.assertIn("busy", ids())

    def test_related_cache_and_versions_removed(self):
        old = NOW - timedelta(days=200)
        insert_cm(cm_row("x", old))
        store.cache_put("wx:obs:x:om-era5-v2", {"a": 1})
        store.cache_put("clim:climate-monthly-v1:37.0:35.0:2000", {"keep": True})
        with db.tx() as con:
            con.execute("INSERT INTO plume_versions(id, modified, version, emission, seen_at) VALUES('x','m','v',1,'t')")
        retention.run(NOW)
        self.assertIsNone(store.cache_get("wx:obs:x:om-era5-v2"))
        self.assertIsNotNone(store.cache_get("clim:climate-monthly-v1:37.0:35.0:2000"))
        with db.read() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM plume_versions").fetchone()[0], 0)

    def test_shared_asset_kept_while_one_event_references_it(self):
        old = NOW - timedelta(days=300)
        data = [{"role": "plume_png", "status": "archived", "url": "u", "data": b"PNGDATA", "ext": "png",
                 "media": "image/png", "license": "t"}]
        archive_event("e1", old, assets=data)
        archive_event("e2", old, assets=data)
        archive.deactivate(["e1"])
        retention.run(NOW)
        with db.read() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM archive_assets").fetchone()[0], 1)
            self.assertEqual({r[0] for r in con.execute("SELECT id FROM peak_events")}, {"e2"})

    def test_integrity_error_defers_orphan_deletion_but_keeps_events(self):
        old = NOW - timedelta(days=300)
        archive_event("e1", old, assets=[{"role": "plume_png", "status": "archived", "url": "u", "data": b"AAA",
                                          "ext": "png", "media": "image/png", "license": "t"}])
        with db.tx() as con:
            path = con.execute("SELECT path FROM archive_assets").fetchone()[0]
            con.execute("INSERT INTO archive_assets(sha256, path, size, media_type, license, fetched_at) "
                        "VALUES('orphan', 'or/orphan.png', 1, 'image/png', 't', 'x')")
        (retention.ARCHIVE_DIR / path).write_bytes(b"tampered")
        r = retention.run(NOW)
        self.assertTrue(r["verificationErrors"])
        self.assertIn("orphan_archive_assets", r["deferred"])
        with db.read() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM archive_assets WHERE sha256='orphan'").fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM peak_events WHERE active=1").fetchone()[0], 1)

    def test_revision_history_kept(self):
        old = NOW - timedelta(days=10)
        self.assertEqual(archive_event("e", old), "created")
        self.assertEqual(archive_event("e", old), "unchanged")
        o = {"id": "e", "observedAt": store.iso(old), "lat": 37.0, "lon": 35.0, "tr": True, "provider": "Carbon Mapper",
             "modifiedAt": "2026-10-01T00:00:00Z",
             "measurement": {"quantityKind": "emission_rate_kg_h", "value": 4200.0, "unit": "kg/h"}, "accessPaths": []}
        self.assertEqual(archive.upsert(o, {"state": peaks.REFERENCE, "reasons": [], "groups": {}}), "revised")
        with db.read() as con:
            revs = con.execute("SELECT revision, obs FROM peak_event_revisions ORDER BY revision").fetchall()
        self.assertEqual([r[0] for r in revs], [1, 2])
        self.assertEqual(json.loads(revs[0][1])["measurement"]["value"], 5000.0)

    def test_signed_url_alone_is_not_archived(self):
        o = {"id": "s", "provider": "NASA LP DAAC", "thumbnail": {"url": "https://lpdaac/x.png?token=abc"}}
        res = archive.fetch_assets(o)
        self.assertEqual(res[0]["status"], "not_archived")
        self.assertNotIn("token", res[0]["url"])

    def test_rejected_artifact_is_slimmed_and_released(self):
        old = NOW - timedelta(days=200)
        insert_cm(cm_row("r", old))
        archive_event("r", old)
        archive.set_review("r", "rejected_artifact", "kenar artefaktı")
        retention.run(NOW)
        self.assertNotIn("r", ids())
        with db.read() as con:
            row = con.execute("SELECT state, active, review FROM peak_events WHERE id='r'").fetchone()
        # Pencere dışı ve etkin olmayan olay silinir; inceleme notu ölçülü audit olarak yalnızca pencere içinde kalır
        self.assertIsNone(row)

    def test_backfill_events_do_not_alert(self):
        from app import alerts
        o_old = NOW - timedelta(days=500)
        o = {"id": "b", "observedAt": store.iso(o_old), "lat": 37.0, "lon": 35.0, "tr": True, "accessPaths": [],
             "measurement": {"quantityKind": "emission_rate_kg_h", "value": 1.0, "unit": "kg/h"}}
        archive.upsert(o, {"state": peaks.CANDIDATE, "reasons": [], "groups": {}, "backfill": True})
        self.assertEqual(alerts._peak_events(NOW), [])
        o2 = {**o, "id": "n"}
        archive.upsert(o2, {"state": peaks.CANDIDATE, "reasons": [], "groups": {}, "backfill": False})
        self.assertEqual([e["kind"] for e in alerts._peak_events(NOW)], ["review_candidate"])


class JobQueueTest(unittest.TestCase):
    def setUp(self):
        if config.DB_PATH.exists():
            config.DB_PATH.unlink()
        db.init()

    def test_auth_blocked_job_is_not_retried_by_time_only_after_credentials_ok(self):
        store.job_enqueue("img:a", "imagery", {"obsId": "a"})
        self.assertEqual(store.job_fail("img:a", "auth", "401", 1), "blocked")
        with db.tx() as con:
            con.execute("UPDATE jobs SET updated_at = '2000-01-01T00:00:00Z'")
        self.assertFalse(store.job_enqueue("img:a", "imagery", {"obsId": "a"}))
        store.job_enqueue("cds:x", "cds_obs", {"obsId": "x"})
        store.job_fail("cds:x", "auth", "401", 1)
        self.assertEqual(store.requeue_after_credentials_ok(["imagery"]), 1)
        self.assertEqual(store.job_get("img:a")["state"], "queued")
        self.assertEqual(store.job_get("cds:x")["state"], "blocked")

    def test_quota_backoff_and_attempt_limit(self):
        store.job_enqueue("img:q", "imagery", {"obsId": "q"})
        self.assertEqual(store.job_fail("img:q", "quota", "429", 1), "queued")
        j = store.job_get("img:q")
        self.assertGreater(j["next_attempt_at"], store.iso(store.now()))
        self.assertEqual(store.jobs_by_state("imagery", ("queued",)), [])
        self.assertEqual(store.job_fail("img:q", "network", "x", store.MAX_TRANSIENT_ATTEMPTS), "failed")
        self.assertEqual(store.requeue_after_credentials_ok(["imagery"]), 0)

    def test_expired_lease_requeued(self):
        store.job_enqueue("img:l", "imagery", {"obsId": "l"})
        store.job_claim("img:l", lease=timedelta(seconds=-1))
        self.assertEqual(store.recover_expired_leases(), 1)
        self.assertEqual(store.job_get("img:l")["state"], "queued")

    def test_cdse_oauth_success_is_not_product_success(self):
        os.environ.update(CDSE_CLIENT_ID="id", CDSE_CLIENT_SECRET="secret")
        try:
            sources.failed("cdse", net.ProviderError("auth", "HTTP 401"))
            store.job_enqueue("img:z", "imagery", {"obsId": "z"})
            store.job_fail("img:z", "auth", "401", 1)
            with mock.patch.object(net, "request", return_value={"access_token": "t", "expires_in": 600}):
                self.assertTrue(imagery.check_connection())
            st = sources.get("cdse")
            self.assertEqual(st["state"], "credentials_ok")
            self.assertNotIn("last_product_at", st)
            self.assertEqual(store.job_get("img:z")["state"], "queued")
            from app import alerts
            self.assertFalse(next(s for s in alerts.source_status() if s["id"] == "cdse")["healthy"])
        finally:
            os.environ.pop("CDSE_CLIENT_ID")
            os.environ.pop("CDSE_CLIENT_SECRET")


if __name__ == "__main__":
    unittest.main()
