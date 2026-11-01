"""Kaynak durumu ve açıklanabilir uyarılar. Her uyarı, neden üretildiğini gösteren yapılandırılmış alanlar taşır;
metin istemcide bu alanlardan kurulur. Katalogda kayıt bulunmaması uyarı veya negatif gözlem üretmez."""
import json
import os
from datetime import datetime, timedelta, timezone

from . import db, sources
from .config import ALERT_WINDOW_DAYS, CARBON_MAPPER_API_TOKEN, INGEST_INTERVAL_MIN, PENDING_CREDENTIALS

# Son başarılı almadan bu kadar saat geçerse "güncel değil" uyarısı (zamanlama aralığının yaklaşık iki katı)
STALE_HOURS = {"carbonmapper": 50, "tropomi": max(6, 3 * INGEST_INTERVAL_MIN / 60), "celestrak": 36}

SOURCES = [
    {"id": "carbonmapper", "name": "Carbon Mapper metan bulutları (Planet Tanager-1, NASA EMIT)",
     "schedule": "24 saatte bir", "connected": True},
    {"id": "tropomi", "name": "Sentinel-5P TROPOMI CH₄ (NASA GES DISC)",
     "schedule": f"{INGEST_INTERVAL_MIN} dakikada bir", "connected": True},
    {"id": "celestrak", "name": "CelesTrak yörünge verisi (geçiş tahmini)",
     "schedule": "12 saatte bir", "connected": True, "observations": False},
    {"id": "cdse", "name": "Copernicus Data Space / Sentinel Hub", "connected": False},
    {"id": "cds", "name": "Copernicus Climate Data Store (ERA5)", "connected": False},
]


def _parse(iso):
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return dt if dt.tzinfo else None


def source_status():
    out = []
    for s in SOURCES:
        st = sources.get(s["id"]) if s["connected"] else {}
        item = {**s, **st}
        if s["id"] in PENDING_CREDENTIALS:
            # Yalnızca değişkenin tanımlı olup olmadığı; değer okunmaz ve döndürülmez
            item["configured"] = all(os.environ.get(k, "").strip() for k in PENDING_CREDENTIALS[s["id"]])
            item["state"] = "not_connected"
        elif s["id"] == "carbonmapper" and "token" not in item:
            item["token"] = "set" if CARBON_MAPPER_API_TOKEN else "unset"
        if s["connected"] and not st:
            item["state"] = "pending"
        out.append(item)
    return out


def _new_plumes(now):
    since = (now - timedelta(days=ALERT_WINDOW_DAYS)).isoformat()
    with db.read() as con:
        rows = con.execute(
            """SELECT c.obs FROM cm_plumes c JOIN plume_seen s ON s.id = c.id
               WHERE c.tr = 1 AND s.baseline = 0 AND s.first_seen >= ?
               ORDER BY s.first_seen DESC, c.observed DESC""",
            (since,),
        ).fetchall()
    out = []
    for (raw,) in rows:
        o = json.loads(raw)
        observed, published = _parse(o.get("observedAt") or ""), _parse(o.get("publishedAt") or "")
        out.append({
            "kind": "new_plume", "level": "info", "plumeId": o["id"], "sensor": o.get("sensor"),
            "observedAt": o.get("observedAt"), "publishedAt": o.get("publishedAt"), "ingestedAt": o.get("ingestedAt"),
            "publicationLagDays": round((published - observed).total_seconds() / 86400, 1)
            if observed and published else None,
            "emission": o.get("emission"),
        })
    return out


def alerts():
    now = datetime.now(timezone.utc)
    out = []
    for s in source_status():
        if not s.get("connected"):
            continue
        if s.get("state") == "error":
            out.append({"kind": "source_error", "level": "warn", "source": s["id"], "name": s["name"],
                        "error": s.get("error"), "lastOk": s.get("last_ok"), "lastCheck": s.get("last_check")})
        last_ok = _parse(s.get("last_ok") or "")
        limit = STALE_HOURS.get(s["id"])
        if last_ok and limit and now - last_ok > timedelta(hours=limit):
            out.append({"kind": "stale", "level": "warn", "source": s["id"], "name": s["name"],
                        "lastOk": s["last_ok"], "maxAgeHours": limit})
        if s["id"] == "carbonmapper" and s.get("token") == "rejected":
            out.append({"kind": "token_rejected", "level": "warn", "source": s["id"], "name": s["name"]})
    return {"windowDays": ALERT_WINDOW_DAYS, "items": out + _new_plumes(now)}
