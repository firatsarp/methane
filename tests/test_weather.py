"""Meteoroloji akışları: Open-Meteo yanıtları taklit edilir, CDS NetCDF çözümlemesi gerçek dosya biçimiyle sınanır."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import _env  # noqa: F401
from app import cds, climate, config, db, net, store, weather

OBS = {"id": "tan20260926t095339c05s4001-A", "observedAt": "2026-09-26T09:53:39.050Z", "lat": 37.7407, "lon": 32.6109}


def archive_payload(params):
    """Open-Meteo archive yanıtı biçiminde 3 günlük saatlik seri."""
    start = datetime.fromisoformat(params["start_date"]).replace(tzinfo=timezone.utc)
    times = [(start + timedelta(hours=k)).strftime("%Y-%m-%dT%H:%M") for k in range(72)]
    hourly = {"time": times}
    units = {"time": "iso8601"}
    unit_of = {"temperature_2m": "°C", "precipitation": "mm", "wind_speed_10m": "m/s", "wind_direction_10m": "°",
               "relative_humidity_2m": "%", "cloud_cover": "%", "surface_pressure": "hPa",
               "wind_speed_100m": "m/s", "wind_direction_100m": "°", "boundary_layer_height": "m"}
    for var in params["hourly"].split(","):
        if var == "boundary_layer_height" and params.get("_no_blh"):
            raise net.ProviderError("bad_response", "HTTP 400 unsupported", 400)
        hourly[var] = [float(k) for k in range(72)]
        units[var] = unit_of[var]
    return {"latitude": 37.75, "longitude": 32.5, "elevation": 1010.0, "hourly": hourly, "hourly_units": units}


class ObservationWeatherTest(unittest.TestCase):
    def setUp(self):
        if config.DB_PATH.exists():
            config.DB_PATH.unlink()
        db.init()
        os.environ.pop("CDSAPI_KEY", None)

    def test_matches_own_hour_and_reports_offset(self):
        with mock.patch.object(net, "get_json", side_effect=lambda url, params, **kw: archive_payload(params)):
            r = weather.observation_weather(OBS)
        p = r["primary"]
        self.assertEqual(r["status"], "ready")
        self.assertEqual(p["validAt"], "2026-09-26T10:00Z")
        self.assertEqual(p["timeOffsetMinutes"], 6)
        self.assertEqual(p["accessPath"], "ERA5 — Open-Meteo üzerinden")
        self.assertEqual(p["model"], "era5")
        self.assertEqual(p["variables"]["wind_speed_10m"]["heightM"], 10)
        self.assertEqual(p["variables"]["precipitation"]["interval"], "önceki 1 saatin toplamı")
        self.assertIsNone(r["cdsJob"])

    def test_unsupported_optional_variable_does_not_drop_request(self):
        def fake(url, params, **kw):
            return archive_payload({**params, "_no_blh": True})
        with mock.patch.object(net, "get_json", side_effect=fake):
            r = weather.observation_weather(OBS)
        self.assertEqual(r["status"], "ready")
        self.assertIn("boundary_layer_height", r["primary"]["missingVariables"])
        self.assertNotIn("boundary_layer_height", r["primary"]["variables"])

    def test_recent_observation_without_reanalysis_is_empty_not_forecast(self):
        recent = {**OBS, "id": "recent", "observedAt": store.iso(store.now() - timedelta(days=1))}

        def fake(url, params, **kw):
            d = archive_payload(params)
            for k in d["hourly"]:
                if k != "time":
                    d["hourly"][k] = [None] * 72
            return d
        with mock.patch.object(net, "get_json", side_effect=fake):
            r = weather.observation_weather(recent)
        self.assertEqual(r["status"], "empty")
        self.assertIsNone(r["primary"])
        self.assertEqual(r["alternatives"][0]["status"], "not_published")

    def test_provider_failure_without_cache_is_unavailable_not_infinite(self):
        with mock.patch.object(net, "get_json", side_effect=net.ProviderError("quota", "HTTP 429")):
            r = weather.observation_weather(OBS)
        self.assertEqual(r["status"], "unavailable")

    def test_cds_configured_queues_job_once(self):
        with mock.patch.dict(os.environ, {"CDSAPI_KEY": "k"}), \
                mock.patch.object(net, "get_json", side_effect=lambda url, params, **kw: archive_payload(params)):
            r1 = weather.observation_weather(OBS)
            r2 = weather.observation_weather(OBS)
        self.assertEqual(r1["cdsJob"]["state"], "queued")
        self.assertEqual(r2["cdsJob"]["id"], r1["cdsJob"]["id"])
        with db.read() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM jobs WHERE kind='cds_obs'").fetchone()[0], 1)


class ForecastTest(unittest.TestCase):
    def setUp(self):
        if config.DB_PATH.exists():
            config.DB_PATH.unlink()
        db.init()

    def test_forecast_is_labelled_model_and_cached(self):
        calls = []

        def fake(url, params, **kw):
            calls.append(params)
            if "current" in params:
                return {"current_units": {"temperature_2m": "°C", "wind_speed_10m": "m/s", "wind_direction_10m": "°"},
                        "current": {"time": "2026-11-01T17:45", "temperature_2m": 15.9, "wind_speed_10m": 3.0,
                                    "wind_direction_10m": 270.0}}
            d = archive_payload({**params, "start_date": "2026-11-01"})
            return d
        with mock.patch.object(net, "get_json", side_effect=fake):
            r = weather.forecast(41.15, 28.2)
            r2 = weather.forecast(41.16, 28.21)
        self.assertEqual(r["kind"], "forecast")
        self.assertIn("yer istasyonu ölçümü değildir", r["modelNote"])
        self.assertEqual(r["current"]["wind10"]["to"], 90.0)
        self.assertEqual(r2["cache"], "hit")
        self.assertEqual(len(calls), 3)

    def test_failure_returns_last_data_as_stale(self):
        store.cache_put("wx:fc:41.15:28.2", {"kind": "forecast", "fetchedAt": "x"}, timedelta(seconds=-1))
        with mock.patch.object(net, "get_json", side_effect=net.ProviderError("network", "down")):
            r = weather.forecast(41.15, 28.2)
        self.assertEqual(r["status"], "stale")
        self.assertEqual(r["fetchedAt"], "x")


class CdsParseTest(unittest.TestCase):
    def test_netcdf_parse_units_and_wind(self):
        import netCDF4
        import numpy as np
        d = tempfile.mkdtemp()
        path = os.path.join(d, "era5.nc")
        with netCDF4.Dataset(path, "w") as ds:
            ds.createDimension("valid_time", 2)
            ds.createDimension("latitude", 3)
            ds.createDimension("longitude", 3)
            t = ds.createVariable("valid_time", "i8", ("valid_time",))
            t.units = "seconds since 1970-01-01"
            base = int(datetime(2026, 9, 26, 9, tzinfo=timezone.utc).timestamp())
            t[:] = [base, base + 3600]
            ds.createVariable("latitude", "f4", ("latitude",))[:] = [38.0, 37.75, 37.5]
            ds.createVariable("longitude", "f4", ("longitude",))[:] = [32.25, 32.5, 32.75]
            for name, units, val in (("u10", "m s**-1", 5.0), ("v10", "m s**-1", 0.0), ("t2m", "K", 293.15),
                                     ("d2m", "K", 283.15), ("sp", "Pa", 90000.0), ("tcc", "(0 - 1)", 0.4),
                                     ("blh", "m", 1200.0), ("tp", "m", 0.0005)):
                v = ds.createVariable(name, "f4", ("valid_time", "latitude", "longitude"))
                v.units = units
                v[:] = np.full((2, 3, 3), val)
        observed = datetime(2026, 9, 26, 9, 53, 39, tzinfo=timezone.utc)
        parsed = cds.parse_files([path], OBS["lat"], OBS["lon"], observed)
        self.assertEqual(parsed["grid"], {"lat": 37.75, "lon": 32.5})
        block = cds.to_block(parsed, OBS, "2026-11-01T00:00:00Z", "req-1")
        self.assertEqual(block["validAt"], "2026-09-26T10:00:00Z")
        self.assertEqual(block["timeOffsetMinutes"], 6)
        v = block["variables"]
        self.assertAlmostEqual(v["t2m"]["value"], 20.0, places=4)
        self.assertEqual(v["t2m"]["unit"], "°C")
        self.assertAlmostEqual(v["sp"]["value"], 900.0)
        self.assertAlmostEqual(v["tcc"]["value"], 40.0, places=4)
        self.assertAlmostEqual(v["tp"]["value"], 0.5, places=4)
        self.assertEqual(v["tp"]["unit"], "mm")
        self.assertEqual(block["wind10"]["from"], 270.0)
        self.assertEqual(block["wind10"]["to"], 90.0)

    def test_request_is_small_and_surrounds_observation(self):
        req = cds.build_request(41.2, 28.1, datetime(2026, 9, 26, 23, 40, tzinfo=timezone.utc))
        self.assertEqual(req["time"], ["00:00", "01:00", "22:00", "23:00"])
        self.assertEqual(req["day"], ["26", "27"])
        self.assertEqual(req["area"], [41.45, 27.85, 40.95, 28.35])
        self.assertEqual(req["product_type"], ["reanalysis"])

    def test_licence_error_is_terms(self):
        self.assertEqual(cds.classify(Exception("403 Client Error: required licences not accepted")), "terms")
        self.assertEqual(cds.classify(Exception("401 Unauthorized")), "auth")
        self.assertEqual(cds.classify(Exception("429 Too Many Requests")), "quota")


class ClimateJobTest(unittest.TestCase):
    def setUp(self):
        if config.DB_PATH.exists():
            config.DB_PATH.unlink()
        db.init()

    def test_first_request_queues_without_downloading_in_request(self):
        with mock.patch.object(net, "get_json", side_effect=AssertionError("HTTP isteği içinde indirme yapılmamalı")):
            r = climate.climate(41.2156, 28.1475)
        self.assertEqual(r["status"], "queued")
        self.assertEqual(r["cell"], {"lat": 41.25, "lon": 28.25})
        self.assertEqual(r["progress"]["yearsNeeded"], 30)


if __name__ == "__main__":
    unittest.main()
