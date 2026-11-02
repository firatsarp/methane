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
    # Önceki sürümde alınan EMIT kayıtları görüntü bağlantısını yalnızca imageUrl alanında taşır
    if not o.get("thumbnail") and o.get("imageUrl"):
        o["thumbnail"] = {"url": o["imageUrl"], "kind": "NASA EMIT bulut kompleksi görüntüsü"}
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


def _attach(best, e, confidence, dist):
    best["accessPaths"].append({**e["accessPaths"][0], "matchDistanceM": round(dist), "matchConfidence": confidence})
    if not best.get("footprint") and e.get("footprint"):
        best["footprint"] = e["footprint"]
    if not best.get("thumbnail"):
        best["thumbnail"] = e.get("thumbnail")
    best["alternateEstimates"].append({
        "provider": e["provider"], "id": e.get("providerId"), "emission": e.get("emission"),
        "measurement": e.get("measurement"), "methodVersion": e.get("methodVersion"),
        "sourceAssociation": e.get("sourceId"), "maxEnhancement": e.get("maxEnhancement"),
        "windNote": e.get("windNote"), "processingVersion": e.get("processingVersion"),
        "matchConfidence": confidence,
    })
    # Gözlem düzeyindeki eşleşme güveni en zayıf bağlantıya göre: bir belirsiz eşleşme bütün gözlemi işaretler
    if confidence != "strong":
        best["matchConfidence"] = "probable"
    elif best.get("matchConfidence") != "probable":
        best["matchConfidence"] = "strong"
    best.setdefault("mergeNote", "Aynı uydu çekimi birden fazla sağlayıcı/erişim yolundan alındı; her sağlayıcının "
                                 "tahmini ayrı korunur. Bu, iki bağımsız ölçüm değildir.")


def merge_by_acquisition(base, others):
    """others içindeki kayıtları base'deki aynı çekime bağlar. Önce özgün çekim kimliği (strong); kimlik yoksa
    yalnızca aynı EMIT sensöründe zaman (≤120 sn) ve konum (≤1 km) yakınlığı "probable" eşleşme olarak işaretlenir.
    Farklı sensörler hiçbir zaman konum/zaman yakınlığıyla birleştirilmez."""
    by_acq = {}
    for c in base:
        if c.get("acquisitionId"):
            by_acq.setdefault(c["acquisitionId"], []).append(c)
    used = set()
    standalone = []
    for e in others:
        best, best_d, conf = None, None, None
        for c in by_acq.get(e.get("acquisitionId") or "\0", []):
            d = distance_m(e["lat"], e["lon"], c["lat"], c["lon"])
            if (c["id"], e["provider"]) not in used and (best_d is None or d < best_d):
                best, best_d, conf = c, d, "strong"
        te = _dt(e.get("observedAt"))
        if best is None and te is not None and e.get("instrument") == "emi" and not e.get("acquisitionId"):
            for c in base:
                if c.get("instrument") != "emi" or (c["id"], e["provider"]) in used or not _dt(c.get("observedAt")):
                    continue
                if abs((_dt(c["observedAt"]) - te).total_seconds()) > SAME_SCENE_SECONDS:
                    continue
                d = distance_m(e["lat"], e["lon"], c["lat"], c["lon"])
                if d <= SAME_PLACE_M and (best_d is None or d < best_d):
                    best, best_d, conf = c, d, "probable"
        # Aynı çekimde birden fazla bulut olabilir; çok uzak bulut aynı gözleme bağlanmaz
        if best is None or best_d > max(SAME_PLACE_M, 2 * (e.get("locationUncertaintyM") or 0)):
            standalone.append(e)
            continue
        used.add((best["id"], e["provider"]))
        _attach(best, e, conf, best_d)
    merged = base + standalone
    for o in merged:
        o.setdefault("matchConfidence", "single")
    return merged


def merge_emit(cm_items, emit_items):
    """Geriye dönük ad: NASA EMIT kayıtlarını Carbon Mapper'daki aynı çekime bağlar."""
    return merge_by_acquisition(cm_items, emit_items)


def _plain_item(raw, tr):
    """MARS / CAMS kayıtları: normalize edilmiş gözlem olduğu gibi, katalog alanları eklenerek."""
    o = json.loads(raw)
    o.update({
        "canonicalObservationId": o["id"], "tr": bool(tr),
        "geometry": {"type": "Point", "coordinates": [o["lon"], o["lat"]]},
        "accessPaths": [{"provider": o["provider"], "id": o.get("providerId"), "url": o.get("sourceUrl"),
                         "kind": o.get("accessKind") or "Yayımlanmış veri dosyası"}],
        "raster": None, "footprint": None, "alternateEstimates": [],
    })
    return o


def compose(cm_obs, emit_obs, mars_obs=(), cams_obs=()):
    """Sağlayıcı kayıtlarından kanonik gözlemler: önce Carbon Mapper, sonra NASA EMIT, MARS ve CAMS aynı çekime
    bağlanır. Pipeline bunu bellekteki tam (90 gün dışı dahil) kümeyle, katalog API'si saklanan kayıtlarla çağırır."""
    items = merge_by_acquisition(list(cm_obs), list(emit_obs))
    items = merge_by_acquisition(items, list(mars_obs))
    return merge_by_acquisition(items, list(cams_obs))


def _archived():
    """Kalıcı arşivdeki etkin yüksek olaylar: {id: (durum, gözlem)}."""
    with db.read() as con:
        rows = con.execute("SELECT id, state, bundle, tr FROM peak_events WHERE active = 1").fetchall()
    out = {}
    for pid, state, bundle, tr in rows:
        b = json.loads(bundle)
        o = b.get("observation") or {}
        if o.get("id"):
            out[pid] = (state, {**o, "tr": bool(tr)})
    return out


def load(scope="tr", include_archive=True):
    with db.read() as con:
        srcs = [(json.loads(r[0]), r[1]) for r in con.execute("SELECT obs, tr FROM cm_sources")]
        cm_rows = con.execute("SELECT id, png, bounds, obs, tr FROM cm_plumes").fetchall()
        emit_rows = con.execute("SELECT id, footprint, obs, tr FROM emit_plumes").fetchall()
        mars_rows = con.execute("SELECT obs, tr FROM mars_plumes").fetchall()
        cams_rows = con.execute("SELECT obs, tr FROM cams_hotspots").fetchall()
    source_of = {pid: s["id"] for s, _ in srcs for pid in s.get("plumeIds", [])}
    items = compose([_cm_item(r, source_of) for r in cm_rows], [_emit_item(r) for r in emit_rows],
                    [_plain_item(*r) for r in mars_rows], [_plain_item(*r) for r in cams_rows])
    if include_archive:
        archived = _archived()
        present = {o["id"] for o in items}
        for o in items:
            if o["id"] in archived:
                o["archiveState"] = archived[o["id"]][0]
        for pid, (state, o) in archived.items():
            if pid not in present:
                # 90 gün penceresinden çıkmış normal kayıt değil; kalıcı yüksek olay arşivinden gelir
                items.append({**o, "archiveState": state, "fromArchive": True})
    if scope == "tr":
        items = [o for o in items if o["tr"]]
    sources = {s["id"]: {**s, "tr": bool(tr)} for s, tr in srcs if scope != "tr" or tr}
    _attach_source_views(items, sources)
    return items, sources


def _attach_source_views(items, sources):
    """Kaynak kartı için: sunulan kapsamdaki bağlı gözlem sayısı ve en son gözlemin gerçek görüntüsü."""
    by_source = {}
    for o in items:
        if o.get("sourceId") in sources:
            by_source.setdefault(o["sourceId"], []).append(o)
    for sid, s in sources.items():
        linked = sorted(by_source.get(sid, []), key=lambda o: o.get("observedAt") or "", reverse=True)
        s["linkedObservations"] = len(linked)
        s["thumbnail"] = next((o["thumbnail"] for o in linked if o.get("thumbnail")), None)


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
        if o["id"] == obs_id or o.get("canonicalObservationId") == obs_id or any(
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
