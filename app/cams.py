"""CAMS Methane Hotspot Explorer tespitleri (Sentinel-5P TROPOMI tabanlı, bölgesel ölçek).

Belge: https://atmosphere.copernicus.eu/ghg-services/cams-methane-hotspot-explorer — sayfadaki "Access to data"
bağlantısı CSV'yi gösterir; yükleme sıklığı sayfada "haftalık" olarak yazılıdır. Sütunlar (02.11.2026 canlı dosya):
id,date,time_UTC,lat,lon,source_rate_t/h,uncertainty_t/h,source_type,source_country
- Değer t/sa -> kg/sa (boyutsal dönüşüm). Belirsizliğin istatistiksel tanımı sayfada yok: sigma sayılmaz.
- Koordinat TROPOMI piksel ölçeğinde yaklaşıktır; tesis konumu değildir.
- Kimlikteki ilk alan yörünge numarasıdır; aynı yörüngeden MARS S5P kaydı bağımsız tespit sayılmaz.
- Sayfada açık lisans metni bulunamadı; lisans "doğrulanmadı" olarak raporlanır.
"""
import csv
import io
import json
import logging

from . import acquisition, db, net, observations, quantities, retention, sources, store
from .geo import in_turkey

log = logging.getLogger("cams")

PAGE = "https://atmosphere.copernicus.eu/ghg-services/cams-methane-hotspot-explorer"
CSV_URL = "https://sites.ecmwf.int/data/cams/products/methane_emissions/data/data.csv"
PROVIDER = "CAMS (Copernicus)"
LOCATION_UNCERTAINTY_M = 7000
LICENSE = "Copernicus/CAMS; dosya sayfasında açık lisans metni doğrulanmadı"


def normalize(r, ingested_at=None):
    rid = (r.get("id") or "").strip()
    lat, lon = observations.parse_number(r.get("lat")), observations.parse_number(r.get("lon"))
    d, t = (r.get("date") or "").strip(), (r.get("time_UTC") or "").strip()
    if not rid or lat is None or lon is None:
        return None
    observed = observations.parse_time(f"{d[:4]}-{d[4:6]}-{d[6:8]}T{t}Z") if len(d) == 8 and t else None
    rate_t, unc_t = observations.parse_rate(r.get("source_rate_t/h")), observations.parse_rate(r.get("uncertainty_t/h"))
    m = quantities.measurement(quantities.EMISSION_RATE, rate_t, "t/h", uncertainty=unc_t,
                               uncertainty_kind=quantities.UNSPECIFIED, scope="regional_hotspot") \
        if rate_t is not None else None
    return {
        "id": f"cams:{rid}", "providerId": rid, "status": "detected", "scope": "regional_hotspot", "gas": "CH4",
        "provider": PROVIDER, "sensor": "Sentinel-5P TROPOMI", "instrument": "s5p", "platform": "Sentinel-5P",
        "observedAt": observed, "publishedAt": None, "ingestedAt": ingested_at,
        "lat": lat, "lon": lon, "locationLabel": f"{lat:.2f}° K, {lon:.2f}° D".replace(".", ","),
        "locationUncertaintyM": LOCATION_UNCERTAINTY_M,
        "locationNote": "TROPOMI piksel ölçeğinde yaklaşık konum; tesis konumu değildir.",
        "sourceAttribution": "unknown", "sourceId": None, "sector": "unknown", "sectorCode": None,
        "providerSector": (r.get("source_type") or "").strip() or None,
        "emission": {"value": m["value"], "unit": "kg/h",
                     "uncertainty": {"value": m["uncertainty"]["value"], "label": None} if m["uncertainty"] else None,
                     "originalUnit": "t/h"} if m else None,
        "measurement": m,
        "methodVersion": "cams:hotspot",
        "acquisitionId": acquisition.from_tile(rid),
        "sourceUrl": PAGE, "accessKind": "CAMS hotspot CSV", "license": LICENSE,
        "qualityNote": "Bölgesel tarama tespiti; yüksek çözünürlüklü bulut gözlemi değildir.",
        "validObservationConfirmed": False,
    }


def ingest(protected=frozenset(), now=None):
    try:
        text = net.request(CSV_URL, raw=True, timeout=90).decode("utf-8")
    except net.ProviderError as e:
        sources.failed("cams_hotspots", e)
        return None
    t = store.iso(now or store.now())
    items, invalid = [], 0
    for r in csv.DictReader(io.StringIO(text)):
        o = normalize(r, t)
        if o is None:
            invalid += 1
            continue
        if not (35.5 <= o["lat"] <= 42.5 and 25.5 <= o["lon"] <= 45.0):
            continue
        o["tr"] = bool(in_turkey([o["lon"]], [o["lat"]])[0])
        items.append(o)
    keep = [o for o in items if retention.keep_on_ingest(o, protected, now)]
    with db.tx() as con:
        con.execute("DELETE FROM cams_hotspots")
        con.executemany("INSERT OR REPLACE INTO cams_hotspots(id, observed, lat, lon, tr, obs, fetched_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        [(o["id"], o["observedAt"] or "", o["lat"], o["lon"], int(o["tr"]),
                          json.dumps({k: v for k, v in o.items() if k != "tr"}, ensure_ascii=False), t) for o in keep])
    tr_items = [o for o in items if o["tr"]]
    sources.succeeded("cams_hotspots", records=len(tr_items), stored=sum(1 for o in keep if o["tr"]), invalid=invalid,
                      last_observation=max((o["observedAt"] for o in tr_items if o["observedAt"]), default=None),
                      last_product_at=t)
    return items


def persist_protected(items, protected, now=None):
    """Aynı turda kalıcı arşive giren eski kayıtları tabloya ekler (sonraki turun seri hesabı eksik kalmasın)."""
    t = store.iso(now or store.now())
    keep = [o for o in items or [] if o["id"] in protected]
    with db.tx() as con:
        con.executemany("INSERT OR IGNORE INTO cams_hotspots(id, observed, lat, lon, tr, obs, fetched_at) VALUES(?,?,?,?,?,?,?)",
                        [(o["id"], o["observedAt"] or "", o["lat"], o["lon"], int(o["tr"]),
                          json.dumps({k: v for k, v in o.items() if k != "tr"}, ensure_ascii=False), t) for o in keep])
    return len(keep)
