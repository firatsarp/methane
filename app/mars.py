"""UNEP IMEO MARS yayımlanmış metan bulutları (Eye on Methane, resmî indirme sayfasındaki CSV paketi).

Belgeler: https://methanedata.unep.org/download-dataset  (paket içindeki definitions_detected_plumes_sources.pdf)
- tile_date: ISO 8601, veri sözlüğüne göre UTC (dosyada ofset yok; UTC bilgisi sözlükten gelir ve kayda yazılır).
- ch4_fluxrate kg/sa; ch4_fluxrate_std: standart sapma (1-sigma olarak işaretlenir).
- Sentinel-5P kaynak koordinatları yaklaşık ve otomatik üretilmiştir.
- Lisans: CC BY-NC-SA 4.0; ticari olmayan kullanım, UNEP IMEO atfı ve değişiklik belirtimi.
Anahtar gerekmez. Aynı çekim başka portalda da yayımlanmışsa (ör. EMIT) bağımsız tespit sayılmaz (catalog.py).
"""
import csv
import io
import json
import logging
import zipfile

from . import acquisition, db, net, quantities, retention, sources, store
from .geo import in_turkey

log = logging.getLogger("mars")

DOWNLOAD_PAGE = "https://methanedata.unep.org/download-dataset"
PLUMES_ZIP = "https://unepazeconomyadlsstorage.blob.core.windows.net/public/unep_methanedata_detected_plumes_csv.zip"
PLUMES_CSV = "unep_methanedata_detected_plumes.csv"
PROVIDER = "UNEP IMEO MARS"
LICENSE = "CC BY-NC-SA 4.0 — UNEP International Methane Emissions Observatory (MARS)"
METHOD = "mars-csv-v1"
# Yaklaşık kaynak koordinatı: TROPOMI pikseli ~5,5 × 7 km. Operasyon varsayılanıdır.
S5P_LOCATION_UNCERTAINTY_M = 7000
SENSORS = {"EMIT - NASA": ("NASA EMIT (ISS)", "emi"), "EnMAP - DLR": ("EnMAP HSI", "enmap"),
           "Sentinel-5P/TROPOMI - ESA": ("Sentinel-5P TROPOMI", "s5p"), "PRISMA - ASI": ("PRISMA", "prisma"),
           "Sentinel-2 - ESA": ("Sentinel-2 MSI", "s2"), "Landsat - NASA/USGS": ("Landsat 8/9 OLI", "landsat")}


def _num(v):
    from .observations import parse_number
    return parse_number(v)


def _utc(v):
    """Sözlük UTC dediği için ofsetsiz zamana Z eklenir; biçim bozuksa None."""
    from .observations import parse_time
    s = (v or "").strip()
    if not s:
        return None
    return parse_time(s if s.endswith("Z") or "+" in s[10:] else s + "Z")


def normalize(r, ingested_at=None):
    pid = (r.get("id_plume") or "").strip()
    lat, lon = _num(r.get("lat")), _num(r.get("lon"))
    if not pid or lat is None or lon is None:
        return None
    sensor, instrument = SENSORS.get((r.get("satellite") or "").strip(), ((r.get("satellite") or "").strip() or None, None))
    rate, std = _num(r.get("ch4_fluxrate")), _num(r.get("ch4_fluxrate_std"))
    rate = rate if rate is not None and rate >= 0 else None
    tile = (r.get("tile") or "").strip() or None
    det, quant = (r.get("detection_institution") or "").strip(), (r.get("quantification_institution") or "").strip()
    wind_u, wind_v = _num(r.get("wind_u")), _num(r.get("wind_v"))
    source_name = (r.get("source_name") or "").strip() or None
    return {
        "id": f"mars:{pid}",
        "providerId": pid,
        "status": "detected",
        "scope": "plume",
        "gas": "CH4",
        "provider": PROVIDER,
        "sensor": sensor,
        "instrument": instrument,
        "platform": (r.get("satellite") or "").strip() or None,
        "observedAt": _utc(r.get("tile_date")),
        "timeZoneSource": "MARS veri sözlüğü: tile_date UTC",
        "publishedAt": None,
        "modifiedAt": _utc(r.get("last_update")),
        "ingestedAt": ingested_at,
        "lat": lat, "lon": lon,
        "locationLabel": f"{lat:.3f}° K, {lon:.3f}° D".replace(".", ","),
        "locationUncertaintyM": S5P_LOCATION_UNCERTAINTY_M if instrument == "s5p" else None,
        "locationNote": "Sentinel-5P kaynak koordinatı yaklaşık ve otomatik üretilmiştir (MARS veri sözlüğü)."
        if instrument == "s5p" else None,
        "sourceAttribution": "unknown",
        "sourceId": f"mars:{source_name}" if source_name else None,
        "providerSource": source_name,
        "providerSector": (r.get("sector") or "").strip() or None,
        "sector": "unknown", "sectorCode": None,
        "emission": {"value": rate, "unit": "kg/h",
                     "uncertainty": {"value": std, "label": "standart sapma (MARS veri sözlüğü)"} if std is not None else None}
        if rate is not None else None,
        "measurement": quantities.measurement(quantities.EMISSION_RATE, rate, "kg/h", uncertainty=std,
                                              uncertainty_kind=quantities.SIGMA1) if rate is not None else None,
        "methodVersion": f"mars:{quant or det or 'unknown'}",
        "detectionInstitution": det or None,
        "quantificationInstitution": quant or None,
        "tile": tile,
        "backgroundTile": (r.get("tile_background") or "").strip() or None,
        "acquisitionId": acquisition.from_tile(tile),
        "providerWind": {"u": wind_u, "v": wind_v, "speed": _num(r.get("wind_speed")), "unit": "m/s",
                         "source": "MARS kayıt alanı (wind_u/wind_v); model kaynağı kayıtta belirtilmemiş"}
        if wind_u is not None and wind_v is not None else None,
        "sourceUrl": DOWNLOAD_PAGE,
        "accessKind": "MARS plumes CSV (Eye on Methane)",
        "license": LICENSE,
        "qualityNote": f"MARS eyleme dönüklük: {(r.get('actionable') or '').strip() or 'belirtilmemiş'}.",
        "validObservationConfirmed": False,
    }


def parse(zip_bytes, ingested_at=None):
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        text = z.read(PLUMES_CSV).decode("utf-8")
    out, invalid = [], 0
    for r in csv.DictReader(io.StringIO(text)):
        lat, lon = _num(r.get("lat")), _num(r.get("lon"))
        # Ülke adı alanına değil, Türkiye geometrisine göre seçilir (kutu ön elemesi hız içindir)
        if lat is None or lon is None or not (35.5 <= lat <= 42.5 and 25.5 <= lon <= 45.0):
            continue
        o = normalize(r, ingested_at)
        if o is None:
            invalid += 1
            continue
        out.append(o)
    return out, invalid


def fetch():
    """(zip baytları ya da None [değişmemiş], yeni etag). Koşullu istekle günlük gereksiz 11 MB indirme önlenir."""
    etag = db.get_meta().get("mars_etag")
    headers = {"If-None-Match": etag} if etag else {}
    try:
        payload, resp_headers = net.request(PLUMES_ZIP, headers=headers, raw=True, timeout=180, with_headers=True)
    except net.ProviderError as e:
        if e.status == 304:
            return None, etag
        raise
    return payload, resp_headers.get("ETag")


def ingest(protected=frozenset(), now=None):
    """Bellekte tam (Türkiye kutusu) listeyi döndürür; tabloya yalnızca 90 gün penceresi + korunanlar yazılır.
    Dosya değişmediyse None döner (sınıflandırma bu kaynak için tekrarlanmaz)."""
    try:
        payload, etag = fetch()
    except net.ProviderError as e:
        log.warning("MARS indirilemedi: %s", e.kind)
        sources.failed("mars", e)
        return None
    if payload is None:
        sources.checked("mars", state="ok", unchanged=True)
        return None
    t = store.iso(now or store.now())
    items, invalid = parse(payload, t)
    for o in items:
        o["tr"] = bool(in_turkey([o["lon"]], [o["lat"]])[0])
    keep = [o for o in items if retention.keep_on_ingest(o, protected, now)]
    with db.tx() as con:
        con.execute("DELETE FROM mars_plumes")
        con.executemany("INSERT OR REPLACE INTO mars_plumes(id, observed, lat, lon, tr, obs, fetched_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        [(o["id"], o["observedAt"] or "", o["lat"], o["lon"], int(o["tr"]),
                          json.dumps({k: v for k, v in o.items() if k != "tr"}, ensure_ascii=False), t) for o in keep])
    # ETag yalnızca tablo yazıldıktan sonra kaydedilir; yarıda kalan çalışma bir sonraki turda tekrarlanır
    if etag:
        db.set_meta("mars_etag", etag)
    tr_items = [o for o in items if o["tr"]]
    sources.succeeded("mars", records=len(tr_items), stored=sum(1 for o in keep if o["tr"]), invalid=invalid,
                      last_observation=max((o["observedAt"] for o in tr_items if o["observedAt"]), default=None),
                      license=LICENSE, last_product_at=t)
    return items


def persist_protected(items, protected, now=None):
    """Aynı turda kalıcı arşive giren eski kayıtları tabloya ekler (sonraki turun seri hesabı eksik kalmasın)."""
    t = store.iso(now or store.now())
    keep = [o for o in items or [] if o["id"] in protected]
    with db.tx() as con:
        con.executemany("INSERT OR IGNORE INTO mars_plumes(id, observed, lat, lon, tr, obs, fetched_at) VALUES(?,?,?,?,?,?,?)",
                        [(o["id"], o["observedAt"] or "", o["lat"], o["lon"], int(o["tr"]),
                          json.dumps({k: v for k, v in o.items() if k != "tr"}, ensure_ascii=False), t) for o in keep])
    return len(keep)
