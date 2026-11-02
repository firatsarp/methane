"""NASA Earthdata'dan TROPOMI metan, Carbon Mapper'dan metan bulutu verilerini çeker."""
import json
import logging
import os
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import numpy as np

from . import db, observations, sources
from .config import (
    BBOX, CARBON_MAPPER_API_TOKEN, CARBONMAPPER_API, CARBONMAPPER_PAGE, CARBONMAPPER_SOURCES_API, NRT_DAYS, NX, NY,
    QA_MIN, TMP_DIR,
    BACKFILL_DAYS, TROPOMI_NRT, TROPOMI_OFFL,
)
from .geo import cell_index, in_turkey, turkey_cell_mask

log = logging.getLogger("ingest")

_logged_in = False


def _now():
    return datetime.now(timezone.utc)


def login():
    """EARTHDATA_USERNAME/EARTHDATA_PASSWORD (veya EARTHDATA_TOKEN) ortam değişkenleriyle giriş."""
    global _logged_in
    if _logged_in:
        return True
    import earthaccess

    # docker-compose tanımsız tokenı "" olarak geçirir; earthaccess "" değerini token sayar ve
    # CMR 401 "Token does not exist" döner. Boşsa kaldır ki kullanıcı adı/parola kullanılsın.
    if not os.environ.get("EARTHDATA_TOKEN"):
        os.environ.pop("EARTHDATA_TOKEN", None)
    try:
        auth = earthaccess.login(strategy="environment")
        _logged_in = bool(auth and auth.authenticated)
    except Exception as e:  # kimlik bilgisi yoksa earthaccess hata fırlatabilir
        log.warning("Earthdata girişi başarısız: %s", e)
        _logged_in = False
    db.set_meta("auth", "ok" if _logged_in else "missing")
    return _logged_in


def _granule_id(g):
    return g["umm"].get("GranuleUR") or g["meta"]["native-id"]


def _granule_start(g):
    s = g["umm"]["TemporalExtent"]["RangeDateTime"]["BeginningDateTime"]
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# --- TROPOMI -----------------------------------------------------------------

def _grid_file(path):
    """Bir TROPOMI CH4 L2 dosyasını Türkiye kutusunda 0.1° grid toplamlarına çevirir."""
    import netCDF4

    with netCDF4.Dataset(path) as ds:
        p = ds.groups["PRODUCT"]
        lat = np.ma.filled(p["latitude"][0], np.nan).astype(float)
        lon = np.ma.filled(p["longitude"][0], np.nan).astype(float)
        box = (lon >= BBOX[0]) & (lon < BBOX[2]) & (lat >= BBOX[1]) & (lat < BBOX[3])
        if not box.any():
            return None
        rows = np.where(box.any(axis=1))[0]
        r0, r1 = rows.min(), rows.max() + 1
        qa = np.ma.filled(p["qa_value"][0, r0:r1, :], 0).astype(float)
        ch4 = np.ma.filled(p["methane_mixing_ratio_bias_corrected"][0, r0:r1, :], np.nan).astype(float)

    lat, lon, box = lat[r0:r1], lon[r0:r1], box[r0:r1]
    ok = box & (qa > QA_MIN) & np.isfinite(ch4) & (ch4 > 1000) & (ch4 < 3000)
    if not ok.any():
        return None
    j, i = cell_index(lon[ok], lat[ok])
    flat = j * NX + i
    s = np.bincount(flat, weights=ch4[ok], minlength=NX * NY)
    n = np.bincount(flat, minlength=NX * NY)
    return s, n


def _store_cells(con, day, source, s, n):
    mask = turkey_cell_mask().ravel()
    idx = np.nonzero(n)[0]
    con.executemany(
        """INSERT INTO cells(day, source, j, i, tr, s, n) VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(day, source, j, i) DO UPDATE SET s = s + excluded.s, n = n + excluded.n""",
        [(day, source, int(k // NX), int(k % NX), int(mask[k]), float(s[k]), int(n[k])) for k in idx],
    )
    return int(n[idx].sum())


def _ingest_product(product, source, start, end):
    import earthaccess

    short_name, version = product
    granules = earthaccess.search_data(
        short_name=short_name, version=version,
        bounding_box=BBOX, temporal=(start.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ")),
    )
    with db.read() as con:
        done = {r[0] for r in con.execute("SELECT id FROM granules WHERE product = ?", (short_name,))}
    todo = [g for g in granules if _granule_id(g) not in done]
    log.info("%s: %d granül bulundu, %d yeni", short_name, len(granules), len(todo))

    added = 0
    for g in todo:
        gid = _granule_id(g)
        day = _granule_start(g).date().isoformat()
        tmp = tempfile.mkdtemp(dir=TMP_DIR)
        try:
            files = earthaccess.download([g], local_path=tmp, threads=1)
            nc = [f for f in map(str, files) if f.endswith(".nc")]
            if not nc:
                raise RuntimeError("indirme boş döndü (kimlik bilgilerini kontrol edin)")
            res = _grid_file(nc[0])
            with db.tx() as con:
                npix = _store_cells(con, day, source, *res) if res else 0
                con.execute(
                    "INSERT OR REPLACE INTO granules(id, product, day, n_pixels, processed_at) VALUES(?,?,?,?,?)",
                    (gid, short_name, day, npix, _now().isoformat()),
                )
            added += 1
            log.info("  %s -> %s, %d piksel", gid, day, npix)
        except Exception as e:
            log.exception("  %s işlenemedi", gid)
            db.set_meta("last_error", f"{_now().isoformat()} {gid}: {e}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return added


def _prune(oldest):
    """Veri penceresinin dışında kalan günleri siler, veritabanı küçük kalır."""
    day = oldest.date().isoformat()
    with db.tx() as con:
        removed = con.execute("DELETE FROM cells WHERE day < ?", (day,)).rowcount
        con.execute("DELETE FROM granules WHERE day < ?", (day,))
    if removed:
        log.info("%s öncesine ait %d grid satırı silindi", day, removed)
        # SQLite DELETE ile dosyayı küçültmez; VACUUM işlem (transaction) dışında çalışmalı
        with db.tx() as con:
            con.execute("VACUUM")


def _last_tropomi_day():
    with db.read() as con:
        return con.execute("SELECT MAX(day) FROM cells WHERE tr = 1").fetchone()[0]


def ingest_tropomi():
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    if not login():
        log.warning("Earthdata kimlik bilgisi yok, TROPOMI atlandı")
        sources.failed("tropomi", "Earthdata kimlik bilgisi tanımlı değil veya reddedildi", auth="missing")
        return
    now = _now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = today - timedelta(days=NRT_DAYS - 1)
    oldest = today - timedelta(days=BACKFILL_DAYS - 1)
    _prune(oldest)
    db.set_meta("tropomi_started", now.isoformat())
    try:
        if oldest < cutoff:
            _ingest_product(TROPOMI_OFFL, "OFFL", oldest, cutoff)
        _ingest_product(TROPOMI_NRT, "NRTI", cutoff, now)
    except Exception as e:  # granül araması başarısızsa (ağ, CMR) durum kaydına yaz, zamanlayıcı sonraki turu dener
        log.exception("TROPOMI araması başarısız")
        sources.failed("tropomi", e, auth="ok", last_observation=_last_tropomi_day())
        return
    db.set_meta("tropomi_finished", _now().isoformat())
    sources.succeeded("tropomi", auth="ok", last_observation=_last_tropomi_day())


# --- Carbon Mapper ---------------------------------------------------------------

def _fetch_carbonmapper_pages(token):
    items, offset = [], 0
    headers = {"User-Agent": "metan-turkiye/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    while True:
        query = urllib.parse.urlencode(
            [("bbox", v) for v in BBOX]
            + [("plume_gas", "CH4"), ("status", "published"), ("limit", CARBONMAPPER_PAGE), ("offset", offset)]
        )
        req = urllib.request.Request(f"{CARBONMAPPER_API}?{query}", headers=headers)
        with urllib.request.urlopen(req, timeout=60) as r:
            page = json.load(r)["items"]
        items += page
        if len(page) < CARBONMAPPER_PAGE:
            return items
        offset += CARBONMAPPER_PAGE


def _fetch_carbonmapper():
    """Kutuyla kesişen, yayımlanmış tüm CH4 bulutları; sayfa sayfa. (kayıtlar, token durumu) döner.

    Token tanımlıysa gönderilir. Reddedilirse katalog herkese açık olduğu için tokensız tekrar denenir
    ve durum "rejected" olarak raporlanır; böylece süresi dolan bir token veri akışını durdurmaz."""
    if not CARBON_MAPPER_API_TOKEN:
        return _fetch_carbonmapper_pages(None), "unset"
    try:
        return _fetch_carbonmapper_pages(CARBON_MAPPER_API_TOKEN), "accepted"
    except urllib.error.HTTPError as e:
        if e.code not in (401, 403):
            raise
        log.warning("Carbon Mapper tokenı reddedildi (HTTP %s); herkese açık katalogla devam ediliyor", e.code)
        return _fetch_carbonmapper_pages(None), "rejected"


def _plume_row(p, obs):
    lon, lat = obs["lon"], obs["lat"]
    e = obs["emission"]
    return (
        obs["id"], obs["observedAt"] or p.get("scene_timestamp"), lat, lon, int(in_turkey([lon], [lat])[0]),
        e["value"] if e else None, (e["uncertainty"] or {}).get("value") if e else None,
        p.get("sector"), p.get("platform"), p.get("wind_speed_avg_auto"),
        p.get("plume_png"), json.dumps(p["plume_bounds"]) if p.get("plume_bounds") else None,
        json.dumps(obs, ensure_ascii=False),
    )


def _first_seen(ids, now):
    """Yeni kimlikleri plume_seen'e ekler, her kimliğin ilk alınma zamanını döner.
    Tablo boşsa (ilk doldurma) kayıtlar baseline olarak işaretlenir ve uyarı üretmez."""
    with db.tx() as con:
        baseline = int(con.execute("SELECT COUNT(*) FROM plume_seen").fetchone()[0] == 0)
        con.executemany("INSERT OR IGNORE INTO plume_seen(id, first_seen, baseline) VALUES(?,?,?)",
                        [(i, now, baseline) for i in ids])
        return dict(con.execute("SELECT id, first_seen FROM plume_seen").fetchall())


def _record_versions(obs_list, now):
    """Her kaydın sağlayıcı değişiklik zamanını saklar. Aynı kayıt yeniden alındığında yeni olay üretmez;
    yalnızca modified değişirse yeni satır eklenir (izleme olaylarında 'kayıt güncellendi')."""
    rows = [(o["id"], o["modifiedAt"], o.get("processingVersion"), (o.get("emission") or {}).get("value"), now)
            for o in obs_list if o.get("modifiedAt")]
    with db.tx() as con:
        con.executemany("INSERT OR IGNORE INTO plume_versions(id, modified, version, emission, seen_at) VALUES(?,?,?,?,?)",
                        rows)


def ingest_plumes(protected=None):
    """Carbon Mapper bulut kataloğu (Tanager + EMIT). Görüntü bağlantıları imzalı ve süreli olduğu,
    Carbon Mapper bulutları geri çekip güncelleyebildiği için tablo her çalışmada baştan yazılır.

    Saklama: tam katalog yalnızca bellekte (geçici işleme alanı) pik sınıflandırması için döndürülür; kalıcı
    tabloya 90 günlük pencere + kalıcı arşivde korunan kayıtlar yazılır. Böylece silinen eski normal kayıtlar
    her gün geri dolmaz. Dönüş: tüm kayıtların (id, png, bounds, obs_json, tr) satırları ya da hata durumunda None."""
    from . import retention
    try:
        raw, token_state = _fetch_carbonmapper()
    except Exception as e:
        log.exception("Carbon Mapper kataloğu alınamadı")
        db.set_meta("last_error", f"{_now().isoformat()} carbonmapper: {e}")
        sources.failed("carbonmapper", e)
        return None

    now = _now().isoformat()
    protected = retention.protected_ids() if protected is None else protected
    pairs = [(p, observations.normalize_cm_plume(p)) for p in raw]
    invalid = sum(1 for _, o in pairs if o is None)
    pairs = [(p, o) for p, o in pairs if o is not None]
    raw_of = {id(o): p for p, o in pairs}
    unique, dropped = observations.dedupe([o for _, o in pairs])
    pairs = [(raw_of[id(o)], o) for o in unique]
    all_rows = [_plume_row(p, o) for p, o in pairs]
    pairs = [(p, o) for p, o in pairs if retention.keep_on_ingest(o, protected)]

    seen = _first_seen([o["id"] for _, o in pairs], now)
    for _, o in pairs:
        o["ingestedAt"] = observations.parse_time(seen.get(o["id"]))
    rows = [_plume_row(p, o) for p, o in pairs]
    _record_versions([o for _, o in pairs], now)
    with db.tx() as con:
        con.execute("DELETE FROM cm_plumes")
        con.executemany(
            """INSERT OR REPLACE INTO cm_plumes(id, observed, lat, lon, tr, emission, uncertainty,
               sector, platform, wind_speed, png, bounds, obs) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        last_obs, last_pub = con.execute(
            "SELECT MAX(json_extract(obs, '$.observedAt')), MAX(json_extract(obs, '$.publishedAt')) "
            "FROM cm_plumes WHERE tr = 1").fetchone()
    db.set_meta("plumes_finished", _now().isoformat())
    sources.succeeded("carbonmapper", token=token_state, records=len(all_rows), stored=len(rows), duplicates=dropped,
                      invalid=invalid, last_observation=last_obs, last_published=last_pub, last_product_at=now)
    log.info("Carbon Mapper: %d bulut alındı, %d saklandı (%d tekrar, %d geçersiz; token: %s)",
             len(all_rows), len(rows), dropped, invalid, token_state)
    ingest_cm_sources()
    # _plume_row: (id, observed, lat, lon, tr, emission, unc, sector, platform, wind, png, bounds, obs)
    return all_rows


def persist_protected_cm(rows, protected):
    """Aynı turda kalıcı arşive giren eski kayıtları tabloya ekler (sonraki turların seri hesabı eksik kalmasın)."""
    keep = [r for r in rows or [] if r[0] in protected]
    with db.tx() as con:
        con.executemany(
            """INSERT OR IGNORE INTO cm_plumes(id, observed, lat, lon, tr, emission, uncertainty,
               sector, platform, wind_speed, png, bounds, obs) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", keep)
    return len(keep)


def ingest_cm_sources():
    """Carbon Mapper kaynak özetleri. Başarısız olursa bulut verisi etkilenmez; durum ayrı kaynak olarak raporlanır."""
    query = urllib.parse.urlencode([("bbox", v) for v in BBOX] + [("plume_gas", "CH4")])
    headers = {"User-Agent": "metan-turkiye/1.1"}
    try:
        req = urllib.request.Request(f"{CARBONMAPPER_SOURCES_API}?{query}", headers=headers)
        with urllib.request.urlopen(req, timeout=90) as r:
            features = json.load(r).get("features", [])
    except Exception as e:
        log.exception("Carbon Mapper kaynak özetleri alınamadı")
        sources.failed("carbonmapper_sources", e)
        return
    rows, invalid = [], 0
    for f in features:
        o = observations.normalize_cm_source(f)
        if not o:
            invalid += 1
            continue
        rows.append((o["id"], o["lat"], o["lon"], int(in_turkey([o["lon"]], [o["lat"]])[0]),
                     json.dumps(o, ensure_ascii=False)))
    with db.tx() as con:
        con.execute("DELETE FROM cm_sources")
        con.executemany("INSERT OR REPLACE INTO cm_sources(id, lat, lon, tr, obs) VALUES(?,?,?,?,?)", rows)
    sources.succeeded("carbonmapper_sources", records=len(rows), invalid=invalid)