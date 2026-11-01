"""Copernicus Data Space (Sentinel Hub) Sentinel-2 L2A yüzey görüntüsü.

Seçilen gözlemin konumu ve tarihine en yakın sahne Catalog API ile bulunur, gerçek sahne tarihi ve bulut oranıyla
Process API'den doğal renkli PNG alınır. Görüntü metan ölçümü değildir; gözlemle aynı anda çekilmiş sayılmaz.
Kimlik: CDSE_CLIENT_ID / CDSE_CLIENT_SECRET (client_credentials), yalnızca sunucuda.
"""
import hashlib
import logging
import os
import time
import urllib.parse
from datetime import timedelta

from . import catalog, meteo, net, sources, store
from .config import DATA_DIR

log = logging.getLogger("imagery")

TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
CATALOG_URL = "https://sh.dataspace.copernicus.eu/catalog/v1/search"
PROCESS_URL = "https://sh.dataspace.copernicus.eu/process/v1"
COLLECTION = "sentinel-2-l2a"
IMAGE_DIR = DATA_DIR / "imagery"
SEARCH_DAYS = 20          # gözlem tarihinin ± bu kadar gün çevresinde sahne aranır
MAX_CLOUD = 60            # sahne bulut oranı üst sınırı (%)
HALF_SIZE_DEG = 0.03      # görüntü kutusunun yarı genişliği (~3 km)
SIZE_PX = 512
METHOD = "s2l2a-truecolor-v1"
TRUE_COLOR = """//VERSION=3
function setup(){return{input:["B02","B03","B04","dataMask"],output:{bands:4}};}
function evaluatePixel(s){return[2.5*s.B04,2.5*s.B03,2.5*s.B02,s.dataMask];}"""

_token = {"value": None, "exp": 0.0}


def configured():
    return bool(os.environ.get("CDSE_CLIENT_ID", "").strip() and os.environ.get("CDSE_CLIENT_SECRET", "").strip())


def _access_token():
    if _token["value"] and time.time() < _token["exp"] - 60:
        return _token["value"]
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": os.environ["CDSE_CLIENT_ID"].strip(),
        "client_secret": os.environ["CDSE_CLIENT_SECRET"].strip(),
    })
    d = net.request(TOKEN_URL, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
    _token.update(value=d["access_token"], exp=time.time() + float(d.get("expires_in", 300)))
    return _token["value"]


def _bbox(lat, lon):
    return [round(lon - HALF_SIZE_DEG, 5), round(lat - HALF_SIZE_DEG, 5),
            round(lon + HALF_SIZE_DEG, 5), round(lat + HALF_SIZE_DEG, 5)]


def search_scenes(lat, lon, observed):
    t0, t1 = observed - timedelta(days=SEARCH_DAYS), observed + timedelta(days=SEARCH_DAYS)
    body = {
        "collections": [COLLECTION], "bbox": _bbox(lat, lon),
        "datetime": f"{store.iso(t0)}/{store.iso(t1)}", "limit": 100,
        "filter": f"eo:cloud_cover < {MAX_CLOUD}", "filter-lang": "cql2-text",
    }
    d = net.request(CATALOG_URL, data=body, headers={"Authorization": f"Bearer {_access_token()}"}, timeout=40)
    scenes = []
    for f in d.get("features", []):
        props = f.get("properties", {})
        when = meteo.parse_utc(props.get("datetime"))
        if when is None:
            continue
        scenes.append({"id": f.get("id"), "datetime": store.iso(when), "cloudCover": props.get("eo:cloud_cover"),
                       "offsetDays": round((when - observed).total_seconds() / 86400, 2)})
    scenes.sort(key=lambda s: abs(s["offsetDays"]))
    return scenes


def render(lat, lon, scene):
    day = scene["datetime"][:10]
    body = {
        "input": {
            "bounds": {"bbox": _bbox(lat, lon), "properties": {"crs": "http://www.opengis.net/def/crs/OGC/1.3/CRS84"}},
            "data": [{"type": COLLECTION, "dataFilter": {
                "timeRange": {"from": f"{day}T00:00:00Z", "to": f"{day}T23:59:59Z"}, "mosaickingOrder": "leastCC"}}],
        },
        "output": {"width": SIZE_PX, "height": SIZE_PX, "responses": [{"identifier": "default", "format": {"type": "image/png"}}]},
        "evalscript": TRUE_COLOR,
    }
    return net.request(PROCESS_URL, data=body, raw=True, timeout=90,
                       headers={"Authorization": f"Bearer {_access_token()}", "Accept": "image/png"})


def _key(obs_id):
    return f"img:{obs_id}:{METHOD}"


def image_path(obs_id):
    return IMAGE_DIR / f"{hashlib.sha256(obs_id.encode()).hexdigest()[:24]}.png"


def status_for(obs):
    """İstemci için durum; gerekirse arka plan işini kuyruğa alır, indirmeyi beklemez."""
    if not configured():
        return {"status": "not_configured", "message": "Yüzey görüntüsü erişimi sunucuda tanımlı değil."}
    hit = store.cache_get(_key(obs["id"]))
    if hit:
        return hit[0]
    job_id = f"img:{obs['id']}"
    store.job_enqueue(job_id, "imagery", {"obsId": obs["id"]})
    job = store.job_get(job_id)
    if job and job["state"] in ("failed", "blocked"):
        return {"status": "unavailable", "errorClass": job["error_class"], "job": store.job_public(job)}
    return {"status": "queued", "job": store.job_public(job)}


def process_jobs(limit=2):
    if not configured():
        return
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    for job in store.jobs_by_state("imagery", ("queued",), limit=limit):
        obs, _, _ = catalog.get(job["payload"]["obsId"])
        if not obs or not meteo.parse_utc(obs.get("observedAt") or ""):
            store.job_update(job["id"], state="failed", error_class="not_found", error="Gözlem veya zamanı yok")
            continue
        observed = meteo.parse_utc(obs["observedAt"])
        store.job_update(job["id"], state="running", attempts=job["attempts"] + 1)
        try:
            scenes = search_scenes(obs["lat"], obs["lon"], observed)
            if not scenes:
                result = {"status": "empty", "searchedDays": SEARCH_DAYS, "maxCloudCover": MAX_CLOUD,
                          "message": f"Gözlem tarihinin ±{SEARCH_DAYS} günü içinde bulut oranı %{MAX_CLOUD} altında "
                                     "Sentinel-2 L2A sahnesi bulunamadı."}
            else:
                scene = scenes[0]
                png = render(obs["lat"], obs["lon"], scene)
                image_path(obs["id"]).write_bytes(png)
                result = {"status": "ready", "label": "Yüzey görüntüsü", "collection": "Sentinel-2 L2A",
                          "scene": scene, "bbox": _bbox(obs["lat"], obs["lon"]),
                          "imageUrl": f"/api/imagery/{urllib.parse.quote(obs['id'], safe='')}.png",
                          "provider": "Copernicus Data Space Ecosystem", "method": METHOD,
                          "note": "Doğal renkli yüzey görüntüsüdür; metan ölçümü değildir. Sahne tarihi gözlem "
                                  f"zamanından {abs(scene['offsetDays']):.1f} gün "
                                  + ("sonradır." if scene["offsetDays"] > 0 else "öncedir.")}
            store.cache_put(_key(obs["id"]), result)
            store.job_update(job["id"], state="done", progress=1.0, error_class=None, error=None)
            sources.succeeded("cdse")
        except net.ProviderError as e:
            state = "blocked" if e.kind in ("auth", "terms") else ("queued" if e.kind in ("quota", "network", "server")
                                                                    and job["attempts"] < 4 else "failed")
            store.job_update(job["id"], state=state, error_class=e.kind, error=str(e)[:200])
            sources.failed("cdse", e)
            if e.kind in ("auth", "quota"):
                return
