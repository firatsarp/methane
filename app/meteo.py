"""Meteoroloji ve iklim hesapları. Saf fonksiyonlar; ağ veya veritabanı kullanmaz.

Yön kuralları: meteorolojik "geldiği yön" (FROM) kuzeyden saat yönünde derece; taşınma yönü (TO) = FROM + 180.
Açı ortalamaları bileşenlerle alınır (350° ve 10° ortalaması 0°'dir, 180° değil).
"""
import calendar
import math
from datetime import datetime, timedelta, timezone

# Bu hızın altında yön anlamlı sayılmaz
CALM_MS = 0.5
# Gözlem anına en yakın saatlik değer bundan uzaksa kullanılmaz
MAX_OFFSET_MIN = 60
CLIMATE_METHOD_VERSION = "climate-monthly-v1"
REFERENCE_START, REFERENCE_END = 1991, 2020


def _finite(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def wind_from_uv(u, v):
    """(hız m/s, geldiği yön °, taşınma yönü °). Hız sakin eşiğin altındaysa yönler None."""
    if not (_finite(u) and _finite(v)):
        return None, None, None
    speed = math.hypot(u, v)
    if speed < CALM_MS:
        return speed, None, None
    frm = (math.degrees(math.atan2(-u, -v)) + 360) % 360
    return speed, frm, (frm + 180) % 360


def uv_from_wind(speed, from_deg):
    """Geldiği yön ve hızdan (u, v). Open-Meteo yalnızca hız/yön verdiği için bileşen ortalamasında kullanılır."""
    if not (_finite(speed) and _finite(from_deg)):
        return None, None
    r = math.radians(from_deg)
    return -speed * math.sin(r), -speed * math.cos(r)


def to_direction(from_deg):
    return None if not _finite(from_deg) else (from_deg + 180) % 360


def mean_direction(directions, weights=None):
    """Bileşen (vektör) ortalaması. Boş veya birbirini sıfırlayan yönlerde None."""
    pairs = [(d, 1.0 if weights is None else w) for d, w in zip(directions, weights or [None] * len(directions))
             if _finite(d) and (weights is None or _finite(w))]
    if not pairs:
        return None
    x = sum(w * math.sin(math.radians(d)) for d, w in pairs)
    y = sum(w * math.cos(math.radians(d)) for d, w in pairs)
    if math.hypot(x, y) < 1e-9:
        return None
    return (math.degrees(math.atan2(x, y)) + 360) % 360


# ---- Birim dönüşümleri (dosyanın birim metadata'sına göre) ----------------------------

def convert(value, unit):
    """(değer, hedef birim). Tanınmayan birim olduğu gibi döner; değer tahmin edilmez."""
    if not _finite(value):
        return None, unit
    u = (unit or "").strip()
    if u == "K":
        return value - 273.15, "°C"
    if u == "Pa":
        return value / 100.0, "hPa"
    if u in ("(0 - 1)", "0-1", "1", "fraction"):
        return value * 100.0, "%"
    # Yükseklik metre kalır; yağış birikimi ayrıca precip_m_to_mm ile çevrilir
    return value, u


def precip_m_to_mm(value):
    return None if not _finite(value) else value * 1000.0


def humidity_from_dewpoint(t_c, td_c):
    """Magnus formülüyle bağıl nem (%). Girdi °C."""
    if not (_finite(t_c) and _finite(td_c)):
        return None
    a, b = 17.625, 243.04
    rh = 100.0 * math.exp(a * td_c / (b + td_c) - a * t_c / (b + t_c))
    return max(0.0, min(100.0, rh))


# ---- Zaman eşleştirme ---------------------------------------------------------

def parse_utc(s):
    if not isinstance(s, str):
        return None
    t = s.strip()
    if not t:
        return None
    if t.endswith(("Z", "z")):
        t = t[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    # Open-Meteo timezone=UTC ile ofsetsiz saat döndürür; bu modülde ofsetsiz zaman UTC kabul edilir
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def nearest_index(times, target, max_offset_min=MAX_OFFSET_MIN):
    """Hedef zamana en yakın örneğin (indeks, dakika farkı). Tolerans dışındaysa (None, fark)."""
    best, best_diff = None, None
    for k, t in enumerate(times):
        dt = parse_utc(t) if isinstance(t, str) else t
        if dt is None:
            continue
        diff = (dt - target).total_seconds() / 60.0
        if best_diff is None or abs(diff) < abs(best_diff):
            best, best_diff = k, diff
    if best is None:
        return None, None
    if abs(best_diff) > max_offset_min:
        return None, round(best_diff)
    return best, round(best_diff)


def interpolate_wind(times, speeds, directions, target, max_offset_min=MAX_OFFSET_MIN):
    """Hedefi çevreleyen iki geçerli saatlik örnek varsa u/v bileşenlerinde doğrusal interpolasyon.
    (hız, geldiği yön, taşınma yönü, yöntem) ya da yoksa None. Yön açıları aritmetik ortalanmaz."""
    parsed = [(parse_utc(t) if isinstance(t, str) else t, s, d) for t, s, d in zip(times, speeds, directions)]
    before = [p for p in parsed if p[0] and p[0] <= target and _finite(p[1]) and _finite(p[2])]
    after = [p for p in parsed if p[0] and p[0] >= target and _finite(p[1]) and _finite(p[2])]
    if not before or not after:
        return None
    b, a = max(before, key=lambda p: p[0]), min(after, key=lambda p: p[0])
    if (target - b[0]).total_seconds() / 60 > max_offset_min or (a[0] - target).total_seconds() / 60 > max_offset_min:
        return None
    ub, vb = uv_from_wind(b[1], b[2])
    ua, va = uv_from_wind(a[1], a[2])
    span = (a[0] - b[0]).total_seconds()
    w = 0.0 if span == 0 else (target - b[0]).total_seconds() / span
    s, f, t = wind_from_uv(ub + w * (ua - ub), vb + w * (va - vb))
    return {"speed": s, "from": f, "to": t, "calm": f is None, "unit": "m/s",
            "method": "u/v doğrusal interpolasyon", "bracket": [b[0].isoformat(), a[0].isoformat()], "weight": w}


def direction_change(times, directions, speeds, target, hours=1):
    """Gözlemden ±hours saat önce/sonra geldiği yön farkı (0–180°). Sakin rüzgârda veya veri yoksa None."""
    parsed = {}
    for t, d, s in zip(times, directions, speeds):
        dt = parse_utc(t) if isinstance(t, str) else t
        if dt is not None:
            parsed[dt] = (d, s)
    base = target.replace(minute=0, second=0, microsecond=0)
    pre, post = parsed.get(base - timedelta(hours=hours - 1)), parsed.get(base + timedelta(hours=hours))
    if not pre or not post or not all(_finite(x) for x in (*pre, *post)) or min(pre[1], post[1]) < CALM_MS:
        return None
    d = abs((post[0] - pre[0]) % 360)
    return min(d, 360 - d)


def precip_prior(times, precip, target, hours):
    """Gözlem saatine kadar (dahil) son 'hours' saatlik yağış toplamı (mm). Eksik saat varsa None (sıfır sayılmaz)."""
    idx, _ = nearest_index(times, target)
    if idx is None or idx - hours + 1 < 0:
        return None
    vals = precip[idx - hours + 1: idx + 1]
    return sum(vals) if len(vals) == hours and all(_finite(v) for v in vals) else None


# ---- İklim toplulaştırması ----------------------------------------------------

def hours_in_month(year, month):
    return calendar.monthrange(year, month)[1] * 24


def monthly_aggregates(times, temp, precip, wind):
    """Saatlik UTC seriden ay bazında değerler. Yalnızca eksiksiz aylar (her saatte geçerli değer) hesaplanır;
    eksik saat sıfırla doldurulmaz. Dönüş: {(yıl, ay): {"temp": °C ort, "precip": mm toplam, "wind": m/s skaler ort}}."""
    buckets = {}
    for t, a, p, w in zip(times, temp, precip, wind):
        dt = parse_utc(t) if isinstance(t, str) else t
        if dt is None:
            continue
        b = buckets.setdefault((dt.year, dt.month), {"temp": [], "precip": [], "wind": []})
        if _finite(a):
            b["temp"].append(a)
        if _finite(p):
            b["precip"].append(p)
        if _finite(w):
            b["wind"].append(w)
    out = {}
    for (y, m), b in buckets.items():
        need = hours_in_month(y, m)
        out[(y, m)] = {
            "temp": sum(b["temp"]) / need if len(b["temp"]) == need else None,
            "precip": sum(b["precip"]) if len(b["precip"]) == need else None,
            # Ortalama hız skaler hızlardan alınır; ortalama u/v vektörünün büyüklüğü kullanılmaz
            "wind": sum(b["wind"]) / need if len(b["wind"]) == need else None,
        }
    return out


def climate_reference(monthly, start=REFERENCE_START, end=REFERENCE_END):
    """Her takvim ayı için referans dönemindeki geçerli yılların ortalaması ve geçerli yıl sayısı."""
    months = []
    for m in range(1, 13):
        row = {"month": m}
        for key in ("temp", "precip", "wind"):
            vals = [monthly[(y, m)][key] for y in range(start, end + 1)
                    if (y, m) in monthly and monthly[(y, m)][key] is not None]
            row[key] = sum(vals) / len(vals) if vals else None
            row[f"{key}Years"] = len(vals)
        months.append(row)
    return months


def compare_month(monthly, reference, year, month):
    """Tamamlanmış bir ayın aynı yöntemle hesaplanan değerinin referanstan farkı. Ay eksikse None."""
    cur = monthly.get((year, month))
    if not cur or any(cur[k] is None for k in ("temp", "precip", "wind")):
        return None
    ref = reference[month - 1]
    out = {"year": year, "month": month, "values": cur, "diff": {}, "precipPercent": None}
    for k in ("temp", "precip", "wind"):
        out["diff"][k] = cur[k] - ref[k] if ref[k] is not None else None
    if ref["precip"] is not None and ref["precip"] > 0:
        out["precipPercent"] = 100.0 * (cur["precip"] - ref["precip"]) / ref["precip"]
    return out


def latest_complete_month(monthly, now=None):
    """Seride eksiksiz olan en son (yıl, ay); içinde bulunulan ay hiçbir zaman seçilmez."""
    now = now or datetime.now(timezone.utc)
    done = [k for k, v in monthly.items()
            if (k[0], k[1]) < (now.year, now.month) and all(v[x] is not None for x in ("temp", "precip", "wind"))]
    return max(done) if done else None


def month_bounds(year, month):
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=calendar.monthrange(year, month)[1])
    return start, end
