"""Sağlayıcının sayısal artış GeoTIFF'i için okuyucu, maske ve morfoloji (yeni bağımlılık gerektirmez).

Desteklenen: tek bant, tamsayı/float örnekler; sıkıştırmasız veya DEFLATE; öngörücü 1/2/3; şerit veya döşeme;
GeoTIFF ModelPixelScale + ModelTiepoint; EPSG:4326 ve WGS84 UTM (326xx/327xx).

Bilimsel sınır: Dosyanın birimi metadata'da yoksa birim None kalır ve toplamlar "dosya birimi" olarak raporlanır.
Bu modül yeni bir salım (Q) veya IME hesabı yapmaz; Q için kalibre edilmiş Ueff/L ve birimi doğrulanmış girdi gerekir.
Yalnızca RGB PNG varsa kullanılmaz: renkten sayısal nicelik geri çıkarılmaz.
"""
import math
import re
import struct
import zlib

import numpy as np

METHOD = "raster-morphology-v1"

_TYPES = {1: ("B", 1), 2: ("s", 1), 3: ("H", 2), 4: ("I", 4), 5: ("II", 8), 6: ("b", 1), 8: ("h", 2), 9: ("i", 4),
          11: ("f", 4), 12: ("d", 8), 16: ("Q", 8)}
TAGS = {256: "width", 257: "height", 258: "bits", 259: "compression", 273: "strip_offsets", 277: "spp",
        278: "rows_per_strip", 279: "strip_counts", 284: "planar", 317: "predictor", 322: "tile_w", 323: "tile_h",
        324: "tile_offsets", 325: "tile_counts", 339: "sample_format", 33550: "pixel_scale", 33922: "tiepoint",
        34735: "geokeys", 34737: "geo_ascii", 42112: "gdal_metadata", 42113: "gdal_nodata"}


class RasterError(Exception):
    pass


def _tags(buf):
    order = buf[:2]
    if order not in (b"II", b"MM"):
        raise RasterError("TIFF değil")
    e = "<" if order == b"II" else ">"
    if struct.unpack(e + "H", buf[2:4])[0] != 42:
        raise RasterError("BigTIFF veya geçersiz TIFF desteklenmiyor")
    off = struct.unpack(e + "I", buf[4:8])[0]
    n = struct.unpack(e + "H", buf[off:off + 2])[0]
    out = {}
    for k in range(n):
        tag, typ, cnt, val = struct.unpack(e + "HHII", buf[off + 2 + 12 * k: off + 14 + 12 * k])
        if typ not in _TYPES:
            continue
        fmt, size = _TYPES[typ]
        nbytes = size * cnt
        data = buf[off + 10 + 12 * k: off + 14 + 12 * k][:nbytes] if nbytes <= 4 else buf[val: val + nbytes]
        if typ == 2:
            v = data.rstrip(b"\0").decode("latin1")
        else:
            v = struct.unpack(e + fmt * cnt, data)
        out[TAGS.get(tag, tag)] = v
    return out, e


def _first(t, k, default=None):
    v = t.get(k)
    return v[0] if isinstance(v, tuple) and v else (default if v is None else v)


def _decode_block(raw, predictor, width, rows, dtype):
    """Sıkıştırması açılmış döşeme/şerit -> (rows, width) dizi. Öngörücü 2: örnek sözcükleri üzerinde yatay fark
    (float için de tamsayı sözcük olarak, libtiff horAcc); 3: kayan nokta bayt düzlemi öngörücüsü (fpAcc)."""
    bps = dtype.itemsize
    if predictor == 2:
        words = np.frombuffer(raw, dtype=np.dtype(f"{dtype.byteorder}u{bps}"))[: width * rows]
        acc = np.cumsum(words.reshape(rows, width).astype(f"=u{bps}"), axis=1, dtype=f"=u{bps}")
        return acc.view(f"={dtype.kind}{bps}")
    if predictor == 3:
        b = np.cumsum(np.frombuffer(raw, dtype=np.uint8)[: width * rows * bps].reshape(rows, width * bps),
                      axis=1, dtype=np.uint8)
        planes = b.reshape(rows, bps, width)
        # Bayt düzlemleri en anlamlı bayttan başlar; küçük-endian sözcüğe çevrilir
        out = np.stack([planes[:, bps - 1 - k, :] for k in range(bps)], axis=-1)
        return out.reshape(rows, width * bps).view(f"<{dtype.kind}{bps}")
    return np.frombuffer(raw, dtype=dtype)[: width * rows].reshape(rows, width)


def read_geotiff(buf):
    """(dizi float64 [satır, sütun], georef sözlüğü, metadata). Tanınmayan biçimde RasterError."""
    t, e = _tags(buf)
    width, height = _first(t, "width"), _first(t, "height")
    spp = _first(t, "spp", 1)
    if spp != 1:
        raise RasterError(f"Tek bant bekleniyordu (spp={spp}); RGB görüntüden sayısal değer çıkarılmaz")
    bits = _first(t, "bits", 8)
    fmt = _first(t, "sample_format", 1)
    comp = _first(t, "compression", 1)
    pred = _first(t, "predictor", 1)
    if comp not in (1, 8, 32946):
        raise RasterError(f"Sıkıştırma {comp} desteklenmiyor")
    bps = bits // 8
    kind = {1: "u", 2: "i", 3: "f"}[fmt]
    dtype = np.dtype(f"{e}{kind}{bps}")
    if "tile_w" in t:
        tw, th = _first(t, "tile_w"), _first(t, "tile_h")
        offsets, counts = t["tile_offsets"], t["tile_counts"]
    else:
        tw, th = width, _first(t, "rows_per_strip", height)
        offsets, counts = t["strip_offsets"], t["strip_counts"]
    across = math.ceil(width / tw)
    arr = np.full((height, width), np.nan)
    for idx, (o, c) in enumerate(zip(offsets, counts)):
        raw = buf[o: o + c]
        if comp in (8, 32946):
            raw = zlib.decompress(raw)
        rows = min(th, len(raw) // (tw * bps))
        block = _decode_block(raw, pred, tw, rows, dtype).astype(float)
        r0, c0 = (idx // across) * th, (idx % across) * tw
        r1, c1 = min(height, r0 + rows), min(width, c0 + tw)
        arr[r0:r1, c0:c1] = block[: r1 - r0, : c1 - c0]
    nodata = t.get("gdal_nodata")
    if isinstance(nodata, str) and nodata.strip().lower() not in ("nan", ""):
        try:
            arr[arr == float(nodata)] = np.nan
        except ValueError:
            pass
    meta = t.get("gdal_metadata") or ""
    desc = re.findall(r'role="description">([^<]*)<', meta)
    unit = re.findall(r'<Item name="(?:units?|UNITS?)"[^>]*>([^<]*)<', meta)
    return arr, _georef(t), {"description": desc[0] if desc else None, "unitInFile": unit[0] if unit else None,
                              "compression": comp, "predictor": pred, "sampleFormat": fmt, "bits": bits}


def _georef(t):
    scale, tie = t.get("pixel_scale"), t.get("tiepoint")
    if not scale or not tie:
        raise RasterError("GeoTIFF jeoreferansı yok")
    keys = t.get("geokeys") or ()
    geo = {keys[i]: keys[i + 3] for i in range(4, len(keys), 4)}
    epsg = geo.get(3072) or geo.get(2048)
    pixel_is_point = geo.get(1025) == 2
    x0, y0 = tie[3] - tie[0] * scale[0], tie[4] + tie[1] * scale[1]
    if pixel_is_point:
        x0, y0 = x0 - scale[0] / 2, y0 + scale[1] / 2
    return {"epsg": epsg, "x0": x0, "y0": y0, "dx": scale[0], "dy": scale[1]}


# ---- UTM (WGS84), Snyder 1987 seri formülleri -------------------------------------------

_A, _F = 6378137.0, 1 / 298.257223563
_E2 = _F * (2 - _F)
_EP2 = _E2 / (1 - _E2)
_K0 = 0.9996


def _zone(epsg):
    if 32601 <= epsg <= 32660:
        return epsg - 32600, True
    if 32701 <= epsg <= 32760:
        return epsg - 32700, False
    raise RasterError(f"Desteklenmeyen EPSG {epsg}")


def utm_forward(lat, lon, zone, north=True):
    phi, lam = math.radians(lat), math.radians(lon)
    lam0 = math.radians(zone * 6 - 183)
    n = _A / math.sqrt(1 - _E2 * math.sin(phi) ** 2)
    t = math.tan(phi) ** 2
    c = _EP2 * math.cos(phi) ** 2
    a = math.cos(phi) * (lam - lam0)
    m = _A * ((1 - _E2 / 4 - 3 * _E2 ** 2 / 64 - 5 * _E2 ** 3 / 256) * phi
              - (3 * _E2 / 8 + 3 * _E2 ** 2 / 32 + 45 * _E2 ** 3 / 1024) * math.sin(2 * phi)
              + (15 * _E2 ** 2 / 256 + 45 * _E2 ** 3 / 1024) * math.sin(4 * phi)
              - (35 * _E2 ** 3 / 3072) * math.sin(6 * phi))
    x = _K0 * n * (a + (1 - t + c) * a ** 3 / 6 + (5 - 18 * t + t * t + 72 * c - 58 * _EP2) * a ** 5 / 120) + 500000
    y = _K0 * (m + n * math.tan(phi) * (a * a / 2 + (5 - t + 9 * c + 4 * c * c) * a ** 4 / 24
                                       + (61 - 58 * t + t * t + 600 * c - 330 * _EP2) * a ** 6 / 720))
    return x, y if north else y + 10000000


def utm_inverse(x, y, zone, north=True):
    x -= 500000
    if not north:
        y -= 10000000
    m = y / _K0
    mu = m / (_A * (1 - _E2 / 4 - 3 * _E2 ** 2 / 64 - 5 * _E2 ** 3 / 256))
    e1 = (1 - math.sqrt(1 - _E2)) / (1 + math.sqrt(1 - _E2))
    phi1 = (mu + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu) + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32)
            * math.sin(4 * mu) + (151 * e1 ** 3 / 96) * math.sin(6 * mu) + (1097 * e1 ** 4 / 512) * math.sin(8 * mu))
    n1 = _A / math.sqrt(1 - _E2 * math.sin(phi1) ** 2)
    t1 = math.tan(phi1) ** 2
    c1 = _EP2 * math.cos(phi1) ** 2
    r1 = _A * (1 - _E2) / (1 - _E2 * math.sin(phi1) ** 2) ** 1.5
    d = x / (n1 * _K0)
    lat = phi1 - (n1 * math.tan(phi1) / r1) * (d * d / 2 - (5 + 3 * t1 + 10 * c1 - 4 * c1 * c1 - 9 * _EP2) * d ** 4 / 24
                                               + (61 + 90 * t1 + 298 * c1 + 45 * t1 * t1 - 252 * _EP2 - 3 * c1 * c1)
                                               * d ** 6 / 720)
    lon = (d - (1 + 2 * t1 + c1) * d ** 3 / 6 + (5 - 2 * c1 + 28 * t1 - 3 * c1 * c1 + 8 * _EP2 + 24 * t1 * t1)
           * d ** 5 / 120) / math.cos(phi1)
    return math.degrees(lat), zone * 6 - 183 + math.degrees(lon)


def bearing(lat1, lon1, lat2, lon2):
    """Başlangıç azimutu (kuzeyden saat yönünde, derece)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def angle_diff(a, b):
    """İki yön arasındaki en küçük açı (0–180)."""
    d = abs((a - b) % 360)
    return min(d, 360 - d)


def axial_diff(axis, direction):
    """Eksen (0–180, yönsüz) ile yön arasındaki en küçük açı (0–90): eksenin 180° belirsizliği."""
    d = angle_diff(axis % 180, direction % 180)
    return min(d, 180 - d)


def morphology(arr, geo, origin=None):
    """Maske (sonlu pikseller), piksel alanı, ağırlıklı merkez, ana eksen ve kaynak->merkez yönü.
    Değerler dosya birimindedir. Negatif artışlar ağırlıkta sıfırlanır (gürültü); sayıları ayrıca raporlanır."""
    mask = np.isfinite(arr)
    n = int(mask.sum())
    if n == 0:
        return {"status": "insufficient_data", "reason": "Maskede geçerli piksel yok"}
    rows, cols = np.nonzero(mask)
    vals = arr[mask]
    w = np.clip(vals, 0, None)
    x = geo["x0"] + (cols + 0.5) * geo["dx"]
    y = geo["y0"] - (rows + 0.5) * geo["dy"]
    projected = geo["epsg"] not in (4326, None)
    pixel_area = geo["dx"] * geo["dy"] if projected else None
    out = {"status": "ok", "method": METHOD, "pixels": n, "negativePixels": int((vals < 0).sum()),
           "pixelSizeM": [geo["dx"], geo["dy"]] if projected else None,
           "maskAreaM2": n * pixel_area if pixel_area else None,
           "maxValue": float(np.nanmax(vals)), "sumValue": float(vals.sum()), "meanValue": float(vals.mean())}
    if w.sum() <= 0:
        out.update(status="insufficient_data", reason="Pozitif artış pikseli yok")
        return out
    cx, cy = float((x * w).sum() / w.sum()), float((y * w).sum() / w.sum())
    zone_north = _zone(geo["epsg"]) if projected else None
    to_ll = (lambda a, b: utm_inverse(a, b, *zone_north)) if projected else (lambda a, b: (b, a))
    clat, clon = to_ll(cx, cy)
    out["centroid"] = {"lat": clat, "lon": clon}
    # Ağırlıklı kovaryans: ana eksen yönü (yönsüz) ve uzama oranı. Izgara kuzeyi ile coğrafi kuzey farkı
    # (UTM yakınsaması) eksen azimutunu dört ucun coğrafi azimutuyla hesaplayarak giderilir.
    dx, dy = x - cx, y - cy
    cov = np.array([[np.sum(w * dx * dx), np.sum(w * dx * dy)], [np.sum(w * dx * dy), np.sum(w * dy * dy)]]) / w.sum()
    evals, evecs = np.linalg.eigh(cov)
    major = evecs[:, 1]
    if n >= 3 and evals[0] >= 0 and evals[1] > 0:
        ex, ey = cx + major[0] * math.sqrt(evals[1]), cy + major[1] * math.sqrt(evals[1])
        elat, elon = to_ll(ex, ey)
        out["axisDeg"] = bearing(clat, clon, elat, elon) % 180
        out["elongation"] = float(math.sqrt(evals[1] / evals[0])) if evals[0] > 0 else None
        out["lengthScaleM"] = float(2 * math.sqrt(evals[1])) if projected else None
    if origin and all(isinstance(v, (int, float)) for v in origin):
        olat, olon = origin
        if projected:
            ox, oy = utm_forward(olat, olon, *zone_north)
            dist = math.hypot(cx - ox, cy - oy)
            min_dist = 2 * max(geo["dx"], geo["dy"])
        else:
            dist, min_dist = None, None
        out["origin"] = {"lat": olat, "lon": olon, "source": "sağlayıcı bulut noktası"}
        out["originToCentroidM"] = dist
        if dist is not None and dist >= min_dist:
            out["transportDeg"] = bearing(olat, olon, clat, clon)
        else:
            out["transportDeg"] = None
            out["transportNote"] = "Merkez, kaynak noktasına iki pikselden yakın; taşınma yönü belirlenemedi."
    return out
