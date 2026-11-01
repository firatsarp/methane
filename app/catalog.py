"""Kanonik gözlem kataloğu: Carbon Mapper bulutları + NASA EMIT bulut kompleksleri + Carbon Mapper kaynak özetleri.

Aynı EMIT gözlemi iki sağlayıcıdan gelirse tek gözlem olur; erişim yolları ve her sağlayıcının kendi tahmini
ayrı alanlarda korunur. Kaynak özetleri tek bulutlara atanmaz; yalnızca sağlayıcının kümelemesiyle bağlanır.
"""
import json
import math
from datetime import datetime, timedelta, timezone

from . import db

# Aynı EMIT gözlemi sayılması için en fazla zaman ve konum farkı
SAME_SCENE_SECONDS = 120
SAME_PLACE_M = 1000


def _dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def distance_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _cm_item(row, source_of):
    pid, png, bounds, raw, tr = row
    o = json.loads(raw)
    o.update({
        "canonicalObservationId": o["id"],
        "tr": bool(tr),
        "geometry": {"type": "Point", "coordinates": [o["lon"], o["lat"]]},
        "accessPaths": [{"provider": "Carbon Mapper", "id": o.get("providerId") or o["id"], "url": o.get("sourceUrl"),
                         "kind": "Katalog kaydı (plumes/annotated)"}],
        # Sağlayıcının yayımladığı artış görüntüsü; sayısal lejant API yanıtında yok
        "raster": {"png": png, "bounds": json.loads(bounds) if bounds else None,
                   "label": "Carbon Mapper metan artış görüntüsü"} if png else None,
        "footprint": None,
        "sourceId": source_of.get(o["id"]),
        "alternateEstimates": [],
    })
    return o


def _emit_item(row):
    _, footprint, raw, tr = row
    o = json.loads(raw)
    o.update({
        "canonicalObservationId": o["id"],
        "tr": bool(tr),
        "geometry": {"type": "Point", "coordinates": [o["lon"], o["lat"]]},
        "accessPaths": [{"provider": o["provider"], "id": o.get("providerId"), "url": o.get("sourceUrl"),
                         "kind": f"Granül {o.get('granule')}"}],
        "raster": None,
        "footprint": json.loads(footprint) if footprint else None,
        "sourceId": None,
        "alternateEstimates": [],
    })
    return o


def merge_emit(cm_items, emit_items):
    """EMIT kayıtlarını Carbon Mapper'daki aynı EMIT gözlemiyle eşleştirir. Her CM kaydı en çok bir kez eşleşir."""
    used = set()
    standalone = []
    cm_emit = [c for c in cm_items if c.get("instrument") == "emi" and _dt(c.get("observedAt"))]
    for e in emit_items:
        te = _dt(e.get("observedAt"))
        best, best_d = None, None
        for c in cm_emit:
            if c["id"] in used or te is None:
                continue
            if abs((_dt(c["observedAt"]) - te).total_seconds()) > SAME_SCENE_SECONDS:
                continue
            d = distance_m(e["lat"], e["lon"], c["lat"], c["lon"])
            if d <= SAME_PLACE_M and (best_d is None or d < best_d):
                best, best_d = c, d
        if best is None:
            standalone.append(e)
            continue
        used.add(best["id"])
        best["accessPaths"].append({**e["accessPaths"][0], "matchDistanceM": round(best_d)})
        best["footprint"] = e["footprint"]
        best["alternateEstimates"].append({
            "provider": e["provider"], "id": e.get("providerId"), "emission": e.get("emission"),
            "maxEnhancement": e.get("maxEnhancement"), "windNote": e.get("windNote"),
            "processingVersion": e.get("processingVersion"),
        })
        best.setdefault("mergeNote", "Aynı EMIT gözlemi Carbon Mapper ve NASA LP DAAC üzerinden alındı; "
                                     "iki bağımsız sensör doğrulaması değildir.")
    return cm_items + standalone


def load(scope="tr"):
    with db.read() as con:
        srcs = [(json.loads(r[0]), r[1]) for r in con.execute("SELECT obs, tr FROM cm_sources")]
        cm_rows = con.execute("SELECT id, png, bounds, obs, tr FROM cm_plumes").fetchall()
        emit_rows = con.execute("SELECT id, footprint, obs, tr FROM emit_plumes").fetchall()
    source_of = {pid: s["id"] for s, _ in srcs for pid in s.get("plumeIds", [])}
    items = merge_emit([_cm_item(r, source_of) for r in cm_rows], [_emit_item(r) for r in emit_rows])
    if scope == "tr":
        items = [o for o in items if o["tr"]]
    sources = {s["id"]: {**s, "tr": bool(tr)} for s, tr in srcs if scope != "tr" or tr}
    return items, sources


def facets(items):
    sectors = sorted({o.get("sectorCode") or "unknown" for o in items})
    sensors = sorted({o.get("sensor") for o in items if o.get("sensor")})
    times = sorted(o["observedAt"] for o in items if o.get("observedAt"))
    return {"sectors": sectors, "sensors": sensors,
            "observedRange": [times[0], times[-1]] if times else None}


def apply_filters(items, sector=None, sensor=None, since=None, until=None, days=None, emission_only=False,
                  sort="observed", now=None):
    """Zaman filtresi gözlem zamanına uygulanır; zamanı doğrulanmamış kayıt zaman filtresine dahil edilmez."""
    now = now or datetime.now(timezone.utc)
    lo = now - timedelta(days=days) if days else _dt(since) if since else None
    hi = _dt(until) if until else None
    out = []
    for o in items:
        if sector and (o.get("sectorCode") or "unknown") != sector:
            continue
        if sensor and o.get("sensor") != sensor:
            continue
        if emission_only and not o.get("emission"):
            continue
        t = _dt(o.get("observedAt"))
        if (lo or hi) and t is None:
            continue
        if lo and t < lo:
            continue
        if hi and t > hi:
            continue
        out.append(o)
    if sort == "emission":
        # Emisyonu olmayanlar sona; sıfır gibi sıralanmaz
        out.sort(key=lambda o: (o.get("emission") is None, -((o.get("emission") or {}).get("value") or 0)))
    else:
        field = "publishedAt" if sort == "published" else "observedAt"
        # Alanı olmayan kayıtlar en sona
        out.sort(key=lambda o: o.get(field) or "", reverse=True)
        out.sort(key=lambda o: o.get(field) is None)
    return out


def public(o):
    """İstemci sözleşmesi: büyük ham alanlar ve iç bayraklar çıkarılır."""
    return {k: v for k, v in o.items() if k not in ("tr",)}


def get(obs_id, scope="all"):
    items, sources = load(scope)
    for o in items:
        if o["id"] == obs_id or o["canonicalObservationId"] == obs_id or any(
                a.get("id") == obs_id for a in o["accessPaths"]):
            src = sources.get(o.get("sourceId"))
            related = []
            if src:
                ids = set(src.get("plumeIds", []))
                related = sorted((x for x in items if x["id"] in ids and x["id"] != o["id"]),
                                 key=lambda x: x.get("observedAt") or "", reverse=True)
            return o, src, related
    return None, None, []


def get_source(source_id, scope="all"):
    items, sources = load(scope)
    s = sources.get(source_id)
    if not s:
        return None, []
    ids = set(s.get("plumeIds", []))
    plumes = sorted((x for x in items if x["id"] in ids), key=lambda x: x.get("observedAt") or "", reverse=True)
    return s, plumes
