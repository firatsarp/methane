"""Run: python -m unittest discover -s tests"""
import copy
import unittest

from app import observations as obs

# 01.11.2026 tarihinde canlı Carbon Mapper yanıtından alınmış kayıt (görüntü bağlantıları kısaltıldı)
CM = {
    "plume_id": "tan20260927t091355c44s4001-A",
    "geometry_json": {"bbox": None, "type": "Point", "coordinates": [43.889144362936946, 36.18315650017935]},
    "scene_id": "705c783b-c9e2-4da2-bc02-f1e040ab89aa",
    "scene_timestamp": "2026-09-27T09:13:55.440Z",
    "instrument": "tan",
    "mission_phase": "production",
    "platform": "Planet Tanager 1",
    "emission_auto": 605.3486234765437,
    "emission_uncertainty_auto": 174.96683296925417,
    "plume_quality": None,
    "wind_speed_avg_auto": 2.5555555555555554,
    "wind_direction_avg_auto": 316.3250959011331,
    "sector": "6A",
    "hide_emission": False,
    "published_at": "2026-10-27T09:19:14.956Z",
}


def cm(**changes):
    p = copy.deepcopy(CM)
    p.update(changes)
    return p


class ParseNumberTest(unittest.TestCase):
    def test_values(self):
        cases = [
            (605.3, 605.3), (0, 0.0), ("605.3", 605.3), (" 12 ", 12.0), ("1e3", 1000.0),
            ("N/A", None), ("n/a", None), ("", None), ("  ", None), (None, None), ("12 kg/h", None),
            ("nan", None), ("inf", None), (float("nan"), None), (float("inf"), None), (True, None), ([1], None),
        ]
        for raw, expected in cases:
            self.assertEqual(obs.parse_number(raw), expected, repr(raw))

    def test_rate_rejects_negative(self):
        self.assertIsNone(obs.parse_rate(-1))
        self.assertIsNone(obs.parse_rate("-1"))
        self.assertEqual(obs.parse_rate("0"), 0.0)


class ParseTimeTest(unittest.TestCase):
    def test_requires_timezone(self):
        self.assertEqual(obs.parse_time("2026-09-27T09:13:55.440Z"), "2026-09-27T09:13:55.440000Z")
        self.assertEqual(obs.parse_time("2026-09-27T12:13:55+03:00"), "2026-09-27T12:13:55+03:00")
        for bad in ("2026-09-27T09:13:55", "2026-09-27", "", None, "N/A", "2026-13-40T00:00:00Z"):
            self.assertIsNone(obs.parse_time(bad), repr(bad))


class NormalizeTest(unittest.TestCase):
    def test_maps_live_record(self):
        o = obs.normalize_cm_plume(CM, ingested_at="2026-11-01T10:00:00+00:00")
        self.assertEqual(o["id"], CM["plume_id"])
        self.assertEqual((o["status"], o["scope"]), ("detected", "plume"))
        self.assertEqual(o["sensor"], "Planet Tanager-1")
        self.assertEqual(o["emission"]["unit"], "kg/h")
        self.assertAlmostEqual(o["emission"]["value"], 605.3486, places=3)
        self.assertAlmostEqual(o["emission"]["uncertainty"]["value"], 174.9668, places=3)
        # API istatistiksel tanım vermiyor; etiket uydurulmamalı
        self.assertIsNone(o["emission"]["uncertainty"]["label"])
        self.assertEqual(o["sector"], "solid_waste")
        self.assertEqual(o["sourceAttribution"], "unknown")
        self.assertEqual(o["publishedAt"], "2026-10-27T09:19:14.956000Z")
        self.assertEqual(o["ingestedAt"], "2026-11-01T10:00:00Z")
        self.assertIn("2,6 m/s", o["windNote"])
        self.assertIn("Güncel hava durumu değildir", o["windNote"])
        self.assertIsNone(o["qualityNote"])
        self.assertTrue(o["sourceUrl"].startswith("https://data.carbonmapper.org/?plume_id="))

    def test_missing_or_hidden_emission_is_not_zero(self):
        for p in (cm(emission_auto=None), cm(emission_auto="N/A"), cm(emission_auto=-3),
                  cm(hide_emission=True), cm(emission_auto="")):
            self.assertIsNone(obs.normalize_cm_plume(p)["emission"])

    def test_numeric_string_emission_is_accepted(self):
        self.assertEqual(obs.normalize_cm_plume(cm(emission_auto="605.5"))["emission"]["value"], 605.5)

    def test_missing_uncertainty_is_none_not_zero(self):
        self.assertIsNone(obs.normalize_cm_plume(cm(emission_uncertainty_auto=None))["emission"]["uncertainty"])

    def test_time_without_zone_is_unverified(self):
        self.assertIsNone(obs.normalize_cm_plume(cm(scene_timestamp="2026-09-27T09:13:55"))["observedAt"])

    def test_sector_mapping(self):
        self.assertEqual(obs.normalize_cm_plume(cm(sector="1B2"))["sector"], "other")
        self.assertEqual(obs.normalize_cm_plume(cm(sector=None))["sector"], "unknown")

    def test_non_production_phase_is_noted(self):
        self.assertIn("first_light", obs.normalize_cm_plume(cm(mission_phase="first_light"))["qualityNote"])

    def test_invalid_record_is_dropped(self):
        self.assertIsNone(obs.normalize_cm_plume(cm(plume_id=None)))
        self.assertIsNone(obs.normalize_cm_plume(cm(geometry_json={"coordinates": ["N/A", 36.1]})))

    def test_detection_is_never_marked_valid_negative(self):
        o = obs.normalize_cm_plume(CM)
        self.assertEqual(o["status"], "detected")
        self.assertFalse(o["validObservationConfirmed"])


class DedupeTest(unittest.TestCase):
    def test_same_id_and_same_scene_location(self):
        a = obs.normalize_cm_plume(CM)
        same_id = obs.normalize_cm_plume(CM)
        # Aynı EMIT/Tanager sahnesi farklı kimlikle ikinci kez gelirse tek gözlem sayılır
        same_scene = obs.normalize_cm_plume(cm(plume_id="other-id"))
        other = obs.normalize_cm_plume(cm(plume_id="x", geometry_json={"coordinates": [28.1488, 41.2135]}))
        unique, dropped = obs.dedupe([a, same_id, same_scene, other])
        self.assertEqual([o["id"] for o in unique], [CM["plume_id"], "x"])
        self.assertEqual(dropped, 2)


if __name__ == "__main__":
    unittest.main()
