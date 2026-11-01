import json
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone

import numpy as np
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import alerts, catalog, cds, climate, db, emit, imagery, ingest, passes, store, weather
from .config import BBOX, BACKFILL_DAYS, GRID, INGEST_INTERVAL_MIN, STATIC_DIR
from .geo import turkey_cell_mask

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

scheduler = BackgroundScheduler(timezone="UTC")

# Meteoroloji/iklim isteklerinin kabul edildiği alan (Türkiye ve çevresi); kota kötüye kullanımını sınırlar
WEATHER_BOX = (20.0, 30.0, 50.0, 46.0)


def _seed_climate():
    """İlk çalışmada en çok bulut kaydı bağlanmış Carbon Mapper kaynağının konumu için iklim işini başlatır."""
    _, srcs = catalog.load("tr")
    if not srcs:
        return
    best = max(srcs.values(), key=lambda s: len(s.get("plumeIds", [])))
    climate.seed_default(best["lat"], best["lon"])


@asynccontextmanager
async def lifespan(app):
    db.init()
    now = datetime.now(timezone.utc)
    after = lambda s: now + timedelta(seconds=s)  # noqa: E731
    scheduler.add_job(ingest.ingest_tropomi, "interval", minutes=INGEST_INTERVAL_MIN,
                      next_run_time=after(5), id="tropomi", coalesce=True)
    scheduler.add_job(ingest.ingest_plumes, "interval", hours=24, next_run_time=after(20), id="plumes", coalesce=True)
    scheduler.add_job(emit.ingest, "interval", hours=24, next_run_time=after(45), id="emit", coalesce=True)
    scheduler.add_job(_seed_climate, "date", run_date=after(120), id="climate_seed")
    scheduler.add_job(climate.process_jobs, "interval", minutes=2, next_run_time=after(150), id="climate",
                      coalesce=True, max_instances=1)
    scheduler.add_job(cds.process_jobs, "interval", minutes=5, next_run_time=after(90), id="cds",
                      coalesce=True, max_instances=1)
    scheduler.add_job(cds.check_connection, "interval", hours=12, next_run_time=after(60), id="cds_check",
                      coalesce=True)
    scheduler.add_job(imagery.process_jobs, "interval", minutes=1, next_run_time=after(30), id="imagery",
                      coalesce=True, max_instances=1)
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Türkiye Metan İzleyici", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000)


@app.middleware("http")
async def cache_headers(request, call_next):
    """HTML/JS/CSS/JSON her açılışta yeniden doğrulanır (ETag); eski içerik sürümü önbellekten geri gelmez."""
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/api/imagery/") and path.endswith(".png"):
        response.headers["Cache-Control"] = "public, max-age=86400"
    elif path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    elif path == "/" or path.endswith((".html", ".js", ".css", ".json")):
        response.headers["Cache-Control"] = "no-cache"
    return response


def _check_point(lat, lon):
    if not (WEATHER_BOX[1] <= lat <= WEATHER_BOX[3] and WEATHER_BOX[0] <= lon <= WEATHER_BOX[2]):
        raise HTTPException(400, "Konum Türkiye ve çevresi dışında.")


def _parse_day(s):
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        raise HTTPException(400, f"Geçersiz tarih: {s}")


@app.get("/api/days")
def days():
    with db.read() as con:
        stats = {r[0]: r[1:] for r in con.execute(
            """WITH best AS (SELECT day, MAX(source) AS source FROM cells GROUP BY day)
               SELECT c.day, b.source, COUNT(*), SUM(c.s) / SUM(c.n)
               FROM cells c JOIN best b ON c.day = b.day AND c.source = b.source
               WHERE c.tr = 1 GROUP BY c.day""")}
        processed = [r[0] for r in con.execute("SELECT DISTINCT day FROM granules ORDER BY day")]
    total = int(turkey_cell_mask().sum())
    out = []
    for d in sorted(set(processed) | set(stats)):
        source, cells, mean = stats.get(d, (None, 0, None))
        out.append({
            "day": d, "source": source, "cells": cells,
            "coverage": round(cells / total, 3),
            "mean": round(mean, 1) if mean else None,
        })
    return out


@app.get("/api/tropomi")
def tropomi(start: str, end: str | None = None, scope: str = Query("tr", pattern="^(tr|all)$")):
    start = _parse_day(start)
    end = _parse_day(end) if end else start
    with db.read() as con:
        rows = con.execute(
            """WITH best AS (SELECT day, MAX(source) AS source FROM cells WHERE day BETWEEN ? AND ? GROUP BY day)
               SELECT c.j, c.i, SUM(c.s), SUM(c.n), MAX(c.tr)
               FROM cells c JOIN best b ON c.day = b.day AND c.source = b.source
               WHERE (? = 'all' OR c.tr = 1)
               GROUP BY c.j, c.i""",
            (start, end, scope),
        ).fetchall()
    cells = [[j, i, round(s / n, 1), n] for j, i, s, n, _ in rows]
    tr_vals = np.array([s / n for _, _, s, n, tr in rows if tr], dtype=float)
    total = int(turkey_cell_mask().sum())
    stats = None
    if len(tr_vals):
        stats = {
            "mean": round(float(tr_vals.mean()), 1),
            "median": round(float(np.median(tr_vals)), 1),
            "p02": round(float(np.percentile(tr_vals, 2)), 1),
            "p98": round(float(np.percentile(tr_vals, 98)), 1),
            "max": round(float(tr_vals.max()), 1),
            "cells": int(len(tr_vals)),
            "coverage": round(len(tr_vals) / total, 3),
        }
    return {
        "start": start, "end": end, "unit": "ppb",
        "grid": GRID, "lon0": BBOX[0], "lat0": BBOX[1],
        "cells": cells, "stats": stats,
    }


@app.get("/api/plumes")
def plumes(scope: str = Query("tr", pattern="^(tr|all)$")):
    with db.read() as con:
        rows = con.execute(
            """SELECT id, observed, lat, lon, emission, uncertainty, sector, platform, wind_speed, png, bounds, obs
               FROM cm_plumes WHERE (? = 'all' OR tr = 1) ORDER BY observed DESC""",
            (scope,),
        ).fetchall()
    features = []
    for pid, observed, lat, lon, emission, uncertainty, sector, platform, wind_speed, png, bounds, obs in rows:
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "id": pid, "observed": observed, "emission": emission, "uncertainty": uncertainty,
                "sector": sector, "platform": platform, "wind_speed": wind_speed,
                "png": png, "bounds": json.loads(bounds) if bounds else None,
                # metan-icerik.json observationSchema biçiminde normalleştirilmiş kayıt
                "observation": json.loads(obs),
            },
        })
    return {"type": "FeatureCollection", "features": features}


@app.get("/api/passes")
def get_passes():
    return passes.passes()


@app.get("/api/positions")
def get_positions():
    return passes.positions()


@app.get("/api/status")
def status():
    meta = db.get_meta()
    with db.read() as con:
        n_gran = con.execute("SELECT COUNT(*) FROM granules").fetchone()[0]
        n_plumes = con.execute("SELECT COUNT(*) FROM cm_plumes WHERE tr = 1").fetchone()[0]
    job = scheduler.get_job("tropomi")
    return {
        "auth": meta.get("auth", "unknown"),
        "tropomi_finished": meta.get("tropomi_finished"),
        "plumes_finished": meta.get("plumes_finished"),
        "last_error": meta.get("last_error"),
        "granules": n_gran,
        "plumes": n_plumes,
        "backfill_days": BACKFILL_DAYS,
        "next_ingest": job.next_run_time.isoformat() if job and job.next_run_time else None,
    }


@app.get("/api/sources")
def get_sources():
    return alerts.source_status()


@app.get("/api/alerts")
def get_alerts():
    return alerts.alerts()


@app.get("/api/data-status")
def data_status():
    with db.read() as con:
        jobs = {f"{k}:{s}": n for k, s, n in con.execute("SELECT kind, state, COUNT(*) FROM jobs GROUP BY kind, state")}
    return {"generatedAt": store.iso(store.now()), "sources": alerts.source_status(), "jobs": jobs}


@app.get("/api/monitoring-events")
def monitoring_events():
    return alerts.events()


# ---- Gözlemler --------------------------------------------------------------------

@app.get("/api/observations")
def observations(
    sector: str | None = None, sensor: str | None = None,
    days: int | None = Query(None, ge=1, le=3650), since: str | None = None, until: str | None = None,
    emission_only: bool = False, sort: str = Query("observed", pattern="^(observed|published|emission)$"),
    limit: int = Query(500, ge=1, le=2000), offset: int = Query(0, ge=0),
    scope: str = Query("tr", pattern="^(tr|all)$"),
):
    items, _ = catalog.load(scope)
    rows = catalog.apply_filters(items, sector=sector, sensor=sensor, days=days, since=since, until=until,
                                 emission_only=emission_only, sort=sort)
    return {
        "status": "ready" if items else "empty",
        "total": len(items), "filtered": len(rows), "offset": offset, "limit": limit,
        "facets": catalog.facets(items),
        "filters": {"sector": sector, "sensor": sensor, "days": days, "since": since, "until": until,
                    "emissionOnly": emission_only, "sort": sort, "timeField": "observedAt"},
        "items": [catalog.public(o) for o in rows[offset:offset + limit]],
    }


@app.get("/api/observations/{obs_id:path}")
def observation(obs_id: str):
    o, src, related = catalog.get(obs_id)
    if not o:
        raise HTTPException(404, "Gözlem bulunamadı")
    return {"status": "ready", "observation": catalog.public(o), "sourceSummary": src,
            "relatedObservations": [catalog.public(x) for x in related]}


@app.get("/api/source-summaries")
def source_summaries(scope: str = Query("tr", pattern="^(tr|all)$")):
    _, srcs = catalog.load(scope)
    return {"status": "ready" if srcs else "empty",
            "items": sorted(srcs.values(), key=lambda s: -(len(s.get("plumeIds", []))))}


@app.get("/api/source-summaries/{source_id:path}")
def source_summary(source_id: str):
    s, plumes = catalog.get_source(source_id)
    if not s:
        raise HTTPException(404, "Kaynak özeti bulunamadı")
    return {"status": "ready", "source": s, "observations": [catalog.public(x) for x in plumes]}


# ---- Meteoroloji, iklim, yüzey görüntüsü ------------------------------------------------

@app.get("/api/weather/observation/{obs_id:path}")
def weather_observation(obs_id: str):
    o, _, _ = catalog.get(obs_id)
    if not o:
        raise HTTPException(404, "Gözlem bulunamadı")
    return weather.observation_weather(o)


@app.get("/api/weather/forecast")
def weather_forecast(lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180)):
    _check_point(lat, lon)
    return weather.forecast(lat, lon)


@app.get("/api/climate")
def get_climate(lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180),
                period: str = Query("1991-2020", pattern="^1991-2020$"),
                compare: str | None = Query(None, pattern=r"^\d{4}-\d{2}$")):
    _check_point(lat, lon)
    return climate.climate(lat, lon, compare)


@app.get("/api/imagery/{obs_id:path}")
def get_imagery(obs_id: str):
    png = obs_id.endswith(".png")
    oid = obs_id[:-4] if png else obs_id
    o, _, _ = catalog.get(oid)
    if not o:
        raise HTTPException(404, "Gözlem bulunamadı")
    if png:
        path = imagery.image_path(o["id"])
        if not path.exists():
            raise HTTPException(404, "Görüntü henüz yok")
        return FileResponse(path, media_type="image/png")
    return imagery.status_for(o)


# Konuma özel eski rehber sayfası kaldırıldı; eski bağlantılar genel yüksek değer rehberine kalıcı yönlenir
RETIRED_PAGES = {"/seymen.html": "/yuksek-degerleri-yorumlamak.html"}


@app.get("/seymen.html", include_in_schema=False)
def retired_seymen():
    return RedirectResponse(RETIRED_PAGES["/seymen.html"], status_code=308)


@app.get("/healthz")
def healthz():
    return {"ok": True}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
