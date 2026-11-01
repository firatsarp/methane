"""Sağlayıcı kayıtlarını metan-icerik.json içindeki observationSchema sözleşmesine çevirir.

Kurallar (bilimsel içerik paketinden):
- Sayı gibi görünen metinler, "N/A" ve null doğrulanır; eksik değer sıfır emisyona dönüşmez.
- Zaman dilimi olmayan zaman geçerli sayılmaz.
- Katalogda bulut kaydı olmaması negatif gözlem üretmez; bu modül yalnızca var olan kayıtları çevirir.
- Aynı gözlem birden fazla kayıtla gelirse tek gözlem sayılır.
"""
import math
import re
from datetime import datetime

# Carbon Mapper alan adları: https://api.carbonmapper.org/api/v1/docs (01.11.2026 tarihli canlı yanıtla doğrulandı)
CM_PROVIDER = "Carbon Mapper"
CM_SENSORS = {"tan": "Planet Tanager-1", "emi": "NASA EMIT (ISS)"}
# IPCC sektör kodu 6A katı atık; içerik paketindeki sector alanı yalnızca bu ayrımı yapıyor
SOLID_WASTE_SECTORS = {"6A"}

_NUMBER_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
_TZ_RE = re.compile(r"(Z|[+-]\d{2}:\d{2})$", re.IGNORECASE)


def parse_number(value):
    """Sonlu sayı ya da None. bool, boş metin, "N/A", NaN ve sayı olmayan metinler None olur."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        s = value.strip()
        if not _NUMBER_RE.match(s):
            return None
        v = float(s)
        return v if math.isfinite(v) else None
    return None


def parse_rate(value):
    """Emisyon hızı ve belirsizlik negatif olamaz."""
    v = parse_number(value)
    return v if v is not None and v >= 0 else None


def parse_time(value):
    """ISO 8601 ve zaman dilimi içeren zamanı normalleştirir; aksi halde None."""
    if not isinstance(value, str) or not _TZ_RE.search(value.strip()):
        return None
    s = value.strip()
    try:
        dt = datetime.fromisoformat(s[:-1] + "+00:00" if s[-1] in "zZ" else s)
    except ValueError:
        return None
    return dt.isoformat().replace("+00:00", "Z")


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _sector(code):
    if not _text(code):
        return "unknown"
    return "solid_waste" if code in SOLID_WASTE_SECTORS else "other"


def _wind_note(p):
    speed = parse_number(p.get("wind_speed_avg_auto"))
    if speed is None or speed < 0:
        return None
    direction = parse_number(p.get("wind_direction_avg_auto"))
    dir_txt = f", yön {direction:.0f}°" if direction is not None else ""
    speed_txt = f"{speed:.1f}".replace(".", ",")
    return (f"Sağlayıcının gözlem için otomatik ortalama rüzgâr tahmini: {speed_txt} m/s{dir_txt}. "
            "Güncel hava durumu değildir.")


def _quality_note(p):
    notes = []
    if _text(p.get("plume_quality")):
        notes.append(f"Sağlayıcı kalite alanı: {p['plume_quality'].strip()}.")
    phase = _text(p.get("mission_phase"))
    if phase and phase != "production":
        notes.append(f"Sağlayıcı görev evresi: {phase}.")
    return " ".join(notes) or None


def cm_portal_url(plume_id, lat, lon):
    from urllib.parse import quote
    return f"https://data.carbonmapper.org/?plume_id={quote(plume_id)}#12/{lat:.4f}/{lon:.4f}"


def normalize_cm_plume(p, ingested_at=None):
    """Carbon Mapper bulut kaydını observationSchema biçimine çevirir. Geçersiz kayıtta None."""
    pid = _text(p.get("plume_id"))
    coords = (p.get("geometry_json") or {}).get("coordinates") or []
    lon = parse_number(coords[0]) if len(coords) > 0 else None
    lat = parse_number(coords[1]) if len(coords) > 1 else None
    if not pid or lon is None or lat is None:
        return None

    # Carbon Mapper tahmini gizlediyse (hide_emission) değer yokmuş gibi davranılır, 0 yazılmaz
    value = None if p.get("hide_emission") is True else parse_rate(p.get("emission_auto"))
    emission = None
    if value is not None:
        unc = parse_rate(p.get("emission_uncertainty_auto"))
        emission = {
            "value": value,
            "unit": "kg/h",
            # API yalnızca simetrik tek değer veriyor; istatistiksel tanımı yanıt içinde yok, uydurulmaz
            "uncertainty": {"value": unc, "label": None} if unc is not None else None,
        }

    instrument = _text(p.get("instrument"))
    version = " · ".join(x for x in (
        _text(p.get("collection")),
        f"emisyon {p['emission_version']}" if _text(p.get("emission_version")) else None,
        f"yazılım {p['processing_software']}" if _text(p.get("processing_software")) else None,
    ) if x)
    return {
        "id": pid,
        "providerId": pid,
        "status": "detected",
        "scope": "plume",
        "gas": _text(p.get("gas")) or "CH4",
        "provider": CM_PROVIDER,
        "sensor": CM_SENSORS.get(instrument) or _text(p.get("platform")),
        "instrument": instrument,
        "platform": _text(p.get("platform")),
        "processingVersion": version or None,
        "modifiedAt": parse_time(p.get("modified")),
        "gsdM": parse_number(p.get("gsd")),
        "sensitivityMode": _text(p.get("sensitivity_mode")),
        "sceneId": _text(p.get("scene_id")),
        "observedAt": parse_time(p.get("scene_timestamp")),
        "publishedAt": parse_time(p.get("published_at")),
        "ingestedAt": parse_time(ingested_at),
        "locationLabel": f"{lat:.3f}° K, {lon:.3f}° D".replace(".", ","),
        "lat": lat,
        "lon": lon,
        # Katalog kaydı tesis doğrulaması içermiyor
        "sourceAttribution": "unknown",
        "emission": emission,
        "sourceUrl": cm_portal_url(pid, lat, lon),
        "qualityNote": _quality_note(p),
        "windNote": _wind_note(p),
        "sector": _sector(p.get("sector")),
        "sectorCode": _text(p.get("sector")),
        "validObservationConfirmed": False,
    }


def normalize_cm_source(feature):
    """Carbon Mapper kaynak özeti (sources.geojson). Tek bulut değildir; scope=source_summary.

    Sağlayıcı alanları (01.11.2026 canlı yanıtla doğrulandı): avg_emission_auto tespit edilen bulutların
    ortalaması; emission_auto = avg_emission_auto × persistence (tespit tarihi / gözlem tarihi)."""
    p = feature.get("properties") or {}
    coords = (feature.get("geometry") or {}).get("coordinates") or []
    lon = parse_number(coords[0]) if len(coords) > 0 else None
    lat = parse_number(coords[1]) if len(coords) > 1 else None
    name = _text(p.get("source_name"))
    if not name or lon is None or lat is None:
        return None
    # Sağlayıcı source_name içine isteğin sorgu dizesini ekleyebiliyor; kimlik yalnızca ilk bölümdür
    sid = name.split("?", 1)[0]

    def rate(value, unc):
        v = parse_rate(value)
        if v is None:
            return None
        u = parse_rate(unc)
        return {"value": v, "unit": "kg/h", "uncertainty": {"value": u, "label": None} if u is not None else None}

    return {
        "id": f"cmsrc:{sid}",
        "providerId": sid,
        "status": "detected",
        "scope": "source_summary",
        "gas": _text(p.get("gas")) or "CH4",
        "provider": CM_PROVIDER,
        "lat": lat,
        "lon": lon,
        "locationLabel": f"{lat:.3f}° K, {lon:.3f}° D".replace(".", ","),
        "sector": _sector(p.get("sector")),
        "sectorCode": _text(p.get("sector")),
        "plumeIds": [x for x in (p.get("plume_ids") or []) if isinstance(x, str)],
        "plumeCount": parse_number(p.get("plume_count")),
        "detectionDateCount": parse_number(p.get("detection_date_count")),
        "observationDateCount": parse_number(p.get("observation_date_count")),
        "persistence": parse_number(p.get("persistence")),
        "detectedMean": rate(p.get("avg_emission_auto"), p.get("avg_emission_uncertainty_auto")),
        "persistenceWeighted": rate(p.get("emission_auto"), p.get("emission_uncertainty_auto")),
        "firstObserved": _text(p.get("timestamp_min")),
        "lastObserved": _text(p.get("timestamp_max")),
        "firstPublished": _text(p.get("published_at_min")),
        "lastPublished": _text(p.get("published_at_max")),
        "clusterRadiusM": _cluster_radius(sid),
        "sourceAttribution": "unknown",
        "sourceUrl": f"https://data.carbonmapper.org/#12/{lat:.4f}/{lon:.4f}",
    }


def _cluster_radius(sid):
    m = re.search(r"_(\d+)m_", sid)
    return int(m.group(1)) if m else None


def observation_key(o):
    """Aynı sensörün aynı sahnesindeki aynı konum tek gözlemdir (ör. aynı EMIT sahnesi iki kaynaktan gelirse)."""
    return (o.get("instrument") or o.get("sensor"), o.get("observedAt"), round(o["lat"], 4), round(o["lon"], 4))


def dedupe(observations):
    """Kayıt kimliği ve gözlem anahtarına göre tekilleştirir. (benzersiz liste, atılan sayısı) döner."""
    seen_ids, seen_keys, out = set(), set(), []
    for o in observations:
        key = observation_key(o)
        if o["id"] in seen_ids or key in seen_keys:
            continue
        seen_ids.add(o["id"])
        seen_keys.add(key)
        out.append(o)
    return out, len(observations) - len(out)
