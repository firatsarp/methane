"""Kalıcı yüksek olay arşivi: paket, revizyon geçmişi ve dosya varlıkları.

Paket: kanonik gözlem + sağlayıcı tahminleri, gerçek zamanlar, konum/footprint, gözlem anı meteorolojisi, yorum
analizi, arşiv seçimi gerekçesi, referans istatistiği anlık görüntüsü, kural/yöntem sürümleri ve kaynaklar.
Süreli imzalı URL arşiv değildir: dosya kullanım koşulları elveriyorsa indirilir ve sha256 ile saklanır; aksi halde
URL/kimlik/köken ve "arşivlenemedi" durumu tutulur.
"""
import hashlib
import json
import logging
import urllib.parse

from . import db, net, policy, store
from .retention import ARCHIVE_DIR

log = logging.getLogger("archive")

# Sağlayıcı başına dosya arşivleme izni ve lisans notu. Değer, sağlayıcının yayımlanmış kullanım koşuluna dayanır.
ASSET_POLICY = {
    "Carbon Mapper": {"allowed": True, "license": "Carbon Mapper kullanım koşulları: ticari olmayan kullanım, "
                                                  "kaynak gösterimi zorunlu (https://carbonmapper.org/terms)"},
    "NASA LP DAAC": {"allowed": False, "license": "NASA EMIT verisi açık; indirme Earthdata yetkisi ister — bu sürümde "
                                                  "yetkili dosya arşivleme bağlanmadı"},
}
# Transient alanlar revizyon özetine girmez (imzalı URL'ler her gün değişir)
TRANSIENT = ("thumbnail", "raster", "numericRaster", "ingestedAt", "imageUrl", "tr", "archiveState", "fromArchive")


def _canon(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_sha(o):
    return hashlib.sha256(_canon({k: v for k, v in o.items() if k not in TRANSIENT}).encode("utf-8")).hexdigest()


def _strip_query(url):
    if not url:
        return None
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme}://{p.netloc}{p.path}"


def _store_asset(con, data, ext, media_type, license_text):
    sha = hashlib.sha256(data).hexdigest()
    rel = f"{sha[:2]}/{sha}.{ext}"
    path = ARCHIVE_DIR / rel
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(path)
    con.execute("INSERT OR IGNORE INTO archive_assets(sha256, path, size, media_type, license, fetched_at) "
                "VALUES(?,?,?,?,?,?)", (sha, rel, len(data), media_type, license_text, store.iso(store.now())))
    return sha


def fetch_assets(o):
    """[(rol, url, uzantı, medya türü)] -> indirme sonuçları. Ağ işi işlem (transaction) dışında yapılır."""
    pol = ASSET_POLICY.get(o.get("provider"), {"allowed": False, "license": None})
    wanted = []
    if (o.get("numericRaster") or {}).get("url"):
        wanted.append(("numeric_raster", o["numericRaster"]["url"], "tif", "image/tiff"))
    if (o.get("raster") or {}).get("png"):
        wanted.append(("plume_png", o["raster"]["png"], "png", "image/png"))
    elif (o.get("thumbnail") or {}).get("url"):
        wanted.append(("thumbnail", o["thumbnail"]["url"], "png", "image/png"))
    out = []
    for role, url, ext, media in wanted:
        if not pol["allowed"]:
            out.append({"role": role, "status": "not_archived", "url": _strip_query(url),
                        "note": pol.get("license") or "Sağlayıcı dosya arşivleme koşulu doğrulanmadı"})
            continue
        try:
            data = net.request(url, raw=True, timeout=90)
            out.append({"role": role, "status": "archived", "url": _strip_query(url), "data": data, "ext": ext,
                        "media": media, "license": pol["license"]})
        except net.ProviderError as e:
            # İmzalı bağlantının süresi dolmuş olabilir; ertesi gün yeni bağlantıyla yeniden denenir
            out.append({"role": role, "status": "fetch_failed", "url": _strip_query(url),
                        "note": f"{e.kind}: dosya alınamadı; URL tek başına arşiv sayılmaz"})
    return out


def upsert(o, classification, weather=None, analysis=None, assets=None, now=None):
    """Olayı ekler/günceller. Sağlayıcı içeriği değiştiyse yeni revizyon satırı yazılır; eski sürüm korunur.
    Dönüş: 'created' | 'revised' | 'unchanged' | 'reactivated'."""
    now = store.iso(now or store.now())
    public_obs = {k: v for k, v in o.items() if k not in ("tr", "archiveState", "fromArchive")}
    groups = classification.get("groups") or {}
    bundle = {
        "schema": "peak-bundle-v1",
        "observation": public_obs,
        "providerEstimates": [{"provider": o.get("provider"), "measurement": o.get("measurement"),
                               "methodVersion": o.get("methodVersion")}]
        + [{"provider": a.get("provider"), "measurement": a.get("measurement"), "methodVersion": a.get("methodVersion"),
            "matchConfidence": a.get("matchConfidence")} for a in o.get("alternateEstimates") or []],
        "times": {k: o.get(k) for k in ("observedAt", "publishedAt", "modifiedAt", "ingestedAt")},
        "geometry": {"point": [o["lon"], o["lat"]], "footprint": o.get("footprint"),
                     "rasterBounds": (o.get("raster") or {}).get("bounds")},
        "selection": {"state": classification["state"], "reasons": classification["reasons"],
                      "baselines": {g: v["baseline"] for g, v in groups.items()}},
        "weather": weather,
        "analysis": analysis,
        "policyVersion": policy.version(),
        "peakPolicy": policy.peak(),
        "references": [policy.reference(r) for r in ("CM", "EMIT", "MARS", "S23", "S24", "SC23") if policy.reference(r)],
        "archivedAt": now,
    }
    group = sorted(groups)[0] if groups else "unassociated"
    m = o.get("measurement") or {}
    csha = content_sha(public_obs)
    with db.tx() as con:
        row = con.execute("SELECT bundle, active FROM peak_events WHERE id = ?", (o["id"],)).fetchone()
        if row:
            old = json.loads(row[0])
            # İlk arşiv anı korunur; meteoroloji/analiz yeni girdi yoksa eskisi kalır
            bundle["archivedAt"] = old.get("archivedAt", now)
            bundle["weather"] = weather or old.get("weather")
            bundle["analysis"] = analysis or old.get("analysis")
            bundle["selection"]["backfill"] = (old.get("selection") or {}).get("backfill", False)
        else:
            # İlk geri doldurmada arşive giren eski kayıtlar "yeni yüksek gözlem" bildirimi üretmez
            bundle["selection"]["backfill"] = bool(classification.get("backfill"))
        text = _canon(bundle)
        bsha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        con.execute(
            """INSERT INTO peak_events(id, group_key, state, reasons, quantity_kind, value, unit, observed, lat, lon, tr,
                                       bundle, bundle_sha, policy_version, active, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)
               ON CONFLICT(id) DO UPDATE SET group_key=excluded.group_key, state=excluded.state,
                 reasons=excluded.reasons, quantity_kind=excluded.quantity_kind, value=excluded.value,
                 unit=excluded.unit, observed=excluded.observed, bundle=excluded.bundle,
                 bundle_sha=excluded.bundle_sha, policy_version=excluded.policy_version, active=1,
                 updated_at=excluded.updated_at""",
            (o["id"], group, classification["state"], json.dumps(classification["reasons"], ensure_ascii=False),
             m.get("quantityKind"), m.get("value"), m.get("unit"), o.get("observedAt") or "", o["lat"], o["lon"],
             int(bool(o.get("tr"))), text, bsha, policy.version(), now, now))
        last = con.execute("SELECT revision, content_sha FROM peak_event_revisions WHERE event_id = ? "
                           "ORDER BY revision DESC LIMIT 1", (o["id"],)).fetchone()
        result = "created" if not row else ("reactivated" if not row[1] else "unchanged")
        if not last or last[1] != csha:
            con.execute("INSERT INTO peak_event_revisions(event_id, revision, provider_modified, content_sha, obs, "
                        "recorded_at) VALUES(?,?,?,?,?,?)",
                        (o["id"], (last[0] + 1) if last else 1, o.get("modifiedAt"), csha, _canon(public_obs), now))
            if last:
                result = "revised"
        for a in assets or []:
            sha = _store_asset(con, a["data"], a["ext"], a["media"], a["license"]) if a["status"] == "archived" else None
            prev = con.execute("SELECT sha256 FROM archive_asset_refs WHERE event_id = ? AND role = ?",
                               (o["id"], a["role"])).fetchone()
            if sha is None and prev and prev[0]:
                continue  # Daha önce arşivlenmiş dosya, bugünkü indirme hatasıyla kaybedilmez
            con.execute("INSERT OR REPLACE INTO archive_asset_refs(event_id, role, sha256, status, source_url, note) "
                        "VALUES(?,?,?,?,?,?)", (o["id"], a["role"], sha, a["status"], a.get("url"), a.get("note")))
    return result


def deactivate(event_ids):
    """Top-N'den çıkan ve başka koruma gerekçesi olmayan olaylar. Silme retention.run'da, pencere dışındaysa."""
    if not event_ids:
        return 0
    with db.tx() as con:
        return sum(con.execute("UPDATE peak_events SET active = 0, updated_at = ? WHERE id = ? AND active = 1 "
                               "AND state = 'historical_high_reference'", (store.iso(store.now()), i)).rowcount
                   for i in event_ids)


def reviews():
    with db.read() as con:
        return {pid: json.loads(r)["state"] for pid, r in
                con.execute("SELECT id, review FROM peak_events WHERE review IS NOT NULL")}


def set_review(event_id, state, note):
    """İnsan incelemesi. Reddedilen artefakt: paket özete indirilir, dosya referansları kaldırılır (audit kalır)."""
    if state not in ("reviewed_peak", "rejected_artifact"):
        raise ValueError("state")
    now = store.iso(store.now())
    review = {"state": state, "note": (note or "")[:500], "at": now}
    with db.tx() as con:
        row = con.execute("SELECT bundle FROM peak_events WHERE id = ?", (event_id,)).fetchone()
        if not row:
            return False
        if state == "rejected_artifact":
            b = json.loads(row[0])
            slim = {"schema": "peak-bundle-rejected-v1", "observation": {k: b["observation"].get(k) for k in
                    ("id", "providerId", "provider", "sensor", "observedAt", "lat", "lon", "accessPaths")},
                    "review": review, "archivedAt": b.get("archivedAt")}
            text = _canon(slim)
            con.execute("UPDATE peak_events SET state=?, review=?, bundle=?, bundle_sha=?, active=0, updated_at=? "
                        "WHERE id=?", (state, _canon(review), text, hashlib.sha256(text.encode()).hexdigest(), now,
                                       event_id))
            con.execute("DELETE FROM archive_asset_refs WHERE event_id = ?", (event_id,))
        else:
            con.execute("UPDATE peak_events SET state=?, review=?, active=1, updated_at=? WHERE id=?",
                        (state, _canon(review), now, event_id))
    return True


def list_events(scope="tr", state=None, include_inactive=False):
    q = "SELECT id, state, reasons, quantity_kind, value, unit, observed, lat, lon, tr, bundle, active, review, " \
        "created_at, updated_at FROM peak_events WHERE 1=1"
    args = []
    if not include_inactive:
        q += " AND active = 1"
    if scope == "tr":
        q += " AND tr = 1"
    if state:
        q += " AND state = ?"
        args.append(state)
    with db.read() as con:
        rows = con.execute(q + " ORDER BY observed DESC", args).fetchall()
        refs = {}
        for eid, role, sha, status, url, note in con.execute(
                "SELECT event_id, role, sha256, status, source_url, note FROM archive_asset_refs"):
            refs.setdefault(eid, []).append({"role": role, "sha256": sha, "status": status, "sourceUrl": url,
                                             "note": note, "url": f"/api/archive/assets/{sha}" if sha else None})
        revs = dict(con.execute("SELECT event_id, COUNT(*) FROM peak_event_revisions GROUP BY event_id").fetchall())
    out = []
    for r in rows:
        b = json.loads(r[10])
        o = b.get("observation") or {}
        out.append({"id": r[0], "state": r[1], "reasons": json.loads(r[2]), "quantityKind": r[3], "value": r[4],
                    "unit": r[5], "observedAt": r[6], "lat": r[7], "lon": r[8], "active": bool(r[11]),
                    "review": json.loads(r[12]) if r[12] else None, "archivedAt": b.get("archivedAt"),
                    "updatedAt": r[14], "sensor": o.get("sensor"), "provider": o.get("provider"),
                    "sourceId": o.get("sourceId"), "locationLabel": o.get("locationLabel"),
                    "selection": b.get("selection"), "hasWeather": bool((b.get("weather") or {}).get("primary")),
                    "hasAnalysis": bool(b.get("analysis")), "assets": refs.get(r[0], []),
                    "revisions": revs.get(r[0], 0), "policyVersion": b.get("policyVersion")})
    return out


def asset(sha):
    with db.read() as con:
        row = con.execute("SELECT path, media_type FROM archive_assets WHERE sha256 = ?", (sha,)).fetchone()
    return (ARCHIVE_DIR / row[0], row[1]) if row else (None, None)
