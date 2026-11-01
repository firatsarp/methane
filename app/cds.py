"""Copernicus Climate Data Store (CDS) ERA5 single levels: gözlem anını çevreleyen saatler için arka plan işi.

İndirme HTTP isteği içinde beklenmez: istek kuyruğa alınır, zamanlayıcı CDS'e gönderir ve durumunu izler.
Kimlik: CDSAPI_KEY (Personal Access Token), CDSAPI_URL. Değerler loglanmaz ve istemciye gönderilmez.
"""
import logging
import math
import os
import shutil
import tempfile
import zipfile
from datetime import timedelta, timezone
from pathlib import Path

from . import meteo, sources, store
from .config import TMP_DIR

log = logging.getLogger("cds")

DATASET = "reanalysis-era5-single-levels"
CATALOGUE_URL = "https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels"
METHOD = "cds-era5-v1"
VARIABLES = [
    "10m_u_component_of_wind", "10m_v_component_of_wind",
    "100m_u_component_of_wind", "100m_v_component_of_wind",
    "2m_temperature", "2m_dewpoint_temperature", "surface_pressure",
    "total_cloud_cover", "boundary_layer_height", "total_precipitation",
]
# NetCDF kısa adları -> (etiket, yükseklik m, aralık)
SHORT = {
    "t2m": ("Sıcaklık", 2, "anlık"), "d2m": ("Çiy noktası", 2, "anlık"), "sp": ("Yüzey basıncı", 0, "anlık"),
    "tcc": ("Toplam bulutluluk", None, "anlık"), "blh": ("Sınır tabakası yüksekliği", None, "anlık"),
    "tp": ("Yağış", None, "önceki 1 saatin toplamı"),
}
# ERA5 son ~5 günü henüz yayımlamaz; daha yeni gözlemler için iş beklemede kalır
ERA5_LATENCY = timedelta(days=6)
MAX_ACTIVE = 2
MAX_ATTEMPTS = 5
HALF_BOX = 0.25


def configured():
    return bool(os.environ.get("CDSAPI_KEY", "").strip())


def _client():
    # cdsapi 0.7+ yeni CDS için ecmwf-datastores istemcisini kullanır; engellemeyen gönderim/izleme için doğrudan çağrılır
    from ecmwf.datastores import Client
    return Client(url=os.environ.get("CDSAPI_URL", "").strip() or "https://cds.climate.copernicus.eu/api",
                  key=os.environ["CDSAPI_KEY"].strip(), progress=False, maximum_tries=1, timeout=60)


def _job_id(obs_id):
    return f"cds:{obs_id}"


def _cache_key(obs_id):
    return f"wx:obs:{obs_id}:{METHOD}"


def build_request(lat, lon, observed):
    """Gözlem saatini çevreleyen saatler (önceki tam saat −1 ... sonraki tam saat +1), küçük alan."""
    t0 = observed.replace(minute=0, second=0, microsecond=0)
    hours = [t0 + timedelta(hours=k) for k in (-1, 0, 1, 2)]
    days = sorted({h.date() for h in hours})
    return {
        "product_type": ["reanalysis"],
        "variable": VARIABLES,
        "year": sorted({f"{d.year}" for d in days}),
        "month": sorted({f"{d.month:02d}" for d in days}),
        "day": sorted({f"{d.day:02d}" for d in days}),
        "time": sorted({f"{h.hour:02d}:00" for h in hours}),
        # Kuzey, batı, güney, doğu
        "area": [round(lat + HALF_BOX, 2), round(lon - HALF_BOX, 2), round(lat - HALF_BOX, 2), round(lon + HALF_BOX, 2)],
        "data_format": "netcdf",
        "download_format": "unarchived",
    }


def classify(exc):
    text = str(exc).lower()
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if "licen" in text or "terms" in text:
        return "terms"
    if status == 401 or "401" in text or "authentication" in text or "unauthorized" in text:
        return "auth"
    if status == 403 or "403" in text or "forbidden" in text:
        return "terms" if "licen" in text else "auth"
    if status == 429 or "429" in text or "too many" in text or "quota" in text:
        return "quota"
    if "timed out" in text or "connection" in text:
        return "network"
    return "server"


def observation_result(obs):
    """(CDS meteoroloji bloğu ya da None, iş kaydı ya da None). Yapılandırılmışsa iş kuyruğa alınır."""
    hit = store.cache_get(_cache_key(obs["id"]))
    job = store.job_get(_job_id(obs["id"]))
    if hit:
        return hit[0], job
    if not configured():
        return None, job
    observed = meteo.parse_utc(obs.get("observedAt"))
    if observed is None:
        return None, job
    if store.now() - observed < ERA5_LATENCY:
        return {"status": "not_published", "provider": "Copernicus CDS", "dataset": "ERA5",
                "accessPath": "ERA5 — Copernicus CDS"}, job
    store.job_enqueue(_job_id(obs["id"]), "cds_obs",
                      {"obsId": obs["id"], "lat": obs["lat"], "lon": obs["lon"], "observedAt": obs["observedAt"]})
    return None, store.job_get(_job_id(obs["id"]))


# ---- NetCDF çözümleme ---------------------------------------------------------------

def _netcdf_files(path, workdir):
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.endswith(".nc")]
            z.extractall(workdir, members=names)
        return [str(Path(workdir) / n) for n in names]
    return [path]


def parse_files(paths, lat, lon, observed):
    """ERA5 NetCDF dosyalarından gözlem noktasına en yakın ızgara hücresi ve gözlem anına en yakın saat."""
    import netCDF4
    import numpy as np

    merged, units, times, grid = {}, {}, None, None
    for p in paths:
        with netCDF4.Dataset(p) as ds:
            tname = "valid_time" if "valid_time" in ds.variables else "time"
            tv = ds.variables[tname]
            ts = netCDF4.num2date(tv[:], tv.units, only_use_cftime_datetimes=False, only_use_python_datetimes=True)
            lats = np.asarray(ds.variables["latitude"][:], dtype=float)
            lons = np.asarray(ds.variables["longitude"][:], dtype=float)
            j = int(np.argmin(np.abs(lats - lat)))
            i = int(np.argmin(np.abs(lons - lon)))
            grid = {"lat": float(lats[j]), "lon": float(lons[i])}
            stamps = [t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t for t in ts]
            times = times or stamps
            for name, var in ds.variables.items():
                if name in (tname, "latitude", "longitude", "number", "expver") or var.ndim < 3:
                    continue
                arr = np.ma.filled(var[:], np.nan).astype(float)
                # (zaman, enlem, boylam); ek boyut varsa ilk eleman
                while arr.ndim > 3:
                    arr = arr[:, 0]
                merged[name] = [None if not math.isfinite(x) else float(x) for x in arr[:, j, i]]
                units[name] = getattr(var, "units", None)
    if times is None:
        return None
    idx, offset = meteo.nearest_index(times, observed)
    return {"times": times, "values": merged, "units": units, "grid": grid, "idx": idx, "offset": offset}


def to_block(parsed, obs, fetched_at, request_id):
    base = {
        "requestedLocation": {"lat": obs["lat"], "lon": obs["lon"]},
        "resolvedGridPoint": parsed["grid"] if parsed else None,
        "requestedAt": obs["observedAt"],
        "fetchedAt": fetched_at,
        "provider": "Copernicus CDS",
        "accessPath": "ERA5 — Copernicus CDS",
        "dataset": DATASET,
        "model": "ERA5",
        "kind": "reanalysis",
        "temporalResolution": "1 saat",
        "spatialResolution": "0,25° (yaklaşık 25–31 km)",
        "sourceUrl": CATALOGUE_URL,
        "provenance": {"method": METHOD, "requestId": request_id},
    }
    if not parsed or parsed["idx"] is None:
        return {**base, "status": "empty", "validAt": None,
                "timeOffsetMinutes": parsed["offset"] if parsed else None}
    k, v, u = parsed["idx"], parsed["values"], parsed["units"]
    at = lambda name: v.get(name, [None] * (k + 1))[k] if name in v else None  # noqa: E731
    variables = {}
    for name, (label, height, interval) in SHORT.items():
        raw = at(name)
        if raw is None:
            continue
        if name == "tp" and u.get(name) == "m":
            value, unit = meteo.precip_m_to_mm(raw), "mm"
        else:
            value, unit = meteo.convert(raw, u.get(name))
        variables[name] = {"value": value, "unit": unit, "heightM": height, "interval": interval, "label": label}
    t2m, d2m = variables.get("t2m"), variables.get("d2m")
    if t2m and d2m and t2m["unit"] == "°C" and d2m["unit"] == "°C":
        variables["rh"] = {"value": meteo.humidity_from_dewpoint(t2m["value"], d2m["value"]), "unit": "%",
                           "heightM": 2, "interval": "anlık", "label": "Bağıl nem (çiy noktasından hesaplandı)"}

    def wind(uname, vname):
        s, f, t = meteo.wind_from_uv(at(uname), at(vname))
        return None if s is None else {"speed": s, "unit": "m/s", "from": f, "to": t, "calm": f is None}

    return {**base, "status": "ready", "validAt": store.iso(parsed["times"][k]),
            "timeOffsetMinutes": parsed["offset"], "variables": variables,
            "wind10": wind("u10", "v10"), "wind100": wind("u100", "v100")}


# ---- Zamanlayıcı işi -----------------------------------------------------------------

def _finish(job, client, remote_id):
    obs = job["payload"]
    tmp = tempfile.mkdtemp(dir=TMP_DIR)
    try:
        target = os.path.join(tmp, "era5.nc")
        client.get_remote(remote_id).download(target)
        parsed = parse_files(_netcdf_files(target, tmp), obs["lat"], obs["lon"], meteo.parse_utc(obs["observedAt"]))
        block = to_block(parsed, {"id": obs["obsId"], **obs}, store.iso(store.now()), remote_id)
        store.cache_put(_cache_key(obs["obsId"]), block)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def process_jobs():
    if not configured():
        return
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    try:
        client = _client()
    except Exception as e:
        sources.failed("cds", e, error_class=classify(e))
        return
    active = store.jobs_by_state("cds_obs", ("submitted", "running"))
    for job in active:
        try:
            status = client.get_remote(job["remote_id"]).status
        except Exception as e:
            kind = classify(e)
            log.warning("CDS iş durumu alınamadı (%s): %s", job["id"], kind)
            store.job_update(job["id"], error_class=kind)
            continue
        if status in ("accepted",):
            store.job_update(job["id"], state="submitted")
        elif status == "running":
            store.job_update(job["id"], state="running")
        elif status == "successful":
            try:
                _finish(job, client, job["remote_id"])
                store.job_update(job["id"], state="done", progress=1.0, error_class=None, error=None)
                sources.succeeded("cds", last_job=job["remote_id"])
            except Exception as e:
                log.exception("CDS sonucu işlenemedi: %s", job["id"])
                store.job_update(job["id"], state="failed", error_class=classify(e), error=str(e)[:200])
        else:
            store.job_update(job["id"], state="failed", error_class="server", error=f"CDS durumu: {status}")

    free = MAX_ACTIVE - len(store.jobs_by_state("cds_obs", ("submitted", "running")))
    for job in store.jobs_by_state("cds_obs", ("queued",), limit=max(0, free)):
        p = job["payload"]
        req = build_request(p["lat"], p["lon"], meteo.parse_utc(p["observedAt"]))
        try:
            remote = client.submit(DATASET, req)
        except Exception as e:
            kind = classify(e)
            attempts = job["attempts"] + 1
            # Erişim veya lisans eksikse yeniden denemek anlamsız: kesin tanıyla engellenir
            state = "blocked" if kind in ("auth", "terms") else ("queued" if attempts < MAX_ATTEMPTS else "failed")
            store.job_update(job["id"], state=state, attempts=attempts, error_class=kind, error=str(e)[:200])
            sources.failed("cds", e, error_class=kind)
            log.warning("CDS gönderimi başarısız (%s): %s", job["id"], kind)
            if kind in ("auth", "terms", "quota"):
                break
            continue
        store.job_update(job["id"], state="submitted", remote_id=remote.request_id, attempts=job["attempts"] + 1)
        sources.checked("cds", state="ok", error="", error_class="")


def check_connection():
    """Kimlik ve erişimi gerçek bir API çağrısıyla dener; yalnızca token tanımlı olmak 'sağlıklı' sayılmaz."""
    if not configured():
        return
    try:
        _client().check_authentication()
    except Exception as e:
        sources.failed("cds", e, error_class=classify(e))
        return
    sources.succeeded("cds", auth="ok")
