"""CelesTrak TLE'leri ile Sentinel-5P ve ISS (EMIT) Türkiye geçiş tahmini."""
import logging
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import numpy as np
from skyfield.api import EarthSatellite, load, wgs84

from .config import DATA_DIR, SATELLITES

log = logging.getLogger("passes")

TLE_MAX_AGE = 12 * 3600  # CelesTrak 2 saatten sık sorgulanmamasını istiyor
ANKARA = (32.86, 39.93)

_ts = load.timescale()
_lock = threading.Lock()
_sats = {}
_cache = {"at": 0.0, "passes": None}


def _tle_path(norad):
    return DATA_DIR / f"tle_{norad}.txt"


def _fetch_tle(norad):
    path = _tle_path(norad)
    fresh = path.exists() and time.time() - path.stat().st_mtime < TLE_MAX_AGE
    if not fresh:
        url = f"https://celestrak.org/NORAD/elements/gp.php?CATNR={norad}&FORMAT=tle"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "metan-turkiye/1.0"})
            text = urllib.request.urlopen(req, timeout=20).read().decode()
            if text.count("\n") >= 2 and text.lstrip().split("\n")[1].startswith("1 "):
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
        except Exception as e:
            log.warning("TLE alınamadı (%s): %s", norad, e)
    if not path.exists():
        return None
    lines = [l.rstrip() for l in path.read_text().splitlines() if l.strip()]
    return lines[0].strip(), lines[1], lines[2]


def satellites():
    with _lock:
        for key, cfg in SATELLITES.items():
            loaded = _sats.get(key)
            if loaded is None or time.time() - loaded[1] > TLE_MAX_AGE:
                tle = _fetch_tle(cfg["norad"])
                if tle:
                    _sats[key] = (EarthSatellite(tle[1], tle[2], tle[0], _ts), time.time())
        return {k: v[0] for k, v in _sats.items()}


def _subpoints(sat, times):
    sp = wgs84.subpoint_of(sat.at(times))
    lon = (sp.longitude.degrees + 180) % 360 - 180
    return lon, sp.latitude.degrees


def _local_solar_hour(dt, lon):
    return (dt.hour + dt.minute / 60 + lon / 15) % 24


def compute_passes(hours=72, step_s=20):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    n = int(hours * 3600 / step_s)
    times = _ts.from_datetimes([now + timedelta(seconds=k * step_s) for k in range(n)])
    out = []
    for key, sat in satellites().items():
        cfg = SATELLITES[key]
        lon, lat = _subpoints(sat, times)
        x0, y0, x1, y1 = cfg["coverage"]
        inside = (lon >= x0) & (lon <= x1) & (lat >= y0) & (lat <= y1)
        edges = np.diff(np.concatenate([[0], inside.astype(int), [0]]))
        for a, b in zip(np.where(edges == 1)[0], np.where(edges == -1)[0]):
            a, b = int(a), int(b)  # timedelta numpy tamsayısı kabul etmiyor
            seg = slice(a, b)
            d2 = (lon[seg] - ANKARA[0]) ** 2 + (lat[seg] - ANKARA[1]) ** 2
            c = a + int(np.argmin(d2))
            mid = now + timedelta(seconds=c * step_s)
            solar = _local_solar_hour(mid, lon[c])
            # Track: geçişten biraz önce ve sonrası, haritada çizmek için
            t0, t1 = max(a - 30, 0), min(b + 30, n)
            idx = np.linspace(t0, t1 - 1, min(60, t1 - t0)).astype(int)
            out.append({
                "sat": key,
                "name": cfg["name"],
                "start": (now + timedelta(seconds=int(a) * step_s)).isoformat(),
                "end": (now + timedelta(seconds=int(b) * step_s)).isoformat(),
                "closest": mid.isoformat(),
                "closest_lon": round(float(lon[c]), 2),
                "closest_lat": round(float(lat[c]), 2),
                "daylight": bool(7 <= solar <= 17),
                "direction": "kuzeye" if lat[min(b, n - 1)] > lat[a] else "güneye",
                "track": [[round(float(lon[k]), 3), round(float(lat[k]), 3)] for k in idx],
            })
    out.sort(key=lambda p: p["start"])
    return out


def passes():
    if _cache["passes"] is None or time.time() - _cache["at"] > 900:
        _cache["passes"] = compute_passes()
        _cache["at"] = time.time()
    now = datetime.now(timezone.utc).isoformat()
    return [p for p in _cache["passes"] if p["end"] > now]


def positions():
    now = datetime.now(timezone.utc)
    times = _ts.from_datetimes([now + timedelta(minutes=m) for m in range(0, 101, 2)])
    res = {}
    for key, sat in satellites().items():
        lon, lat = _subpoints(sat, times)
        res[key] = {
            "name": SATELLITES[key]["name"],
            "lon": round(float(lon[0]), 3),
            "lat": round(float(lat[0]), 3),
            "ahead": [[round(float(x), 2), round(float(y), 2)] for x, y in zip(lon, lat)],
        }
    return {"time": now.isoformat(), "satellites": res}
