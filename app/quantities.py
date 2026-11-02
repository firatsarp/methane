"""Ölçüm nicelikleri, birim dönüşümü ve belirsizlik anlamı. Saf fonksiyonlar.

Yalnızca aynı nicelik ailesinde boyutsal dönüşüm yapılır (t/sa -> kg/sa). XCH4 ppb, ppm·m artış, kg/m² sütun kütlesi,
orta troposfer profili ve kg/sa salım birbirine katsayıyla çevrilmez; aralarında oran/fark üretilmez.
"""
import math

EMISSION_RATE = "emission_rate_kg_h"
XCH4 = "dry_air_column_xch4_ppb"
ENHANCEMENT_PPM_M = "column_enhancement_ppm_m"
COLUMN_MASS = "column_mass_enhancement_kg_m2"
PROFILE = "vertical_profile"
KINDS = (EMISSION_RATE, XCH4, ENHANCEMENT_PPM_M, COLUMN_MASS, PROFILE)

# Aile içi dönüşüm katsayıları: kanonik birime çarpan
_TO_CANONICAL = {
    EMISSION_RATE: {"kg/h": 1.0, "kg/sa": 1.0, "t/h": 1000.0, "t/sa": 1000.0},
    XCH4: {"ppb": 1.0, "ppm": 1000.0},
    ENHANCEMENT_PPM_M: {"ppm·m": 1.0, "ppm m": 1.0, "ppm*m": 1.0},
    COLUMN_MASS: {"kg/m2": 1.0, "kg/m²": 1.0},
}
CANONICAL_UNIT = {EMISSION_RATE: "kg/h", XCH4: "ppb", ENHANCEMENT_PPM_M: "ppm·m", COLUMN_MASS: "kg/m2"}

# Belirsizliğin istatistiksel anlamı. "unspecified": sağlayıcı ± verdi ama tanımını vermedi -> sigma sayılmaz.
SIGMA1 = "1-sigma"
UNSPECIFIED = "unspecified"


def _finite(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def to_canonical(kind, value, unit):
    """(değer, kanonik birim) ya da tanınmayan birimde (None, None). Değer tahmin edilmez."""
    if not _finite(value):
        return None, None
    factor = _TO_CANONICAL.get(kind, {}).get((unit or "").strip())
    if factor is None:
        return None, None
    return value * factor, CANONICAL_UNIT[kind]


def measurement(kind, value, unit, *, uncertainty=None, uncertainty_kind=UNSPECIFIED, scope="plume"):
    """Ortak ölçüm kaydı. Eksik değer None kalır; sıfıra çevrilmez."""
    v, u = to_canonical(kind, value, unit)
    unc = None
    if _finite(uncertainty):
        uv, _ = to_canonical(kind, uncertainty, unit)
        unc = {"value": uv, "kind": uncertainty_kind}
    return {"quantityKind": kind, "value": v, "unit": u, "uncertainty": unc, "scope": scope,
            "originalUnit": unit}


def numeric_comparable(a, b):
    """İki ölçüm sayısal olarak karşılaştırılabilir mi? (bool, gerekçe kodu)."""
    if not a or not b:
        return False, "missing_measurement"
    if a.get("value") is None or b.get("value") is None:
        return False, "missing_value"
    if a.get("quantityKind") != b.get("quantityKind"):
        return False, "quantity_mismatch"
    if a.get("scope") != b.get("scope"):
        # Kaynak ortalaması ile tek bulut farklı ürünlerdir
        return False, "scope_mismatch"
    if a.get("unit") != b.get("unit"):
        return False, "unit_mismatch"
    return True, "ok"


def difference(a, b):
    """Aynı nicelikteki iki ölçümün farkı ve yüzde farkı. Uygun değilse None ve gerekçe."""
    ok, why = numeric_comparable(a, b)
    if not ok:
        return None, why
    diff = b["value"] - a["value"]
    pct = 100.0 * diff / a["value"] if a["value"] else None
    return {"difference": diff, "percent": pct, "unit": a["unit"]}, "ok"


def combined_sigma(a, b, independent):
    """Fark için birleşik 1-sigma ve z. Yalnızca iki belirsizlik de belgeli 1-sigma ve tahminler bağımsızsa."""
    ua, ub = (a or {}).get("uncertainty"), (b or {}).get("uncertainty")
    if not independent:
        return None, "not_independent"
    if not ua or not ub or ua.get("kind") != SIGMA1 or ub.get("kind") != SIGMA1:
        return None, "uncertainty_definition_unknown"
    s = math.hypot(ua["value"], ub["value"])
    if s <= 0:
        return None, "zero_uncertainty"
    return {"sigma": s, "z": (b["value"] - a["value"]) / s, "unit": a["unit"]}, "ok"
