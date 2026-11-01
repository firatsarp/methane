"""NASA EMIT incelenmiş metan bulut kompleksleri (EMITL2BCH4PLM), NASA CMR + LP DAAC üzerinden.

Koleksiyonun güncel sürümü her çalışmada CMR'den bulunur. Her granülün PLMMETA GeoJSON dosyası (Earthdata
yetkisi gerekir) bulut sınırını, en yüksek artışı ve sağlayıcının emisyon tahminini taşır. Granül bulunmaması
negatif gözlem üretmez; yalnızca var olan kayıtlar alınır. Aynı EMIT sahnesi Carbon Mapper'da da varsa
katalog katmanında tek gözlem olarak birleştirilir.
"""
import json
import logging

from . import db, net, observations, sources, store
from .config import BBOX
from .geo import in_turkey

log = logging.getLogger("emit")

CMR = "https://cmr.earthdata.nasa.gov/search"
SHORT_NAME = "EMITL2BCH4PLM"
PROVIDER = "NASA LP DAAC"
SENSOR = "NASA EMIT (ISS)"


def latest_collection():
    d = net.get_json(f"{CMR}/collections.json", {"short_name": SHORT_NAME, "page_size": 20}, timeout=30)
    entries = d.get("feed", {}).get("entry", [])
    if not entries:
        raise net.ProviderError("not_found", f"CMR'de {SHORT_NAME} koleksiyonu bulunamadı")
    # Sürüm numarası "002", "003" gibi; en yükseği güncel üründür
    best = max(entries, key=lambda e: int(e.get("version_id", "0")) if e.get("version_id", "0").isdigit() else 0)
    return best["id"], best.get("version_id")


def search_granules(concept_id):
    items, page = [], 1
    while True:
        d = net.get_json(f"{CMR}/granules.umm_json", {
            "collection_concept_id": concept_id, "bounding_box": ",".join(map(str, BBOX)),
            "page_size": 200, "page_num": page, "sort_key": "-start_date",
        }, timeout=60)
        batch = d.get("items", [])
        items += batch
        if len(batch) < 200:
            return items
        page += 1


def _urls(umm):
    out = {}
    for r in umm.get("RelatedUrls", []):
        url = r.get("URL", "")
        if not url.startswith("https://"):
            continue
        if url.endswith(".json") and "PLMMETA" in url:
            out["meta"] = url
        elif url.endswith(".tif") and r.get("Type") == "GET DATA":
            out["tif"] = url
        elif url.endswith(".png"):
            out["png"] = url
    return out


def _attr(umm, name):
    for a in umm.get("AdditionalAttributes", []):
        if a.get("Name") == name and a.get("Values"):
            return a["Values"][0]
    return None


def normalize(meta_feature, umm, concept_id, granule_concept_id, version, urls, ingested_at):
    """PLMMETA özellikleri + UMM metadata'sı -> observationSchema. Geçersizse None."""
    p = meta_feature.get("properties", {})
    pid = observations._text(p.get("Plume ID"))
    lat = observations.parse_number(p.get("Latitude of max concentration"))
    lon = observations.parse_number(p.get("Longitude of max concentration"))
    if not pid or lat is None or lon is None:
        return None
    value = observations.parse_rate(p.get("Emissions Rate Estimate (kg/hr)"))
    unc = observations.parse_rate(p.get("Emissions Rate Estimate Uncertainty (kg/hr)"))
    wind = observations.parse_number(p.get("Wind Speed (m/s)"))
    wind_src = observations._text(p.get("Wind Speed Source"))
    build = _attr(umm, "EMIT_GHG_SOFTWARE_BUILD_VERSION")
    return {
        "id": f"emit:{pid}",
        "providerId": pid,
        "status": "detected",
        "scope": "plume",
        "gas": "CH4",
        "provider": PROVIDER,
        "sensor": SENSOR,
        "instrument": "emi",
        "platform": "ISS",
        "observedAt": observations.parse_time(p.get("UTC Time Observed")),
        "publishedAt": None,
        "ingestedAt": ingested_at,
        "lat": lat,
        "lon": lon,
        "locationLabel": f"{lat:.3f}° K, {lon:.3f}° D".replace(".", ","),
        "sourceAttribution": "unknown",
        "emission": {"value": value, "unit": "kg/h",
                     "uncertainty": {"value": unc, "label": None} if unc is not None else None} if value is not None else None,
        "maxEnhancement": {"value": observations.parse_number(p.get("Max Plume Concentration (ppm m)")), "unit": "ppm·m"},
        "windNote": (f"Sağlayıcının emisyon hesabında kullandığı rüzgâr: {wind:.1f} m/s ({wind_src or 'kaynak belirtilmemiş'})."
                     .replace(".", ",", 1)) if wind is not None else None,
        "qualityNote": "NASA EMIT incelenmiş bulut kompleksi (L2B CH4PLM).",
        "sector": "unknown",
        "sectorCode": None,
        "sourceUrl": f"https://cmr.earthdata.nasa.gov/search/concepts/{granule_concept_id}.html",
        "processingVersion": f"{SHORT_NAME} v{version}" + (f" · GHG {build}" if build else ""),
        "granule": umm.get("GranuleUR"),
        "sceneNames": p.get("DAAC Scene Names") or [],
        "imageUrl": urls.get("png"),
        "validObservationConfirmed": False,
    }


def ingest():
    """CMR'deki tüm Türkiye kutusu granüllerini idempotent alır: yeni granüllerin metadata'sı indirilir."""
    from . import ingest as tropomi_ingest  # Earthdata girişi orada
    try:
        concept_id, version = latest_collection()
        granules = search_granules(concept_id)
    except net.ProviderError as e:
        log.warning("EMIT kataloğu alınamadı: %s", e.kind)
        sources.failed("emit", e)
        return
    with db.read() as con:
        done = {r[0] for r in con.execute("SELECT json_extract(obs, '$.granule') FROM emit_plumes")}
    # İlk doldurma entegrasyonun başlangıcıdır; bu kayıtlar "yeni alındı" bildirimi üretmez
    baseline = int(not done)
    todo = [g for g in granules if g["umm"].get("GranuleUR") not in done]
    if todo and not tropomi_ingest.login():
        sources.failed("emit", net.ProviderError("auth", "Earthdata kimlik bilgisi yok veya reddedildi"))
        return
    session = None
    if todo:
        import earthaccess
        session = earthaccess.get_requests_https_session()
    now = store.iso(store.now())
    added, invalid = 0, 0
    for g in todo:
        umm = g["umm"]
        urls = _urls(umm)
        if "meta" not in urls:
            invalid += 1
            continue
        try:
            r = session.get(urls["meta"], timeout=60)
            if r.status_code in (401, 403):
                raise net.ProviderError("auth", f"HTTP {r.status_code} LP DAAC")
            r.raise_for_status()
            fc = r.json()
        except net.ProviderError as e:
            sources.failed("emit", e)
            return
        except Exception as e:
            log.warning("EMIT metadata alınamadı %s: %s", umm.get("GranuleUR"), type(e).__name__)
            invalid += 1
            continue
        rows = []
        for feat in fc.get("features", []):
            o = normalize(feat, umm, concept_id, g["meta"]["concept-id"], version, urls, now)
            if not o:
                invalid += 1
                continue
            geom = feat.get("geometry")
            rows.append((o["id"], o["observedAt"] or "", o["lat"], o["lon"], int(in_turkey([o["lon"]], [o["lat"]])[0]),
                         json.dumps(geom) if geom and geom.get("type") in ("Polygon", "MultiPolygon") else None,
                         json.dumps(o, ensure_ascii=False), now))
        with db.tx() as con:
            con.executemany("""INSERT OR REPLACE INTO emit_plumes(id, observed, lat, lon, tr, footprint, obs, fetched_at)
                               VALUES(?,?,?,?,?,?,?,?)""", rows)
            con.executemany("INSERT OR IGNORE INTO plume_seen(id, first_seen, baseline) VALUES(?,?,?)",
                            [(r[0], now, baseline) for r in rows])
        added += len(rows)
    with db.read() as con:
        total, last_obs = con.execute("SELECT COUNT(*), MAX(observed) FROM emit_plumes WHERE tr = 1").fetchone()
    sources.succeeded("emit", records=total, added=added, invalid=invalid, collection=f"{SHORT_NAME} v{version}",
                      last_observation=last_obs, granules=len(granules))
    log.info("EMIT: %d granül, %d yeni bulut, %d geçersiz", len(granules), added, invalid)
