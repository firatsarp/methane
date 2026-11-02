"""Yüksek olay / pik sınıflandırması. Saf hesap; veritabanına archive.py yazar.

Politika (app/policy/scientific-rules.json peakPolicy; eşikler operasyon başlangıç tercihleridir):
- Seri: kaynak ilişkisi + nicelik + sensör + yöntem sürümü. Farklı seriler birlikte sıralanmaz.
- Aynı çekim (acquisitionId) seride tek örnektir; erişim yolu çoğaltması örneklemi büyütmez.
- Robust aday: aynı serinin yalnızca ÖNCEKİ 90 gününden en az 10 gözlem ve 5 ayrı gün; z = (x - medyan) / (1,4826·MAD)
  >= 3,5. MAD = 0 veya örneklem yetersizse z üretilmez.
- Seyrek örneklem: seri başına en yüksek 3 geçerli bağımsız gözlem "historical_high_reference" (istatistiksel pik değil).
- Mutlak kg/sa eşiği yalnızca PEAK_ABSOLUTE_KGH ile açıkça verilirse.
- İnsan incelemesi: reviewed_peak ve rejected_artifact (reddedilen kayıt top-N'e girmez).
"""
import statistics
from datetime import datetime, timedelta

from . import policy

METHOD = "peaks-v1"
ORDINARY, CANDIDATE, REVIEWED, REFERENCE, REJECTED = ("ordinary", "peak_candidate", "reviewed_peak",
                                                     "historical_high_reference", "rejected_artifact")
# Gösterim önceliği: bir gözlemin birden çok tahmini farklı durum alırsa en güçlüsü
RANK = {REVIEWED: 4, CANDIDATE: 3, REFERENCE: 2, ORDINARY: 1, REJECTED: 0}
Z_EPS = 1e-9


def _dt(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None
    except ValueError:
        return None


def estimates(o):
    """Bir kanonik gözlemin sağlayıcı tahminleri: (sağlayıcı, ölçüm, yöntem sürümü, kaynak ilişkisi)."""
    out = []
    if o.get("measurement"):
        out.append({"provider": o.get("provider"), "measurement": o["measurement"],
                    "methodVersion": o.get("methodVersion"), "sourceAssociation": o.get("sourceId")})
    for a in o.get("alternateEstimates") or []:
        if a.get("measurement"):
            out.append({"provider": a.get("provider"), "measurement": a["measurement"],
                        "methodVersion": a.get("methodVersion"), "sourceAssociation": a.get("sourceAssociation")})
    return out


def group_key(o, est):
    m = est["measurement"]
    if not est.get("sourceAssociation") or m.get("value") is None:
        return None
    return "|".join(str(x) for x in (est["sourceAssociation"], m["quantityKind"], o.get("sensor"), est["methodVersion"]))


def build_series(items, rejected_ids=(), providers=None):
    """{grup: [(zaman, değer, gözlem_id, acquisitionId)]}; aynı çekim seride bir kez yer alır.
    providers sözlüğü verilirse her grubun sağlayıcısı oraya yazılır."""
    series = {}
    for o in items:
        t = _dt(o.get("observedAt"))
        if t is None or o["id"] in rejected_ids:
            continue
        for est in estimates(o):
            g = group_key(o, est)
            if not g:
                continue
            acq = o.get("acquisitionId") or o["id"]
            rows = series.setdefault(g, {})
            if providers is not None:
                providers[g] = est["provider"]
            # Aynı çekimden ikinci kayıt geldiyse (ör. farklı kimlikle) deterministik olarak küçük kimlik kalır
            prev = rows.get(acq)
            if prev is None or o["id"] < prev[2]:
                rows[acq] = (t, est["measurement"]["value"], o["id"], acq)
    return {g: sorted(r.values(), key=lambda x: (x[0], x[2])) for g, r in series.items()}


def robust_baseline(series_rows, t, value, cfg=None):
    """Yalnızca t'den önceki gözlemlerle referans. Değerlendirilen gözlem referansa sızmaz."""
    cfg = cfg or policy.peak()
    lo = t - timedelta(days=cfg["robustBaselineDays"])
    ref = [r for r in series_rows if lo <= r[0] < t]
    days = len({r[0].date() for r in ref})
    snap = {"method": METHOD, "n": len(ref), "distinctDays": days, "windowDays": cfg["robustBaselineDays"],
            "minN": cfg["minimumReferenceObservations"], "minDays": cfg["minimumReferenceDays"],
            "madScale": cfg["madScale"], "zThreshold": cfg["robustZCandidateThreshold"],
            "thresholdsAreOperationalDefaults": True, "median": None, "mad": None, "z": None}
    if len(ref) < cfg["minimumReferenceObservations"] or days < cfg["minimumReferenceDays"]:
        return {**snap, "status": "insufficient_reference"}
    vals = [r[1] for r in ref]
    med = statistics.median(vals)
    mad = statistics.median(abs(v - med) for v in vals)
    snap.update(median=med, mad=mad)
    if mad == 0:
        return {**snap, "status": "zero_mad"}
    z = (value - med) / (cfg["madScale"] * mad)
    # Sınır kapsayıcıdır (z >= eşik); kayan nokta yuvarlaması sınırdaki değeri dışarıda bırakmasın
    hit = z >= cfg["robustZCandidateThreshold"] - Z_EPS
    return {**snap, "z": z, "status": "candidate" if hit else "within_reference"}


def classify(items, reviews=None):
    """(her gözlem için {state, reasons[], groups{}}, {grup: sağlayıcı}). reviews: {id: "reviewed_peak"|"rejected_artifact"}.
    Sonuç yalnızca verilen gözlem kümesine bağlıdır; giriş sırası sonucu değiştirmez."""
    cfg = policy.peak()
    reviews = reviews or {}
    rejected = {i for i, s in reviews.items() if s == REJECTED}
    group_providers = {}
    series = build_series(items, rejected, group_providers)
    top_n = cfg["sparseHistoryArchiveTopN"]
    top = {g: {r[2] for r in sorted(rows, key=lambda r: (-r[1], r[2]))[:top_n]} for g, rows in series.items()}
    absolute = cfg.get("absoluteEmissionThresholdKgh")
    out = {}
    for o in sorted(items, key=lambda x: x["id"]):
        if o["id"] in rejected:
            out[o["id"]] = {"state": REJECTED, "reasons": [{"rule": "human_review", "state": REJECTED}], "groups": {}}
            continue
        state, reasons, groups = ORDINARY, [], {}
        t = _dt(o.get("observedAt"))
        for est in estimates(o):
            g = group_key(o, est)
            m = est["measurement"]
            if not g:
                reasons.append({"rule": "no_series", "provider": est["provider"],
                                "detail": "Kaynak ilişkisi veya değer yok; seri karşılaştırması yapılmadı."})
                continue
            if t is None:
                continue
            base = robust_baseline(series.get(g, []), t, m["value"], cfg)
            groups[g] = {"provider": est["provider"], "value": m["value"], "unit": m["unit"], "baseline": base,
                         "seriesSize": len(series.get(g, []))}
            if base["status"] == "candidate":
                state = max(state, CANDIDATE, key=RANK.get)
                reasons.append({"rule": "robust_z", "group": g, "z": base["z"]})
            if absolute is not None and m["quantityKind"] == "emission_rate_kg_h" and m["value"] >= absolute:
                state = max(state, CANDIDATE, key=RANK.get)
                reasons.append({"rule": "absolute_threshold", "group": g, "thresholdKgh": absolute})
            if o["id"] in top.get(g, ()):
                state = max(state, REFERENCE, key=RANK.get)
                rank = [r[2] for r in sorted(series[g], key=lambda r: (-r[1], r[2]))].index(o["id"]) + 1
                reasons.append({"rule": "sparse_top_n", "group": g, "rank": rank, "of": len(series[g]),
                                "label": cfg["sparseHistoryLabel"]})
        if reviews.get(o["id"]) == REVIEWED:
            state = REVIEWED
            reasons.append({"rule": "human_review", "state": REVIEWED})
        out[o["id"]] = {"state": state, "reasons": reasons, "groups": groups}
    return out, group_providers
