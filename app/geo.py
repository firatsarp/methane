"""Türkiye sınırı ile nokta-içinde-mi testleri ve grid maskesi."""
import json
from functools import lru_cache

import numpy as np

from .config import BBOX, GRID, NX, NY, STATIC_DIR


@lru_cache(maxsize=1)
def _polygons():
    gj = json.loads((STATIC_DIR / "turkey.geojson").read_text(encoding="utf-8"))
    polys = []
    for feat in gj["features"]:
        g = feat["geometry"]
        parts = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
        for rings in parts:
            polys.append([np.asarray(r, dtype=float) for r in rings])
    return polys


def _ring_contains(ring, lon, lat):
    x, y = ring[:, 0], ring[:, 1]
    inside = np.zeros(lon.shape, dtype=bool)
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi, xj, yj = x[i], y[i], x[j], y[j]
        if yi != yj:
            cross = ((yi > lat) != (yj > lat)) & (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi)
            inside ^= cross
        j = i
    return inside


def in_turkey(lon, lat):
    lon = np.asarray(lon, dtype=float)
    lat = np.asarray(lat, dtype=float)
    result = np.zeros(lon.shape, dtype=bool)
    for rings in _polygons():
        inside = _ring_contains(rings[0], lon, lat)
        for hole in rings[1:]:
            inside &= ~_ring_contains(hole, lon, lat)
        result |= inside
    return result


@lru_cache(maxsize=1)
def turkey_cell_mask():
    """(NY, NX) boolean: hücre merkezi Türkiye içinde mi."""
    lon_c = BBOX[0] + (np.arange(NX) + 0.5) * GRID
    lat_c = BBOX[1] + (np.arange(NY) + 0.5) * GRID
    lon2, lat2 = np.meshgrid(lon_c, lat_c)
    return in_turkey(lon2, lat2)


def cell_index(lon, lat):
    i = np.floor((lon - BBOX[0]) / GRID).astype(int)
    j = np.floor((lat - BBOX[1]) / GRID).astype(int)
    return j, i
