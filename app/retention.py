"""90 günlük normal gözlem penceresi ve günlük temizlik.

Kurallar (scientific-rules.json retention):
- Sınır gözlem zamanına (observedAt, UTC) göredir: observedAt < şimdi − 90 gün silinir; tam sınırdaki kayıt korunur.
- Kalıcı yüksek olay arşivi (peak_events active=1) ve onun dosyaları silinmez; bu gözlemlerin tüm erişim yolu
  kimlikleri korunur.
- Sıra: (sınıflandırma pipeline'da yapılır) -> paket bütünlüğü doğrulama -> korunan kimlik/dosya listesi ->
  pencere dışı normal kayıt/analiz/meteoroloji/önbellek silme (tek işlem) -> sahipsiz dosyalar.
- Paket doğrulaması başarısızsa korunacak veriyi riske atan adım (sahipsiz arşiv dosyası temizliği) ertelenir ve
  bakım hatası yazılır; normal kayıt temizliği sürer (eksik veri süresiz normal arşiv yaratmaz).
- Aktif (kuyrukta/çalışan) işi olan gözlemin önbellek/dosyası o turda silinmez.
- Tekrar çalıştırılabilir: ikinci çalışma aynı sonuca ulaşır, ek silme yapmaz.
"""
import hashlib
import json
import logging
import shutil
import time
from datetime import datetime, timedelta, timezone

from . import db, policy, store
from .config import DATA_DIR, TMP_DIR

log = logging.getLogger("retention")

ARCHIVE_DIR = DATA_DIR / "archive"
IMAGE_DIR = DATA_DIR / "imagery"
RASTER_DIR = DATA_DIR / "rasters"
# Ham meteoroloji/görüntü önbelleği için ayrı bütçe: süresi dolmuş satırlar bu kadar sonra silinir
EXPIRED_CACHE_GRACE = timedelta(days=7)
TMP_MAX_AGE_S = 24 * 3600
# Gözlem kimliğiyle bağlı önbellek anahtarı önekleri ("wx:obs:{id}:{yöntem}" vb.)
OBS_CACHE_PREFIXES = ("wx:obs:", "img:", "raster:", "analysis:")
OBS_TABLES = ("cm_plumes", "emit_plumes", "mars_plumes", "cams_hotspots")


def _parse(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else None


def cutoff(now=None):
    return (now or store.now()) - timedelta(days=policy.retention_days())


def is_expired(observed_iso, now=None):
    """observedAt < now − 90 gün. Zamanı doğrulanmamış kayıt yaşı bilinmediği için silinmez (raporlanır)."""
    t = _parse(observed_iso)
    return t is not None and t < cutoff(now)


def keep_on_ingest(o, protected=frozenset(), now=None):
    """Alma sırasında kalıcı tabloya yazılsın mı? Pencere içi veya korunan kayıt. Böylece silinen eski normal
    kayıtlar günlük alımla geri dolmaz; sınıflandırma ise bellekteki tam listeyle yapılır."""
    return o["id"] in protected or not is_expired(o.get("observedAt"), now)


def protected_ids(con=None):
    """Etkin yüksek olayların kanonik ve tüm erişim yolu kimlikleri."""
    own = con is None
    con = con or db.connect()
    try:
        ids = set()
        for pid, bundle in con.execute("SELECT id, bundle FROM peak_events WHERE active = 1"):
            ids.add(pid)
            o = json.loads(bundle).get("observation") or {}
            ids.update(a.get("id") for a in o.get("accessPaths") or [] if a.get("id"))
            ids.update(f"emit:{a['id']}" for a in o.get("accessPaths") or []
                       if a.get("provider") == "NASA LP DAAC" and a.get("id"))
            ids.update(f"mars:{a['id']}" for a in o.get("accessPaths") or []
                       if a.get("provider") == "UNEP IMEO MARS" and a.get("id"))
            ids.update(f"cams:{a['id']}" for a in o.get("accessPaths") or []
                       if a.get("provider") == "CAMS (Copernicus)" and a.get("id"))
        return ids
    finally:
        if own:
            con.close()


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_archive(con):
    """Etkin paketlerin bütünlüğü: paket JSON özeti ve bağlı dosyaların varlığı/özeti. Hata listesi döner."""
    errors = []
    for pid, bundle, sha in con.execute("SELECT id, bundle, bundle_sha FROM peak_events WHERE active = 1"):
        if hashlib.sha256(bundle.encode("utf-8")).hexdigest() != sha:
            errors.append({"event": pid, "error": "bundle_sha_mismatch"})
    rows = con.execute("""SELECT r.event_id, r.sha256, a.path FROM archive_asset_refs r
                          JOIN peak_events e ON e.id = r.event_id AND e.active = 1
                          LEFT JOIN archive_assets a ON a.sha256 = r.sha256
                          WHERE r.sha256 IS NOT NULL""").fetchall()
    for event, sha, path in rows:
        p = ARCHIVE_DIR / path if path else None
        if p is None or not p.exists():
            errors.append({"event": event, "error": "asset_missing", "sha256": sha})
        elif file_sha256(p) != sha:
            errors.append({"event": event, "error": "asset_checksum_mismatch", "sha256": sha})
    return errors


def _active_job_obs(con):
    out = set()
    for payload, in con.execute("SELECT payload FROM jobs WHERE state IN ('queued','submitted','running')"):
        try:
            p = json.loads(payload)
        except ValueError:
            continue
        if p.get("obsId"):
            out.add(p["obsId"])
    return out


def run(now=None):
    """Günlük temizlik. Rapor sözlüğü döner; tekrar çalıştırılabilir."""
    now = now or store.now()
    report = {"cutoff": store.iso(cutoff(now)), "basis": "observedAt_UTC", "deleted": {}, "skippedActiveJobs": 0,
              "unknownObservedAt": 0, "verificationErrors": [], "orphanAssetsDeleted": 0, "deferred": []}
    files_to_delete = []
    with db.tx() as con:
        errors = verify_archive(con)
        report["verificationErrors"] = errors
        protected = protected_ids(con)
        busy = _active_job_obs(con)
        doomed = set()
        for table in OBS_TABLES:
            ids = []
            for oid, observed, obs in con.execute(f"SELECT id, observed, obs FROM {table}"):
                o_obs = json.loads(obs).get("observedAt") or observed
                if _parse(o_obs) is None:
                    report["unknownObservedAt"] += 1
                    continue
                if oid in protected or not is_expired(o_obs, now):
                    continue
                if oid in busy:
                    report["skippedActiveJobs"] += 1
                    continue
                ids.append(oid)
            for k in range(0, len(ids), 500):
                chunk = ids[k:k + 500]
                con.execute(f"DELETE FROM {table} WHERE id IN ({','.join('?' * len(chunk))})", chunk)
            report["deleted"][table] = len(ids)
            doomed.update(ids)
        # Normal kayda bağlı yan veriler: sürüm geçmişi, ilk görülme, analiz, meteoroloji/görüntü önbellekleri
        side = {"plume_versions": 0, "plume_seen": 0, "analysis_results": 0, "cache": 0, "jobs": 0}
        for oid in doomed:
            side["plume_versions"] += con.execute("DELETE FROM plume_versions WHERE id = ?", (oid,)).rowcount
            side["plume_seen"] += con.execute("DELETE FROM plume_seen WHERE id = ?", (oid,)).rowcount
        for oid, observed in con.execute("SELECT obs_id, observed FROM analysis_results").fetchall():
            if oid not in protected and oid not in busy and (oid in doomed or is_expired(observed, now)):
                side["analysis_results"] += con.execute("DELETE FROM analysis_results WHERE obs_id = ?", (oid,)).rowcount
                doomed.add(oid)
        for key, in con.execute("SELECT key FROM cache").fetchall():
            for prefix in OBS_CACHE_PREFIXES:
                if key.startswith(prefix):
                    oid = key[len(prefix):].rsplit(":", 1)[0]
                    if oid in doomed and oid not in protected and oid not in busy:
                        side["cache"] += con.execute("DELETE FROM cache WHERE key = ?", (key,)).rowcount
                        files_to_delete += _obs_files(oid)
                    break
        for jid, payload in con.execute("SELECT id, payload FROM jobs WHERE state IN ('done','failed','blocked')").fetchall():
            if (json.loads(payload).get("obsId") or "") in doomed:
                side["jobs"] += con.execute("DELETE FROM jobs WHERE id = ?", (jid,)).rowcount
        side["cache_expired"] = con.execute("DELETE FROM cache WHERE expires_at IS NOT NULL AND expires_at < ?",
                                            (store.iso(now - EXPIRED_CACHE_GRACE),)).rowcount
        # Top-N'den çıkmış (etkin olmayan) ve pencere dışındaki olaylar: başka koruma gerekçesi yok
        inactive = [r[0] for r in con.execute("SELECT id, observed FROM peak_events WHERE active = 0").fetchall()
                    if is_expired(r[1], now)]
        for pid in inactive:
            con.execute("DELETE FROM peak_event_revisions WHERE event_id = ?", (pid,))
            con.execute("DELETE FROM archive_asset_refs WHERE event_id = ?", (pid,))
            con.execute("DELETE FROM peak_events WHERE id = ?", (pid,))
        side["inactive_events"] = len(inactive)
        report["deleted"].update(side)
        # Sahipsiz arşiv dosyaları: doğrulama hatası varsa ertelenir (bozuk referans yanlış dosyayı sahipsiz gösterebilir)
        if errors:
            report["deferred"].append("orphan_archive_assets")
        else:
            orphans = con.execute("""SELECT sha256, path FROM archive_assets WHERE sha256 NOT IN
                                     (SELECT sha256 FROM archive_asset_refs WHERE sha256 IS NOT NULL)""").fetchall()
            for sha, path in orphans:
                con.execute("DELETE FROM archive_assets WHERE sha256 = ?", (sha,))
                files_to_delete.append(ARCHIVE_DIR / path)
            report["orphanAssetsDeleted"] = len(orphans)
        con.execute("INSERT INTO maintenance_log(kind, level, message, created_at) VALUES(?,?,?,?)",
                    ("retention", "error" if errors else "info",
                     json.dumps({k: report[k] for k in ("deleted", "verificationErrors", "deferred")}, ensure_ascii=False),
                     store.iso(now)))
    # Dosyalar ancak veritabanı işlemi tamamlandıktan sonra silinir
    for p in files_to_delete:
        try:
            p.unlink(missing_ok=True)
        except OSError as e:
            report.setdefault("fileErrors", []).append(f"{p.name}: {type(e).__name__}")
    _clean_tmp()
    log.info("Saklama: %s", report["deleted"])
    return report


def _obs_files(oid):
    from .imagery import image_path
    digest = hashlib.sha256(oid.encode()).hexdigest()[:24]
    return [image_path(oid), RASTER_DIR / f"{digest}.tif"]


def _clean_tmp():
    if not TMP_DIR.exists():
        return
    limit = time.time() - TMP_MAX_AGE_S
    for p in TMP_DIR.iterdir():
        try:
            if p.stat().st_mtime < limit:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink(missing_ok=True)
        except OSError as e:
            log.warning("Geçici dosya silinemedi %s: %s", p.name, type(e).__name__)


def status():
    """Kamuya açık, sır içermeyen saklama özeti."""
    with db.read() as con:
        counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in OBS_TABLES}
        events = dict(con.execute("SELECT state, COUNT(*) FROM peak_events WHERE active = 1 GROUP BY state").fetchall())
        assets = con.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM archive_assets").fetchone()
        refs = dict(con.execute("SELECT status, COUNT(*) FROM archive_asset_refs GROUP BY status").fetchall())
        last = con.execute("SELECT level, message, created_at FROM maintenance_log WHERE kind='retention' "
                           "ORDER BY id DESC LIMIT 1").fetchone()
    return {"policy": policy.rules()["retention"], "policyVersion": policy.version(), "cutoff": store.iso(cutoff()),
            "ordinaryRecords": counts, "archive": {"events": events, "assets": assets[0], "assetBytes": assets[1],
                                                   "assetRefs": refs},
            "lastRun": {"level": last[0], "summary": json.loads(last[1]), "at": last[2]} if last else None}
