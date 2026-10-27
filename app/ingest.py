"""NASA Earthdata'dan TROPOMI metan ve EMIT metan bulutu verilerini çeker."""
import json
import logging
import shutil
import tempfile
from datetime import datetime, timedelta, timezone

import numpy as np

from . import db
from .config import (
    BBOX, EMIT_PLUMES, NRT_DAYS, NX, NY, QA_MIN, TMP_DIR, BACKFILL_DAYS,
    TROPOMI_NRT, TROPOMI_OFFL,
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


def ingest_tropomi():
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    if not login():
        log.warning("Earthdata kimlik bilgisi yok, TROPOMI atlandı")
        return
    now = _now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = today - timedelta(days=NRT_DAYS - 1)
    oldest = today - timedelta(days=BACKFILL_DAYS - 1)
    _prune(oldest)
    db.set_meta("tropomi_started", now.isoformat())
    if oldest < cutoff:
        _ingest_product(TROPOMI_OFFL, "OFFL", oldest, cutoff)
    _ingest_product(TROPOMI_NRT, "NRTI", cutoff, now)
    db.set_meta("tropomi_finished", _now().isoformat())


# --- EMIT ----------------------------------------------------------------------

def _umm_polygon(g):
    try:
        geom = g["umm"]["SpatialExtent"]["HorizontalSpatialDomain"]["Geometry"]
        pts = geom["GPolygons"][0]["Boundary"]["Points"]
        return {"type": "Polygon", "coordinates": [[[p["Longitude"], p["Latitude"]] for p in pts]]}
    except (KeyError, IndexError):
        return None


def _parse_plume_meta(path):
    """EMIT CH4PLMMETA GeoJSON: poligon + maksimum konsantrasyon noktası ve özellikler."""
    gj = json.loads(open(path, encoding="utf-8").read())
    props, polygon, point = {}, None, None
    for f in gj.get("features", []):
        props.update({k: v for k, v in (f.get("properties") or {}).items() if k != "style"})
        geom = f.get("geometry") or {}
        if geom.get("type") in ("Polygon", "MultiPolygon"):
            polygon = geom
        elif geom.get("type") == "Point":
            point = geom["coordinates"][:2]
    return props, polygon, point


def _num(props, *keys):
    for k in keys:
        try:
            return float(props[k])
        except (KeyError, TypeError, ValueError):
            pass
    return None


def ingest_emit():
    """EMIT metan bulutu kataloğu. Metadata herkese açık; detaylı özellikler giriş gerektirir."""
    import earthaccess

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    authed = login()
    short_name, version = EMIT_PLUMES
    granules = earthaccess.search_data(short_name=short_name, version=version, bounding_box=BBOX)
    with db.read() as con:
        done = {r[0] for r in con.execute("SELECT id FROM plumes WHERE props != '{}'")}
    for g in granules:
        gid = _granule_id(g)
        if gid in done:
            continue
        props, polygon, point = {}, _umm_polygon(g), None
        if authed:
            links = [u for u in g.data_links() if "PLMMETA" in u and u.endswith(".json")]
            if links:
                tmp = tempfile.mkdtemp(dir=TMP_DIR)
                try:
                    files = earthaccess.download(links[:1], local_path=tmp, threads=1)
                    if files:
                        props, poly2, point = _parse_plume_meta(str(files[0]))
                        polygon = poly2 or polygon
                except Exception:
                    log.exception("EMIT metadata indirilemedi: %s", gid)
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)
        lat = _num(props, "Latitude of max concentration")
        lon = _num(props, "Longitude of max concentration")
        if lat is None or lon is None:
            if point:
                lon, lat = point
            elif polygon:
                ring = np.asarray(polygon["coordinates"][0] if polygon["type"] == "Polygon"
                                  else polygon["coordinates"][0][0], dtype=float)
                lon, lat = ring[:, 0].mean(), ring[:, 1].mean()
            else:
                continue
        tr = bool(in_turkey([lon], [lat])[0])
        observed = props.get("UTC Time Observed") or _granule_start(g).isoformat()
        with db.tx() as con:
            con.execute(
                "INSERT OR REPLACE INTO plumes(id, observed, lat, lon, tr, max_ppmm, props, geometry) VALUES(?,?,?,?,?,?,?,?)",
                (gid, observed, lat, lon, int(tr),
                 _num(props, "Max Plume Concentration (ppm m)"),
                 json.dumps(props, ensure_ascii=False), json.dumps(polygon) if polygon else None),
            )
    db.set_meta("emit_finished", _now().isoformat())
