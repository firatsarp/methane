"""Gözlem karşılaştırması: karar arka uçta verilir, tarayıcı yalnızca sonucu gösterir.

Modlar (bilimsel-kurallar comparisons.modes):
- near_time_independent: farklı çekimler, |Δt| <= 30 dk (operasyon varsayılanı), konumsal örtüşme, aynı nicelik.
- historical_change: aynı nicelik + aynı seri (kaynak ilişkisi, sensör, yöntem sürümü), farklı zaman. Farklı tarihteki
  iki ölçüm eşzamanlı doğrulama sayılmaz; salım gerçekten değişmiş olabilir.
- context_only: sayısal fark/oran üretilmez, değerler yan yana gösterilir.
Reddedilen sayısal karşılaştırmalar gerekçe koduyla döner (rejectedNumeric).
"""
from datetime import datetime

from . import catalog, policy, quantities

METHOD = "comparison-v1"
# Konum belirsizliği bilinmeyen yüksek çözünürlüklü bulut noktası için en küçük tolerans (m). Sabit bir
# "tesis eşleşmesi" değildir; yalnızca iki noktanın aynı bölgeye işaret edip etmediğini sınar.
DEFAULT_LOCATION_UNCERTAINTY_M = 500


def _dt(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None
    except ValueError:
        return None


def series_key(o, est=None):
    """Karşılaştırılabilir seri: kaynak ilişkisi, nicelik, sensör, yöntem sürümü."""
    m = (est or o).get("measurement") or {}
    return (o.get("sourceId"), m.get("quantityKind"), o.get("sensor"), (est or o).get("methodVersion"))


def _tolerance(o):
    u = o.get("locationUncertaintyM")
    return u if isinstance(u, (int, float)) and u > 0 else DEFAULT_LOCATION_UNCERTAINTY_M


def compare(a, b):
    """İki kanonik gözlem için karar. Sayısal sonuç yalnızca izin verilen koşullarda üretilir."""
    cfg = policy.comparisons()
    max_min = cfg["nearTimeCandidateMaxMinutes"]
    ta, tb = _dt(a.get("observedAt")), _dt(b.get("observedAt"))
    dt_min = abs((tb - ta).total_seconds()) / 60 if ta and tb else None
    dist = catalog.distance_m(a["lat"], a["lon"], b["lat"], b["lon"])
    tol = _tolerance(a) + _tolerance(b)
    ma, mb = a.get("measurement"), b.get("measurement")
    reasons, limitations = [], []
    out = {"method": METHOD, "policyVersion": policy.version(), "a": a["id"], "b": b["id"],
           "timeDifferenceMinutes": round(dt_min, 1) if dt_min is not None else None,
           "distanceM": round(dist), "spatialToleranceM": tol, "numeric": None, "combined": None,
           "independent": None, "rejectedNumeric": None}

    if a.get("acquisitionId") and a.get("acquisitionId") == b.get("acquisitionId"):
        out.update(mode="context_only", independent=False, rejectedNumeric="same_acquisition")
        reasons.append("Aynı uydu çekimi; farklı sağlayıcı/erişim yolu iki bağımsız ölçüm değildir.")
        return {**out, "reasons": reasons, "limitations": limitations}
    if a["id"] == b["id"]:
        out.update(mode="context_only", independent=False, rejectedNumeric="same_record")
        return {**out, "reasons": ["Aynı kayıt."], "limitations": []}

    ok, why = quantities.numeric_comparable(ma, mb)
    if not ok:
        out.update(mode="context_only", rejectedNumeric=why)
        reasons.append({
            "quantity_mismatch": "Ölçülen nicelikler farklı (ör. kg/sa salım ile ppb sütun); oran veya fark üretilmez.",
            "scope_mismatch": "Kaynak ortalaması ile tek bulut gözlemi farklı ürünlerdir.",
            "missing_value": "En az birinde sayısal tahmin yok; yokluk sıfır sayılmaz.",
            "missing_measurement": "En az birinde ölçüm kaydı yok.",
            "unit_mismatch": "Birimler kanonik birime çevrilemedi.",
        }.get(why, why))
        return {**out, "reasons": reasons, "limitations": limitations}

    same_place = dist <= tol
    if dt_min is not None and dt_min <= max_min and same_place and a.get("sensor") != b.get("sensor"):
        out.update(mode="near_time_independent", independent=True)
        reasons.append(f"Farklı çekimler {dt_min:.0f} dk arayla ve {dist:.0f} m mesafede (tolerans {tol:.0f} m).")
        limitations.append(f"{max_min} dk penceresi bu projenin operasyon varsayılanıdır; evrensel eşik değildir.")
        if _shared_wind(a, b):
            limitations.append("İki salım tahmini aynı yeniden analiz rüzgârına dayanıyor olabilir; "
                               "tahmin hataları tam bağımsız değildir.")
    elif series_key(a) == series_key(b) and series_key(a)[0]:
        out.update(mode="historical_change", independent=True)
        reasons.append("Aynı kaynak ilişkisi, sensör ve yöntem sürümünde farklı zamanlardaki iki gözlem.")
        limitations.append("Farklı zamanlardaki fark, salımın gerçekten değişmesinden de kaynaklanabilir; "
                           "aynı hava kütlesi veya kesintisiz olay varsayılmaz.")
    else:
        out.update(mode="context_only", independent=None)
        reasons.append("Seri (kaynak/sensör/yöntem) veya zaman/konum koşulları sayısal karşılaştırma için uygun değil.")
        return {**out, "reasons": reasons, "limitations": limitations}

    diff, _ = quantities.difference(ma, mb)
    out["numeric"] = diff
    comb, why = quantities.combined_sigma(ma, mb, independent=out["independent"] and not _shared_wind(a, b))
    out["combined"] = comb
    if comb is None:
        limitations.append({
            "uncertainty_definition_unknown": "Belirsizliklerin istatistiksel tanımı belgeli değil; birleşik sigma "
                                              "veya anlamlılık hesaplanmadı, aralıklar ayrı gösterilir.",
            "not_independent": "Tahminler bağımsız sayılmadığı için birleşik belirsizlik hesaplanmadı.",
        }.get(why, why))
    return {**out, "reasons": reasons, "limitations": limitations}


def _shared_wind(a, b):
    wa, wb = (a.get("providerWind") or {}).get("source"), (b.get("providerWind") or {}).get("source")
    return bool(wa and wa == wb)


def candidates(o, items, archived=(), limit=30):
    """Seçilen gözlemle karşılaştırılabilecek kayıtlar: aynı kaynak ilişkisi veya yakın konum; havuz = son 90 gün +
    kalıcı yüksek olay arşivi (catalog.load arşivi zaten içerir)."""
    pool = {x["id"]: x for x in list(items) + list(archived) if x["id"] != o["id"]}
    near = [x for x in pool.values()
            if (o.get("sourceId") and x.get("sourceId") == o.get("sourceId"))
            or catalog.distance_m(o["lat"], o["lon"], x["lat"], x["lon"]) <= _tolerance(o) + _tolerance(x)]
    near.sort(key=lambda x: x.get("observedAt") or "", reverse=True)
    return [{"observation": {k: x.get(k) for k in ("id", "observedAt", "sensor", "provider", "measurement",
                                                  "acquisitionId", "archiveState", "lat", "lon")},
             "decision": compare(o, x)} for x in near[:limit]]
