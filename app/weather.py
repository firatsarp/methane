"""Meteoroloji: Open-Meteo güncel/tahmin ve gözlem anına ait ERA5 (Open-Meteo yolu).

Tahmin ve yeniden analiz ayrı veri aileleridir; eski bir gözleme güncel hava bağlanmaz.
CDS ERA5 yolu cds.py'de arka plan işi olarak çalışır; burada yalnızca sonucu varsa birlikte döndürülür.
"""
import logging
from datetime import timedelta

from . import cds, meteo, net, sources, store

log = logging.getLogger("weather")

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
OPEN_METEO_DOCS = "https://open-meteo.com/en/docs"
OPEN_METEO_ARCHIVE_DOCS = "https://open-meteo.com/en/docs/historical-weather-api"
ATTRIBUTION = "Weather data by Open-Meteo.com (CC BY 4.0)"

FORECAST_TTL = timedelta(hours=1)
FORECAST_HOURS = 72
# Önbellek anahtarı için konum yuvarlaması (~5 km); model ızgarası zaten daha kaba
FORECAST_ROUND = 0.05
# Yeniden analiz henüz yayımlanmamışsa bu kadar sonra yeniden denenir
NOT_PUBLISHED_RETRY = timedelta(hours=6)
OBS_METHOD = "om-era5-v1"

# Zorunlu değişkenler tek istekte; isteğe bağlılar ayrı alınır ki desteklenmeyen biri tüm isteği düşürmesin
FORECAST_CORE = ["temperature_2m", "precipitation", "wind_speed_10m", "wind_direction_10m"]
FORECAST_OPTIONAL = ["relative_humidity_2m", "cloud_cover", "surface_pressure"]
ARCHIVE_CORE = ["temperature_2m", "precipitation", "wind_speed_10m", "wind_direction_10m"]
ARCHIVE_OPTIONAL = ["relative_humidity_2m", "cloud_cover", "surface_pressure",
                    "wind_speed_100m", "wind_direction_100m", "boundary_layer_height"]

# Değişken tanımları: etiket, ölçüm yüksekliği (m), zaman aralığı tanımı
VARIABLES = {
    "temperature_2m": ("Sıcaklık", 2, "anlık"),
    "relative_humidity_2m": ("Bağıl nem", 2, "anlık"),
    "precipitation": ("Yağış", None, "önceki 1 saatin toplamı"),
    "cloud_cover": ("Bulutluluk", None, "anlık"),
    "surface_pressure": ("Yüzey basıncı", 0, "anlık"),
    "wind_speed_10m": ("Rüzgâr hızı", 10, "anlık"),
    "wind_direction_10m": ("Rüzgârın geldiği yön", 10, "anlık"),
    "wind_speed_100m": ("Rüzgâr hızı", 100, "anlık"),
    "wind_direction_100m": ("Rüzgârın geldiği yön", 100, "anlık"),
    "boundary_layer_height": ("Sınır tabakası yüksekliği", None, "anlık"),
}


def _round(v, step):
    return round(round(v / step) * step, 4)


def _fetch_parts(url, base, core, optional):
    """Zorunlu değişkenleri alır; isteğe bağlıları ayrı istekte dener. (birleşik yanıt, alınamayan değişkenler)."""
    data = net.get_json(url, {**base, "hourly": ",".join(core)}, timeout=25)
    missing = []
    try:
        extra = net.get_json(url, {**base, "hourly": ",".join(optional)}, timeout=25)
        data["hourly"].update({k: v for k, v in extra["hourly"].items() if k != "time"})
        data["hourly_units"].update(extra.get("hourly_units", {}))
    except net.ProviderError as e:
        if e.kind in ("quota", "network", "server"):
            raise
        # Model bazı değişkenleri desteklemiyorsa tek tek denenir
        for var in optional:
            try:
                one = net.get_json(url, {**base, "hourly": var}, timeout=25)
                data["hourly"][var] = one["hourly"][var]
                data["hourly_units"][var] = one["hourly_units"][var]
            except net.ProviderError as e2:
                if e2.kind in ("quota", "network", "server"):
                    raise
                missing.append(var)
    return data, missing


def _var_block(units, key, value):
    label, height, interval = VARIABLES[key]
    return {"value": value, "unit": units.get(key), "heightM": height, "interval": interval, "label": label}


def _wind(speed, direction):
    if speed is None:
        return None
    calm = speed < meteo.CALM_MS
    return {"speed": speed, "unit": "m/s", "from": None if calm or direction is None else direction,
            "to": None if calm or direction is None else meteo.to_direction(direction), "calm": calm}


# ---- Güncel / tahmin ---------------------------------------------------------------

def forecast(lat, lon):
    key_lat, key_lon = _round(lat, FORECAST_ROUND), _round(lon, FORECAST_ROUND)
    key = f"wx:fc:{key_lat}:{key_lon}"
    hit = store.cache_get(key)
    if hit:
        return {**hit[0], "status": "ready", "cache": "hit"}
    base = {"latitude": key_lat, "longitude": key_lon, "wind_speed_unit": "ms", "timezone": "UTC",
            "forecast_hours": FORECAST_HOURS}
    try:
        current = net.get_json(FORECAST_URL, {**base, "current": ",".join(FORECAST_CORE + FORECAST_OPTIONAL)}, timeout=25)
        hourly, missing = _fetch_parts(FORECAST_URL, base, FORECAST_CORE, FORECAST_OPTIONAL)
    except net.ProviderError as e:
        sources.failed("openmeteo_forecast", e)
        old = store.cache_get(key, allow_expired=True)
        if old:
            return {**old[0], "status": "stale", "error": e.public()}
        return {"status": "unavailable", "error": e.public(), "requestedLocation": {"lat": lat, "lon": lon}}
    fetched = store.iso(store.now())
    cu = current.get("current_units", {})
    cur = current.get("current", {})
    out = {
        "kind": "forecast",
        "provider": "Open-Meteo",
        "dataset": "Open-Meteo Forecast API",
        "model": "best_match",
        "modelNote": "Open-Meteo konuma göre en uygun ulusal/küresel modeli otomatik seçer (best_match). "
                     "Model tahminidir; yer istasyonu ölçümü değildir.",
        "requestedLocation": {"lat": lat, "lon": lon},
        "resolvedGridPoint": {"lat": hourly.get("latitude"), "lon": hourly.get("longitude"),
                              "elevationM": hourly.get("elevation")},
        "fetchedAt": fetched,
        "temporalResolution": "1 saat",
        "current": {
            "validAt": cur.get("time") and cur["time"] + "Z",
            "variables": {k: _var_block(cu, k, cur.get(k)) for k in FORECAST_CORE + FORECAST_OPTIONAL if k in cur},
            "wind10": _wind(cur.get("wind_speed_10m"), cur.get("wind_direction_10m")),
        },
        "hourly": {"time": [t + "Z" for t in hourly["hourly"]["time"]],
                   **{k: v for k, v in hourly["hourly"].items() if k != "time"}},
        "units": hourly.get("hourly_units", {}),
        "missingVariables": missing,
        "sourceUrl": OPEN_METEO_DOCS,
        "attribution": ATTRIBUTION,
    }
    store.cache_put(key, out, FORECAST_TTL)
    sources.succeeded("openmeteo_forecast")
    return {**out, "status": "ready", "cache": "miss"}


# ---- Gözlem anı (ERA5, Open-Meteo yolu) -------------------------------------------------

def _om_era5_for(obs):
    target = meteo.parse_utc(obs["observedAt"])
    day0, day1 = (target - timedelta(days=1)).date(), (target + timedelta(days=1)).date()
    base = {"latitude": obs["lat"], "longitude": obs["lon"], "start_date": day0.isoformat(),
            "end_date": day1.isoformat(), "models": "era5", "wind_speed_unit": "ms", "timezone": "UTC"}
    data, missing = _fetch_parts(ARCHIVE_URL, base, ARCHIVE_CORE, ARCHIVE_OPTIONAL)
    h, units = data["hourly"], data.get("hourly_units", {})
    times = h["time"]
    idx, offset = meteo.nearest_index(times, target)
    fetched = store.iso(store.now())
    block = {
        "requestedLocation": {"lat": obs["lat"], "lon": obs["lon"]},
        "resolvedGridPoint": {"lat": data.get("latitude"), "lon": data.get("longitude"),
                              "elevationM": data.get("elevation")},
        "requestedAt": obs["observedAt"],
        "fetchedAt": fetched,
        "provider": "Open-Meteo",
        "accessPath": "ERA5 — Open-Meteo üzerinden",
        "dataset": "ERA5",
        "model": "era5",
        "kind": "reanalysis",
        "temporalResolution": "1 saat",
        "spatialResolution": "0,25° (yaklaşık 25–31 km)",
        "sourceUrl": OPEN_METEO_ARCHIVE_DOCS,
        "attribution": ATTRIBUTION,
        "missingVariables": missing,
        "provenance": {"api": ARCHIVE_URL, "models": "era5", "method": OBS_METHOD},
    }
    if idx is None or h["temperature_2m"][idx] is None:
        recent = store.now() - target < timedelta(days=10)
        block.update(status="not_published" if recent else "empty", validAt=None, timeOffsetMinutes=offset)
        return block
    values = {k: h[k][idx] for k in h if k != "time"}
    # Grafikte gözlem saatini çevreleyen ±12 saat
    lo, hi = max(0, idx - 12), min(len(times), idx + 13)
    block.update(
        status="ready",
        validAt=times[idx] + "Z",
        timeOffsetMinutes=offset,
        variables={k: _var_block(units, k, v) for k, v in values.items() if k in VARIABLES},
        wind10=_wind(values.get("wind_speed_10m"), values.get("wind_direction_10m")),
        wind100=_wind(values.get("wind_speed_100m"), values.get("wind_direction_100m")),
        series={"time": [t + "Z" for t in times[lo:hi]], **{k: h[k][lo:hi] for k in h if k != "time"}},
        units=units,
    )
    return block


def observation_weather(obs):
    """Bir gözlemin kendi koordinatı ve observedAt zamanı için meteoroloji. CDS sonucu varsa birincil o olur."""
    if not obs.get("observedAt") or meteo.parse_utc(obs["observedAt"]) is None:
        return {"status": "empty", "message": "Gözlem zamanı doğrulanmadığı için meteoroloji eşleştirilmedi."}
    key = f"wx:obs:{obs['id']}:{OBS_METHOD}"
    hit = store.cache_get(key)
    om = hit[0] if hit else None
    if om is None:
        try:
            om = _om_era5_for(obs)
            ttl = None if om["status"] == "ready" else NOT_PUBLISHED_RETRY
            store.cache_put(key, om, ttl)
            sources.succeeded("openmeteo_era5")
        except net.ProviderError as e:
            sources.failed("openmeteo_era5", e)
            old = store.cache_get(key, allow_expired=True)
            om = {**old[0], "status": "stale"} if old else {"status": "unavailable", "error": e.public()}

    cds_block, cds_job = cds.observation_result(obs)
    blocks = [b for b in (cds_block, om) if b and b.get("status") == "ready"]
    primary = blocks[0] if blocks else None
    status = "ready" if primary else (om or {}).get("status", "unavailable")
    if status in ("not_published", "empty"):
        status = "empty"
    return {
        "status": status,
        "observation": {"id": obs["id"], "observedAt": obs["observedAt"], "lat": obs["lat"], "lon": obs["lon"]},
        "primary": primary,
        "alternatives": [b for b in (cds_block, om) if b and b is not primary],
        "cdsJob": store.job_public(cds_job),
        "note": "CDS ve Open-Meteo yolları aynı ERA5 veri ailesine erişir; iki bağımsız doğrulama sayılmaz.",
    }
