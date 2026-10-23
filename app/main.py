import json
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone

import numpy as np
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from . import db, ingest, passes
from .config import BBOX, BACKFILL_DAYS, GRID, INGEST_INTERVAL_MIN, STATIC_DIR
from .geo import turkey_cell_mask

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

scheduler = BackgroundScheduler(timezone="UTC")


@asynccontextmanager
async def lifespan(app):
    db.init()
    now = datetime.now(timezone.utc)
    scheduler.add_job(ingest.ingest_tropomi, "interval", minutes=INGEST_INTERVAL_MIN,
                      next_run_time=now + timedelta(seconds=5), id="tropomi", coalesce=True)
    scheduler.add_job(ingest.ingest_emit, "interval", hours=24,
                      next_run_time=now + timedelta(seconds=20), id="emit", coalesce=True)
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Türkiye Metan İzleyici", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000)


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
            "SELECT id, observed, lat, lon, max_ppmm, props, geometry FROM plumes WHERE (? = 'all' OR tr = 1) ORDER BY observed DESC",
            (scope,),
        ).fetchall()
    features = []
    for pid, observed, lat, lon, max_ppmm, props, geometry in rows:
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "id": pid, "observed": observed, "max_ppmm": max_ppmm,
                "details": json.loads(props or "{}"),
                "outline": json.loads(geometry) if geometry else None,
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
        n_plumes = con.execute("SELECT COUNT(*) FROM plumes WHERE tr = 1").fetchone()[0]
    job = scheduler.get_job("tropomi")
    return {
        "auth": meta.get("auth", "unknown"),
        "tropomi_finished": meta.get("tropomi_finished"),
        "last_error": meta.get("last_error"),
        "granules": n_gran,
        "plumes": n_plumes,
        "backfill_days": BACKFILL_DAYS,
        "next_ingest": job.next_run_time.isoformat() if job and job.next_run_time else None,
    }


@app.get("/healthz")
def healthz():
    return {"ok": True}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
