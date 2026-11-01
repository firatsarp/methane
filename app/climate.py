"""İklim bağlamı: seçilen ERA5 ızgara hücresi için 1991–2020 aylık referans ve tamamlanmış bir ayın karşılaştırması.

Veri Open-Meteo Historical API (models=era5) üzerinden yıl yıl alınır; her yılın aylık özeti kalıcı önbelleğe yazılır.
İndirme arka plan işidir; HTTP isteği yalnızca önbellekteki sonucu ve işin ilerlemesini döndürür.
Gün sınırları UTC'dir. Eksik saatler sıfırla doldurulmaz; eksik ay hesaba katılmaz.
"""
import logging
from datetime import timedelta

from . import meteo, net, sources, store
from .weather import ARCHIVE_URL, ATTRIBUTION, OPEN_METEO_ARCHIVE_DOCS

log = logging.getLogger("climate")

GRID = 0.25
REF_YEARS = list(range(meteo.REFERENCE_START, meteo.REFERENCE_END + 1))
# Her zamanlayıcı turunda en fazla bu kadar yıl indirilir (Open-Meteo ücretsiz kota sınırı gözetilir)
YEARS_PER_RUN = 4
# Günde en fazla bu kadar yeni ızgara hücresi kuyruğa alınır; her ziyaretçi için 30 yıllık iş başlatılmaz
MAX_NEW_CELLS_PER_DAY = 12
CURRENT_YEAR_TTL = timedelta(days=1)


def cell_of(lat, lon):
    return round(round(lat / GRID) * GRID, 2), round(round(lon / GRID) * GRID, 2)


def _year_key(cell, year):
    return f"clim:{meteo.CLIMATE_METHOD_VERSION}:{cell[0]}:{cell[1]}:{year}"


def _job_id(cell):
    return f"climate:{cell[0]}:{cell[1]}"


def _fetch_year(cell, year):
    today = store.now().date()
    end = min(f"{year}-12-31", (today - timedelta(days=1)).isoformat())
    data = net.get_json(ARCHIVE_URL, {
        "latitude": cell[0], "longitude": cell[1], "start_date": f"{year}-01-01", "end_date": end,
        "models": "era5", "hourly": "temperature_2m,precipitation,wind_speed_10m",
        "wind_speed_unit": "ms", "timezone": "UTC",
    }, timeout=60)
    h, units = data["hourly"], data.get("hourly_units", {})
    if units.get("temperature_2m") != "°C" or units.get("precipitation") != "mm" or units.get("wind_speed_10m") != "m/s":
        raise net.ProviderError("bad_response", f"Beklenmeyen birimler: {units}")
    monthly = meteo.monthly_aggregates(h["time"], h["temperature_2m"], h["precipitation"], h["wind_speed_10m"])
    return {
        "year": year,
        "grid": {"lat": data.get("latitude"), "lon": data.get("longitude"), "elevationM": data.get("elevation")},
        "months": {f"{m:02d}": v for (y, m), v in monthly.items() if y == year},
    }


def _years_present(cell, years):
    out = {}
    for y in years:
        hit = store.cache_get(_year_key(cell, y))
        if hit:
            out[y] = hit[0]
    return out


def _to_monthly(year_blocks):
    return {(b["year"], int(m)): v for b in year_blocks.values() for m, v in b["months"].items()}


def enqueue(lat, lon, reason="request"):
    cell = cell_of(lat, lon)
    since = store.iso(store.now() - timedelta(days=1))
    from . import db
    with db.read() as con:
        recent = con.execute("SELECT COUNT(*) FROM jobs WHERE kind='climate' AND created_at >= ?", (since,)).fetchone()[0]
    if store.job_get(_job_id(cell)) is None and recent >= MAX_NEW_CELLS_PER_DAY:
        return cell, False
    return cell, store.job_enqueue(_job_id(cell), "climate", {"lat": cell[0], "lon": cell[1], "reason": reason})


def process_jobs():
    """Sıradaki iklim işinin eksik yıllarından birkaçını indirir; ilerleme iş kaydına yazılır."""
    jobs = store.jobs_by_state("climate", ("running", "queued"), limit=1)
    if not jobs:
        return
    job = jobs[0]
    cell = (job["payload"]["lat"], job["payload"]["lon"])
    this_year = store.now().year
    wanted = REF_YEARS + [this_year - 1, this_year]
    have = _years_present(cell, wanted)
    todo = [y for y in wanted if y not in have][:YEARS_PER_RUN]
    store.job_update(job["id"], state="running")
    for y in todo:
        try:
            block = _fetch_year(cell, y)
        except net.ProviderError as e:
            log.warning("İklim verisi alınamadı %s %s: %s", cell, y, e.kind)
            sources.failed("openmeteo_era5", e)
            store.job_update(job["id"], state="queued" if e.kind in ("quota", "network", "server") else "failed",
                             error_class=e.kind, error=str(e)[:200], attempts=job["attempts"] + 1)
            return
        # Geçmiş yıllar değişmez; içinde bulunulan yıl günlük yenilenir
        store.cache_put(_year_key(cell, y), block, CURRENT_YEAR_TTL if y == this_year else None)
        sources.succeeded("openmeteo_era5")
    done = len(_years_present(cell, wanted))
    store.job_update(job["id"], progress=round(done / len(wanted), 3),
                     state="done" if done == len(wanted) else "running", error_class=None, error=None)


def climate(lat, lon, compare=None):
    cell = cell_of(lat, lon)
    this_year = store.now().year
    have = _years_present(cell, REF_YEARS + [this_year - 1, this_year])
    ref_have = {y: b for y, b in have.items() if y in REF_YEARS}
    job = store.job_get(_job_id(cell))
    base = {
        "requestedLocation": {"lat": lat, "lon": lon},
        "cell": {"lat": cell[0], "lon": cell[1]},
        "referenceStart": f"{meteo.REFERENCE_START}-01-01",
        "referenceEnd": f"{meteo.REFERENCE_END}-12-31",
        "calendar": "Gregoryen (artık yıllar dahil)",
        "dayBoundary": "UTC 00:00",
        "dataset": "ERA5",
        "model": "era5",
        "accessPath": "ERA5 — Open-Meteo üzerinden",
        "methodVersion": meteo.CLIMATE_METHOD_VERSION,
        "method": {
            "temperature": "Her yılın ilgili ayındaki saatlik değerlerin ortalaması; ardından yılların ortalaması.",
            "precipitation": "Her yılın ilgili ayındaki saatlik yağışların toplamı; ardından yılların ortalaması.",
            "wind": "10 m saatlik skaler rüzgâr hızlarının ortalaması; ardından yılların ortalaması.",
            "coverage": "Yalnızca bütün saatleri geçerli olan aylar kullanılır; eksik saat sıfır sayılmaz.",
        },
        "sourceUrl": OPEN_METEO_ARCHIVE_DOCS,
        "attribution": ATTRIBUTION,
        "progress": {"yearsFetched": len(ref_have), "yearsNeeded": len(REF_YEARS)},
        "job": store.job_public(job),
    }
    if len(ref_have) < len(REF_YEARS):
        if not job or job["state"] not in store.ACTIVE:
            _, accepted = enqueue(lat, lon)
            job = store.job_get(_job_id(cell))
            base["job"] = store.job_public(job)
            if not accepted and not job:
                return {**base, "status": "unavailable",
                        "message": "Günlük yeni konum sınırına ulaşıldı; iş yarın kuyruğa alınabilir."}
        state = (job or {}).get("state")
        status = "unavailable" if state in ("failed", "blocked") else "queued"
        return {**base, "status": status}

    monthly = _to_monthly(have)
    reference = meteo.climate_reference(monthly)
    grid = next(iter(ref_have.values()))["grid"]
    target = None
    if compare:
        try:
            y, m = (int(x) for x in compare.split("-"))
            target = (y, m)
        except ValueError:
            target = None
    if target is None:
        target = meteo.latest_complete_month(monthly)
    comparison = meteo.compare_month(monthly, reference, *target) if target else None
    available = sorted(f"{y}-{m:02d}" for (y, m), v in monthly.items()
                       if y > meteo.REFERENCE_END and all(v[k] is not None for k in ("temp", "precip", "wind")))
    return {
        **base,
        "status": "ready",
        "resolvedGridPoint": grid,
        "months": reference,
        "validYearCount": min(min(r[f"{k}Years"] for k in ("temp", "precip", "wind")) for r in reference),
        "units": {"temp": "°C", "precip": "mm", "wind": "m/s"},
        "comparisonMonth": comparison,
        "comparisonRequested": f"{target[0]}-{target[1]:02d}" if target else None,
        "comparisonAvailable": available,
    }


def seed_default(lat, lon):
    enqueue(lat, lon, reason="seed")
