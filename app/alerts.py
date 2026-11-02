"""Kaynak durumu ve açıklanabilir izleme olayları. Her olay, neden üretildiğini gösteren yapılandırılmış alanlar taşır;
metin istemcide bu alanlardan kurulur. Katalogda kayıt bulunmaması olay veya negatif gözlem üretmez.
Olaylar yalnızca site içinde gösterilir; e-posta/mesaj gönderilmez."""
import os
from datetime import datetime, timedelta, timezone

import numpy as np

from . import catalog, cds, db, sources
from .config import (ALERT_WINDOW_DAYS, CARBON_MAPPER_API_TOKEN, INGEST_INTERVAL_MIN, NX, NY, PENDING_CREDENTIALS,
                     QA_MIN)
from .geo import turkey_cell_mask

# Son başarılı almadan bu kadar saat geçerse "güncel değil" uyarısı (zamanlama aralığının yaklaşık iki katı)
STALE_HOURS = {"carbonmapper": 50, "tropomi": max(6, 3 * INGEST_INTERVAL_MIN / 60), "celestrak": 36, "emit": 50,
               "pipeline": 50, "cams_hotspots": 50}
# Gözlem ile ilk alınma arasında bundan uzun süre varsa "geçmiş gözlem yeni yayımlandı" olarak sunulur
PAST_OBSERVATION_DAYS = 30

# Bölgesel artış kuralı (TROPOMI). Değerler arayüzde kuralla birlikte gösterilir.
REGIONAL_RULE = {
    "id": "tropomi-block-mad-v1",
    "blockCells": 5,             # 5 × 5 hücre = 0,5° × 0,5° blok
    "minBlockCoverage": 0.5,     # blok kara hücrelerinin en az yarısı geçerli ölçüm içermeli
    "madFactor": 3.0,            # blok anomalisi, günün blok anomalileri medyanından 3 sağlam sapma (1,4826·MAD) yukarıda
    "windowDays": 7,
    "minDays": 2,                # pencere içinde en az 2 farklı günde koşul sağlanmalı
    "qaMin": QA_MIN,
    "reference": "Aynı gündeki Türkiye kara hücrelerinin medyanı",
}

SOURCES = [
    {"id": "carbonmapper", "name": "Carbon Mapper metan bulutları (Planet Tanager-1, NASA EMIT)",
     "schedule": "24 saatte bir", "connected": True, "credentials": ()},
    {"id": "carbonmapper_sources", "name": "Carbon Mapper kaynak özetleri",
     "schedule": "24 saatte bir", "connected": True, "credentials": ()},
    {"id": "tropomi", "name": "Sentinel-5P TROPOMI CH₄ (NASA GES DISC)",
     "schedule": f"{INGEST_INTERVAL_MIN} dakikada bir", "connected": True,
     "credentials": ("EARTHDATA_TOKEN|EARTHDATA_USERNAME+EARTHDATA_PASSWORD",)},
    {"id": "emit", "name": "NASA EMIT incelenmiş metan bulutları (LP DAAC)",
     "schedule": "24 saatte bir", "connected": True,
     "credentials": ("EARTHDATA_TOKEN|EARTHDATA_USERNAME+EARTHDATA_PASSWORD",)},
    {"id": "celestrak", "name": "CelesTrak yörünge verisi (gözlem fırsatı tahmini)",
     "schedule": "12 saatte bir", "connected": True, "observations": False, "credentials": ()},
    {"id": "openmeteo_forecast", "name": "Open-Meteo güncel durum ve tahmin",
     "schedule": "İstek üzerine, 1 saat önbellek", "connected": True, "credentials": ()},
    {"id": "openmeteo_era5", "name": "ERA5 — Open-Meteo üzerinden (gözlem anı, iklim)",
     "schedule": "İstek üzerine ve arka plan işi, kalıcı önbellek", "connected": True, "credentials": ()},
    {"id": "cds", "name": "ERA5 — Copernicus Climate Data Store",
     "schedule": "Arka plan iş kuyruğu", "connected": True, "credentials": ("CDSAPI_KEY",)},
    {"id": "cdse", "name": "Copernicus Data Space — Sentinel-2 L2A yüzey görüntüsü",
     "schedule": "İstek üzerine, kalıcı önbellek", "connected": True,
     "credentials": ("CDSE_CLIENT_ID+CDSE_CLIENT_SECRET",)},
    {"id": "mars", "name": "UNEP IMEO MARS yayımlanmış bulutları (CSV, CC BY-NC-SA 4.0)",
     "schedule": "Günlük iş hattı, koşullu indirme", "connected": True, "credentials": ()},
    {"id": "cams_hotspots", "name": "CAMS TROPOMI metan sıcak noktaları (CSV, haftalık yayın)",
     "schedule": "Günlük iş hattı", "connected": True, "credentials": ()},
    {"id": "pipeline", "name": "Günlük iş hattı (sınıflandırma, arşiv, yorum)",
     "schedule": "Her gün 03:30 TSİ", "connected": True, "observations": False, "credentials": ()},
    {"id": "retention", "name": "90 gün saklama ve arşiv bütünlüğü",
     "schedule": "Günlük iş hattının son adımı", "connected": True, "observations": False, "credentials": ()},
]


def _parse(iso):
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return dt if dt.tzinfo else None


def _has(spec):
    """'A|B+C' biçimi: A tanımlı ya da hem B hem C tanımlı. Değer okunmaz, yalnızca var olup olmadığı."""
    return any(all(os.environ.get(k, "").strip() for k in alt.split("+")) for alt in spec.split("|"))


def source_status():
    """Sır içermeyen kaynak durumları. 'configured' yalnızca erişim bilgisinin varlığıdır;
    'healthy' gerçek bir isteğin başarıyla sonuçlandığını gösterir."""
    out = []
    for s in SOURCES:
        st = sources.get(s["id"])
        item = {k: v for k, v in s.items() if k != "credentials"}
        item.update(st)
        item["configured"] = all(_has(c) for c in s["credentials"]) if s["credentials"] else True
        item["requiresCredentials"] = bool(s["credentials"])
        if s["id"] == "carbonmapper" and "token" not in item:
            item["token"] = "set" if CARBON_MAPPER_API_TOKEN else "unset"
        if not item["configured"]:
            item["state"] = "not_configured"
        elif not st:
            item["state"] = "pending"
        item["healthy"] = item.get("state") == "ok" and bool(item.get("last_ok"))
        # Hata metinleri kısaltılır; izler, URL sorguları ve anahtarlar gönderilmez
        if item.get("error"):
            item["error"] = str(item["error"]).split("\n")[0][:160]
        out.append(item)
    # Geriye dönük uyumluluk: eski istemciler not_connected bekliyordu
    for item in out:
        if item["id"] in PENDING_CREDENTIALS and item["state"] == "not_configured":
            item["connected"] = False
    return out


# ---- İzleme olayları ----------------------------------------------------------------------

def _seen(since):
    with db.read() as con:
        return dict(con.execute(
            "SELECT id, first_seen FROM plume_seen WHERE baseline = 0 AND first_seen >= ?", (since,)).fetchall())


def _plume_events(now, items, srcs):
    since = (now - timedelta(days=ALERT_WINDOW_DAYS)).isoformat()
    seen = _seen(since)
    by_id = {o["id"]: o for o in items}
    out = []
    for pid, first in sorted(seen.items(), key=lambda x: x[1], reverse=True):
        o = by_id.get(pid)
        if not o:
            continue
        observed, published, ingested = _parse(o.get("observedAt") or ""), _parse(o.get("publishedAt") or ""), _parse(first)
        lag_obs = (ingested - observed).days if observed and ingested else None
        kind = "past_observation_published" if lag_obs is not None and lag_obs > PAST_OBSERVATION_DAYS else "new_observation"
        ev = {
            "kind": kind, "level": "info", "plumeId": pid, "sensor": o.get("sensor"), "provider": o.get("provider"),
            "observedAt": o.get("observedAt"), "publishedAt": o.get("publishedAt"), "ingestedAt": first,
            "publicationLagDays": round((published - observed).total_seconds() / 86400, 1) if observed and published else None,
            "emission": o.get("emission"),
            "rule": f"Kayıt son {ALERT_WINDOW_DAYS} gün içinde sisteme ilk kez alındı"
                    + (f"; gözlem tarihi alınma tarihinden {PAST_OBSERVATION_DAYS} günden eski." if kind != "new_observation" else "."),
        }
        out.append(ev)
        src = srcs.get(o.get("sourceId"))
        if src and len(src.get("plumeIds", [])) >= 2:
            out.append({
                "kind": "repeated_detection", "level": "info", "plumeId": pid, "sourceId": src["id"],
                "plumeCount": len(src["plumeIds"]), "observedAt": o.get("observedAt"),
                "detectionDateCount": src.get("detectionDateCount"), "observationDateCount": src.get("observationDateCount"),
                "rule": "Yeni kayıt, Carbon Mapper'ın aynı kaynak kümesine bağladığı bulutlardan biridir "
                        f"(küme yarıçapı {src.get('clusterRadiusM') or '?'} m). Kümeleme sağlayıcınındır; tesis doğrulaması değildir.",
            })
    return out


def _version_events(now):
    since = (now - timedelta(days=ALERT_WINDOW_DAYS)).isoformat()
    with db.read() as con:
        rows = con.execute(
            """SELECT v.id, v.modified, v.version, v.emission, v.seen_at,
                      (SELECT p.version FROM plume_versions p WHERE p.id = v.id AND p.modified < v.modified
                       ORDER BY p.modified DESC LIMIT 1),
                      (SELECT p.emission FROM plume_versions p WHERE p.id = v.id AND p.modified < v.modified
                       ORDER BY p.modified DESC LIMIT 1)
               FROM plume_versions v WHERE v.seen_at >= ?
                 AND EXISTS (SELECT 1 FROM plume_versions p WHERE p.id = v.id AND p.modified < v.modified)""",
            (since,)).fetchall()
    return [{"kind": "record_updated", "level": "info", "plumeId": r[0], "modifiedAt": r[1], "version": r[2],
             "emission": r[3], "previousVersion": r[5], "previousEmission": r[6], "seenAt": r[4],
             "rule": "Sağlayıcı kaydın değişiklik zamanını (modified) güncelledi; önceki sürüm ayrıca saklanır."}
            for r in rows]


def regional_candidates(now=None):
    """TROPOMI 0,5° bloklarında, tanımlı kurala göre incelenecek bölgesel artışlar. Kaynak ataması yapmaz."""
    r = REGIONAL_RULE
    with db.read() as con:
        last = con.execute("SELECT MAX(day) FROM cells WHERE tr = 1").fetchone()[0]
        if not last:
            return []
        start = (datetime.fromisoformat(last) - timedelta(days=r["windowDays"] - 1)).date().isoformat()
        rows = con.execute(
            """WITH best AS (SELECT day, MAX(source) AS source FROM cells WHERE day BETWEEN ? AND ? GROUP BY day)
               SELECT c.day, c.j, c.i, c.s / c.n FROM cells c JOIN best b ON c.day = b.day AND c.source = b.source
               WHERE c.tr = 1""", (start, last)).fetchall()
    if not rows:
        return []
    mask = turkey_cell_mask()
    b = r["blockCells"]
    land = np.zeros(((NY + b - 1) // b, (NX + b - 1) // b))
    jj, ii = np.nonzero(mask)
    np.add.at(land, (jj // b, ii // b), 1)
    by_day = {}
    for day, j, i, v in rows:
        by_day.setdefault(day, []).append((j, i, v))
    hits = {}
    for day, cells in by_day.items():
        vals = np.array([v for _, _, v in cells])
        median = float(np.median(vals))
        s = np.zeros_like(land)
        n = np.zeros_like(land)
        for j, i, v in cells:
            s[j // b, i // b] += v - median
            n[j // b, i // b] += 1
        ok = (land > 0) & (n >= r["minBlockCoverage"] * land)
        if ok.sum() < 10:
            continue
        anom = np.where(ok, s / np.maximum(n, 1), np.nan)
        a = anom[ok]
        center = float(np.median(a))
        mad = 1.4826 * float(np.median(np.abs(a - center)))
        if mad <= 0:
            continue
        limit = center + r["madFactor"] * mad
        for bj, bi in zip(*np.nonzero(ok & (anom > limit))):
            hits.setdefault((int(bj), int(bi)), []).append(
                {"day": day, "anomalyPpb": round(float(anom[bj, bi]), 1), "limitPpb": round(limit, 1),
                 "coverage": round(float(n[bj, bi] / land[bj, bi]), 2), "dayMedianPpb": round(median, 1)})
    from .config import BBOX, GRID
    out = []
    for (bj, bi), days in hits.items():
        if len({d["day"] for d in days}) < r["minDays"]:
            continue
        lat0, lon0 = BBOX[1] + bj * b * GRID, BBOX[0] + bi * b * GRID
        out.append({"kind": "regional_enhancement", "level": "review",
                    "block": {"south": round(lat0, 2), "west": round(lon0, 2),
                              "north": round(lat0 + b * GRID, 2), "east": round(lon0 + b * GRID, 2)},
                    "days": sorted(days, key=lambda d: d["day"]), "rule": r})
    return out


def _source_events(now):
    out = []
    for s in source_status():
        if s.get("state") == "error":
            out.append({"kind": "source_error", "level": "warn", "source": s["id"], "name": s["name"],
                        "errorClass": s.get("error_class") or "error", "lastOk": s.get("last_ok"),
                        "lastCheck": s.get("last_check"),
                        "rule": "Son kontrol başarısız oldu; gösterilen veriler son başarılı almaya aittir."})
        last_ok = _parse(s.get("last_ok") or "")
        limit = STALE_HOURS.get(s["id"])
        if last_ok and limit and now - last_ok > timedelta(hours=limit):
            out.append({"kind": "stale", "level": "warn", "source": s["id"], "name": s["name"],
                        "lastOk": s["last_ok"], "maxAgeHours": limit,
                        "rule": f"Beklenen en geç {limit:.0f} saat içinde yeni başarılı alma yapılmadı."})
        if s["id"] == "carbonmapper" and s.get("token") == "rejected":
            out.append({"kind": "token_rejected", "level": "warn", "source": s["id"], "name": s["name"],
                        "rule": "Sağlayıcı tokenla yapılan isteği yetkisiz saydı; herkese açık katalog kullanılıyor."})
    return out


def _peak_events(now):
    """Son pencere içinde kalıcı arşive giren yüksek kayıtlar. İlk geri doldurma (backfill) bildirim üretmez.
    Sınıf: inceleme adayı (robust/mutlak kural) veya yeni yayımlanan yüksek gözlem (seri içi en yüksekler)."""
    import json
    since = (now - timedelta(days=ALERT_WINDOW_DAYS)).isoformat()
    with db.read() as con:
        rows = con.execute("SELECT id, state, reasons, value, unit, observed, created_at, bundle FROM peak_events "
                           "WHERE active = 1 AND tr = 1 AND created_at >= ?", (since,)).fetchall()
    out = []
    for pid, state, reasons, value, unit, observed, created, bundle in rows:
        sel = (json.loads(bundle).get("selection") or {})
        if sel.get("backfill"):
            continue
        out.append({"kind": "review_candidate" if state in ("peak_candidate", "reviewed_peak") else "high_observation_published",
                    "level": "review" if state == "peak_candidate" else "info", "plumeId": pid, "state": state,
                    "value": value, "unit": unit, "observedAt": observed, "archivedAt": created,
                    "reasons": json.loads(reasons),
                    "rule": "Kayıt kalıcı yüksek olay arşivine girdi; aday bulgu doğrulanmış yüksek salım değildir."})
    return out


def events():
    now = datetime.now(timezone.utc)
    items, srcs = catalog.load("tr")
    out = _source_events(now) + _plume_events(now, items, srcs) + _version_events(now) + _peak_events(now)
    try:
        out += regional_candidates(now)
    except Exception as e:  # kural hesaplanamazsa diğer olaylar yine döner; neden ayrıca raporlanır
        out.append({"kind": "rule_error", "level": "warn", "rule": REGIONAL_RULE["id"], "error": type(e).__name__})
    return {"windowDays": ALERT_WINDOW_DAYS, "generatedAt": now.isoformat(), "items": out,
            "regionalRule": REGIONAL_RULE, "cdsConfigured": cds.configured()}


def alerts():
    """Eski /api/alerts sözleşmesi: yeni kayıtlar new_plume olarak döner."""
    ev = events()
    items = []
    for e in ev["items"]:
        if e["kind"] in ("new_observation", "past_observation_published"):
            items.append({**e, "kind": "new_plume"})
        elif e["kind"] in ("source_error", "stale", "token_rejected"):
            items.append({**e, "error": e.get("errorClass")})
    return {"windowDays": ev["windowDays"], "items": items}
