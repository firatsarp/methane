"""Gözlem yorum motoru: deterministik, sürümlü, test edilebilir kurallar. LLM kullanılmaz.

Her bulgu: ruleId, status (supported | limited | insufficient_data | requires_validation), claim, facts, inputIds,
missingInputs, limitations, referenceIds, methodVersion, section. Girdi yoksa sonuç üretilmez; eksiklik bulguya yazılır.
Sıra: gözlenen sonuç -> karşılaştırılabilir geçmiş -> diğer sensör kanıtı -> meteoroloji -> belirlenemeyenler ->
izlenecek sonraki gözlem. Kalibrasyonsuz "güven yüzdesi" üretilmez. Sağlayıcı Q değeri yeni rüzgârla düzeltilmez.
"""
from datetime import datetime, timezone

from . import policy, raster, store

METHOD = "interpretation-v1"
SECTIONS = ("observed", "history", "other_sensors", "meteorology", "undetermined", "next")
SHALLOW_PBL_M = 300            # operasyon varsayılanı: sığ karışım katmanı uyarısı
GRID_DISTANCE_WARN_KM = 15     # model hücre merkezine uzaklık uyarısı (ERA5 ~0,25°)


def _f(rule, section, status, claim, *, facts=None, inputs=None, missing=None, limits=None, refs=None):
    return {"ruleId": rule, "section": section, "status": status, "claim": claim, "facts": facts or {},
            "inputIds": inputs or [], "missingInputs": missing or [], "limitations": limits or [],
            "referenceIds": refs or [], "methodVersion": METHOD}


def _fmt(v, d=0):
    return f"{v:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def observed(o):
    m = o.get("measurement")
    t_ok = bool(o.get("observedAt"))
    out = []
    if not t_ok:
        out.append(_f("TIME-01", "observed", "insufficient_data", "Gözlem zamanı saat dilimiyle doğrulanmadı; "
                      "meteoroloji ve geçmiş karşılaştırması yapılmaz.", missing=["observedAt"]))
    if not m or m.get("value") is None:
        out.append(_f("OBS-01", "observed", "insufficient_data",
                      "Sağlayıcı bu gözlem için salım tahmini yayımlamadı. Bu, sıfır salım anlamına gelmez.",
                      inputs=[o["id"]], missing=["provider_emission_estimate"], refs=["S23"]))
        return out
    unc = m.get("uncertainty")
    unc_text = ("belirsizlik verilmemiş" if not unc else
                f"± {_fmt(unc['value'])} {m['unit']} ({'1-sigma (belgeli)' if unc['kind'] == '1-sigma' else 'istatistiksel tanımı belgelenmemiş'})")
    alt = [a for a in o.get("alternateEstimates") or [] if a.get("measurement")]
    limits = ["Tek bir bulut gözleminin anlık tahminidir; yıllık salım veya süreklilik değildir.",
              "Kaynak ortalaması bu buluta atanmaz."]
    if alt:
        limits.append("Aynı çekimin diğer sağlayıcı tahminleri ayrı yöntemlerdir; bağımsız ikinci ölçüm sayılmaz.")
    out.append(_f("OBS-01", "observed", "supported",
                  f"{o.get('provider')} tahmini {_fmt(m['value'])} {m['unit']}; {unc_text}.",
                  facts={"value": m["value"], "unit": m["unit"], "quantityKind": m["quantityKind"],
                         "uncertainty": unc, "sensor": o.get("sensor"), "methodVersion": o.get("methodVersion"),
                         "acquisitionId": o.get("acquisitionId"), "alternateEstimates": len(alt)},
                  inputs=[o["id"]] + [a.get("id") for a in alt], limits=limits, refs=["CM", "S23"]))
    return out


def history(o, classification):
    if not classification:
        return [_f("HIST-01", "history", "insufficient_data", "Seri sınıflandırması henüz hesaplanmadı.",
                   missing=["classification"])]
    out = []
    for g, info in (classification.get("groups") or {}).items():
        b = info["baseline"]
        facts = {"group": g, "provider": info["provider"], "referenceN": b["n"], "distinctDays": b["distinctDays"],
                 "seriesSize": info["seriesSize"], "windowDays": b["windowDays"]}
        lim = ["Eşikler (en az 10 gözlem, 5 gün, z ≥ 3,5) proje operasyon varsayılanıdır; olasılık değildir.",
               "Referans yalnızca bu gözlemden önceki gözlemlerden oluşur.",
               "Farklı zamanlardaki değerler salımın gerçekten değişmesinden de farklı olabilir."]
        if b["status"] == "insufficient_reference":
            out.append(_f("HIST-01", "history", "insufficient_data",
                          f"Aynı seride önceki 90 günde {b['n']} gözlem ({b['distinctDays']} ayrı gün) var; "
                          f"robust karşılaştırma için en az {b['minN']} gözlem ve {b['minDays']} gün gerekiyor.",
                          facts=facts, limits=lim, refs=["SC23"]))
        elif b["status"] == "zero_mad":
            out.append(_f("HIST-01", "history", "insufficient_data",
                          "Referans gözlemlerin yayılımı sıfır (MAD = 0); anlamlı z değeri üretilmedi.",
                          facts={**facts, "median": b["median"]}, limits=lim))
        else:
            word = "referansın belirgin üzerinde (aday)" if b["status"] == "candidate" else "referans aralığında"
            out.append(_f("HIST-01", "history", "supported",
                          f"Önceki {b['n']} gözlemin medyanı {_fmt(b['median'])} {info['unit']}; robust z = "
                          f"{_fmt(b['z'], 1)}, {word}.", facts={**facts, "median": b["median"], "mad": b["mad"],
                                                                 "z": b["z"]}, limits=lim, refs=["SC23"]))
    for r in classification.get("reasons") or []:
        if r["rule"] == "sparse_top_n":
            out.append(_f("HIST-02", "history", "limited",
                          f"Mevcut örneklemde serinin en yüksek {r['rank']}. gözlemi ({r['of']} gözlem içinde); "
                          "istatistiksel pik doğrulanmadı.", facts=r,
                          limits=["Bu seçim normal düzeyin bilindiği veya kaynağın tehlikeli olduğu anlamına gelmez."]))
        if r["rule"] == "no_series":
            out.append(_f("HIST-03", "history", "insufficient_data", r["detail"], facts={"provider": r["provider"]},
                          missing=["source_association"]))
    return out or [_f("HIST-01", "history", "insufficient_data", "Karşılaştırılabilir seri yok.",
                      missing=["comparable_series"])]


def other_sensors(o, candidates):
    out = []
    same = [a for a in o.get("alternateEstimates") or []]
    if same:
        out.append(_f("SENS-01", "other_sensors", "limited",
                      f"Aynı çekim {len(same)} başka sağlayıcı/erişim yolunda da yayımlanmış; bunlar bağımsız tespit değildir.",
                      facts={"providers": [a.get("provider") for a in same],
                             "matchConfidence": [a.get("matchConfidence") for a in same]},
                      inputs=[a.get("id") for a in same], refs=["S24"]))
    near = [c for c in candidates or [] if c["decision"]["mode"] == "near_time_independent"]
    hist = [c for c in candidates or [] if c["decision"]["mode"] == "historical_change"]
    rejected = [c for c in candidates or [] if c["decision"].get("rejectedNumeric")]
    if near:
        out.append(_f("SENS-02", "other_sensors", "supported",
                      f"{len(near)} farklı çekim yakın zamanlı bağımsız karşılaştırmaya uygun.",
                      inputs=[c["observation"]["id"] for c in near], refs=["S24"]))
    else:
        out.append(_f("SENS-02", "other_sensors", "insufficient_data",
                      "30 dakika içinde ve aynı bölgede farklı bir uydu çekimi yok; bağımsız doğrulama etiketi verilmedi.",
                      missing=["near_time_independent_observation"], refs=["S24"]))
    if rejected:
        out.append(_f("SENS-03", "other_sensors", "limited",
                      f"{len(rejected)} yakın kayıt sayısal karşılaştırmaya alınmadı (aynı çekim, farklı nicelik "
                      "veya eksik tahmin).", facts={"reasons": sorted({c['decision']['rejectedNumeric'] for c in rejected})}))
    if hist:
        out.append(_f("SENS-04", "other_sensors", "supported",
                      f"Aynı seride {len(hist)} farklı tarihli gözlem tarihsel değişim karşılaştırmasına uygun.",
                      inputs=[c["observation"]["id"] for c in hist]))
    return out


def _met_block(w):
    return (w or {}).get("primary")


def meteorology(o, w, numeric):
    cfg = policy.meteorology()
    p = _met_block(w)
    out = []
    if not p:
        status = (w or {}).get("status")
        out.append(_f("MET-01", "meteorology", "insufficient_data",
                      "Gözlem anına ait yeniden analiz yok (" + (status or "alınamadı") + "); güncel tahmin bu gözleme "
                      "bağlanmaz.", missing=["era5_at_observation_time"], refs=["ERA5"]))
        out.append(_f("MORPH-01", "meteorology", "insufficient_data",
                      "Rüzgâr girdisi olmadan bulut ekseni–rüzgâr uyumu değerlendirilemez.", missing=["wind"]))
        return out
    off = p.get("timeOffsetMinutes")
    src = f"{p.get('accessPath')} ({p.get('dataset')}, {p.get('spatialResolution')})"
    inputs = [f"{p.get('provider')}:{p.get('dataset')}@{p.get('validAt')}"]
    out.append(_f("MET-01", "meteorology", "supported",
                  f"Gözlem saatine {abs(off or 0)} dk uzaklıktaki saatlik yeniden analiz kullanıldı: {src}.",
                  facts={"validAt": p.get("validAt"), "offsetMinutes": off, "grid": p.get("resolvedGridPoint"),
                         "model": p.get("model"), "accessPath": p.get("accessPath")}, inputs=inputs,
                  limits=["CDS ve Open-Meteo yolları aynı ERA5 veri ailesidir; iki bağımsız rüzgâr ölçümü sayılmaz.",
                          "Model hücre ortalamasıdır; tesis ölçeğindeki yerel rüzgârı temsil etmeyebilir."],
                  refs=["ERA5", "B21"]))
    wind = p.get("wind10Interpolated") or p.get("wind10")
    if not wind:
        out.append(_f("MET-02", "meteorology", "insufficient_data", "10 m rüzgâr yok.", missing=["wind10"]))
    else:
        s = wind["speed"]
        facts = {"speedMs": s, "fromDeg": wind.get("from"), "toDeg": wind.get("to"),
                 "method": wind.get("method", "en yakın saat"), "calmMs": cfg["calmWindMs"],
                 "weakWindMs": cfg["weakWindWarningMs"]}
        if wind.get("calm"):
            out.append(_f("MET-02", "meteorology", "limited",
                          f"Rüzgâr {_fmt(s, 1)} m/s (sakin eşiği {_fmt(cfg['calmWindMs'], 1)} m/s altında); yön tanımsız, "
                          "bulut taşınma yönü yorumlanamaz.", facts=facts, inputs=inputs, refs=["B21"]))
        elif s < cfg["weakWindWarningMs"]:
            out.append(_f("MET-02", "meteorology", "limited",
                          f"Rüzgâr zayıf: {_fmt(s, 1)} m/s, {_fmt(wind['from'])}°'den esiyor (taşınma {_fmt(wind['to'])}°). "
                          "Zayıf rüzgârda bulut biçimi ve salım hesabı rüzgâr belirsizliğine daha duyarlıdır.",
                          facts=facts, inputs=inputs,
                          limits=[f"{_fmt(cfg['weakWindWarningMs'], 0)} m/s eşiği operasyon varsayılanıdır."], refs=["B21"]))
        else:
            out.append(_f("MET-02", "meteorology", "supported",
                          f"Rüzgâr {_fmt(s, 1)} m/s, {_fmt(wind['from'])}°'den esiyor; taşınma yönü {_fmt(wind['to'])}°.",
                          facts=facts, inputs=inputs, refs=["B21"]))
    w100 = p.get("wind100Interpolated") or p.get("wind100")
    w10 = wind
    if w10 and w100 and w10.get("from") is not None and w100.get("from") is not None:
        d = raster.angle_diff(w10["from"], w100["from"])
        st = "limited" if d > cfg["directionMismatchWarningDeg"] else "supported"
        out.append(_f("MET-03", "meteorology", st,
                      f"10 m ile 100 m rüzgâr yönleri arasında {_fmt(d)}° fark var"
                      + ("; dikey yön değişimi taşınma yorumunu kısıtlar." if st == "limited" else "."),
                      facts={"diffDeg": d, "threshold": cfg["directionMismatchWarningDeg"]}, inputs=inputs))
    else:
        out.append(_f("MET-03", "meteorology", "insufficient_data", "100 m rüzgâr yok; dikey yön değişimi değerlendirilmedi.",
                      missing=["wind100"]))
    ch = p.get("directionChange1h")
    if ch is not None:
        st = "limited" if ch > cfg["directionMismatchWarningDeg"] else "supported"
        out.append(_f("MET-04", "meteorology", st, f"Gözlemi çevreleyen saatler arasında yön {_fmt(ch)}° değişti"
                      + ("; değişken rüzgâr." if st == "limited" else "."), facts={"diffDeg": ch}, inputs=inputs))
    grid = p.get("resolvedGridPoint") or {}
    if grid.get("lat") is not None:
        from .catalog import distance_m
        km = distance_m(o["lat"], o["lon"], grid["lat"], grid["lon"]) / 1000
        out.append(_f("MET-05", "meteorology", "limited" if km > GRID_DISTANCE_WARN_KM else "supported",
                      f"Model hücre noktası gözleme {_fmt(km, 1)} km uzaklıkta"
                      + (f" (yükseklik {_fmt(grid['elevationM'])} m)." if grid.get("elevationM") is not None else "."),
                      facts={"distanceKm": km, "grid": grid},
                      limits=["Engebeli arazide hücre ortalaması yerel rüzgârı temsil etmeyebilir."]))
    blh = ((p.get("variables") or {}).get("boundary_layer_height") or (p.get("variables") or {}).get("blh") or {}).get("value")
    if blh is None:
        out.append(_f("MET-06", "meteorology", "insufficient_data", "Sınır tabakası yüksekliği yok.",
                      missing=["boundary_layer_height"]))
    else:
        st = "limited" if blh < SHALLOW_PBL_M else "supported"
        out.append(_f("MET-06", "meteorology", st, f"Sınır tabakası yüksekliği {_fmt(blh)} m"
                      + ("; sığ karışım katmanı yüzeye yakın birikime yol açabilir, bu salımın arttığını göstermez."
                         if st == "limited" else "."), facts={"blhM": blh, "shallowM": SHALLOW_PBL_M}))
    pp = p.get("precipPrior") or {}
    if pp:
        have = {k: v for k, v in pp.items() if v is not None}
        out.append(_f("MET-07", "meteorology", "supported" if have else "insufficient_data",
                      ("Önceki yağış: " + ", ".join(f"{k} {_fmt(v, 1)} mm" for k, v in have.items()) + ". "
                       if have else "Önceki yağış toplamı hesaplanamadı. ")
                      + "Yağış, salımın nedeni olarak yorumlanmaz.", facts=pp,
                      missing=[k for k, v in pp.items() if v is None], refs=["PR26"],
                      limits=["Depolama sahası salımı için nem, atık bileşimi, gaz toplama ve örtü verisi olmadan "
                              "yağışa bağlı nedensel sonuç üretilmez."]))
    out += morphology(o, wind, numeric, cfg)
    pw = o.get("providerWind")
    if pw and wind and pw.get("speed") is not None:
        out.append(_f("MET-08", "meteorology", "supported",
                      f"Sağlayıcının salım hesabında kullandığı rüzgâr {_fmt(pw['speed'], 1)} m/s; yeniden analiz "
                      f"{_fmt(wind['speed'], 1)} m/s.", facts={"provider": pw, "reanalysis": wind["speed"]},
                      limits=["Sağlayıcı Q değeri bu farka göre düzeltilmez; yeni Q ancak ayrı ve kalibre edilmiş "
                              "bir yöntemle ayrı sonuç olarak üretilebilir."], refs=["B21", "P23"]))
    return out


def morphology(o, wind, numeric, cfg):
    m = (numeric or {}).get("morphology") or {}
    if (numeric or {}).get("status") not in ("ok",) or not m:
        reason = (numeric or {}).get("reason") or "Sayısal artış rasterı yok"
        return [_f("MORPH-01", "meteorology", "insufficient_data",
                   f"Bulut ekseni–rüzgâr karşılaştırması yapılmadı: {reason}. Renkli PNG'den sayısal nicelik çıkarılmaz.",
                   missing=["numeric_enhancement_raster"])]
    facts = {k: m.get(k) for k in ("pixels", "negativePixels", "maskAreaM2", "axisDeg", "elongation",
                                    "transportDeg", "originToCentroidM")}
    facts["rasterSha256"] = numeric.get("sha256")
    limits = [numeric.get("maskNote"), "Eksen yönsüzdür (180° belirsizlik); taşınma yönü kaynak noktasından ağırlıklı "
              "merkeze doğru alınır.", "Uyuşmazlık bir inceleme işaretidir; uydu ölçümünün yanlış olduğunu kanıtlamaz."]
    if not wind or wind.get("to") is None:
        return [_f("MORPH-01", "meteorology", "insufficient_data", "Rüzgâr yönü tanımsız olduğu için eksen karşılaştırması "
                   "yapılmadı.", facts=facts, missing=["wind_direction"], limits=limits)]
    if m.get("transportDeg") is not None:
        d = raster.angle_diff(m["transportDeg"], wind["to"])
        st = "limited" if d > cfg["directionMismatchWarningDeg"] else "supported"
        return [_f("MORPH-01", "meteorology", st,
                   f"Sayısal rasterdan kaynaktan ağırlıklı merkeze yön {_fmt(m['transportDeg'])}°; yeniden analiz taşınma "
                   f"yönü {_fmt(wind['to'])}°, fark {_fmt(d)}°" + (" — inceleme işareti." if st == "limited" else "."),
                   facts={**facts, "diffDeg": d}, limits=limits, inputs=[o["id"], f"raster:{numeric.get('sha256')}"],
                   refs=["B21", "P23"])]
    if m.get("axisDeg") is not None:
        d = raster.axial_diff(m["axisDeg"], wind["to"])
        st = "limited" if d > cfg["directionMismatchWarningDeg"] else "supported"
        return [_f("MORPH-01", "meteorology", st, f"Bulut ana ekseni {_fmt(m['axisDeg'])}° (yönsüz); rüzgârla eksen farkı "
                   f"{_fmt(d)}°.", facts={**facts, "axialDiffDeg": d}, limits=limits)]
    return [_f("MORPH-01", "meteorology", "insufficient_data", "Maskeden eksen çıkarılamadı.", facts=facts, limits=limits)]


def undetermined(o):
    return [_f("UND-01", "undetermined", "requires_validation",
               "Belirlenemeyenler: tesis/ekipman eşleşmesi, yıllık veya sürekli salım miktarı, salım nedeni ve "
               "bu sağlayıcı tahmininden bağımsız yeni bir Q.",
               limits=["Kaynak kümesi yarıçapı tesis doğrulaması değildir.",
                       "Tek kg/sa değeri 8760 ile çarpılarak yıllık salım üretilmez.",
                       "Pozitif bulut kataloğunda kayıt olmaması geçerli negatif gözlem değildir."],
               refs=["S23", "SC23"])]


def next_obs(o):
    sensor = (o.get("sensor") or "").lower()
    if "tanager" in sensor:
        txt = "Planet Tanager-1 hedefli çekim yapar; çekim planı kamuya açık değil. Yeni Carbon Mapper yayını izlenir."
    elif "emit" in sensor:
        txt = "EMIT ISS yörüngesinde hedefli çekim yapar; Türkiye geçişleri haritadaki geçiş listesinde, çekim garanti değildir."
    else:
        txt = "Bu sensör için sonraki uygun gözlem yayımlanınca günlük iş hattı yeniden değerlendirir."
    return [_f("NEXT-01", "next", "limited", txt + " Bölgesel tarama için Sentinel-5P günlük geçişi kullanılabilir; "
               "piksel ölçeği tesis bulutuyla aynı değildir.", refs=["SC23"])]


def summary(findings):
    """Şablon cümle yalnızca gerçekten hesaplanan koşullardan kurulur; eksik parça atlanır."""
    by = {f["ruleId"]: f for f in findings}
    parts = []
    if by.get("OBS-01", {}).get("status") == "supported":
        parts.append(by["OBS-01"]["claim"])
    h = next((f for f in findings if f["ruleId"] == "HIST-01" and f["status"] == "supported"), None)
    if h:
        parts.append(f"Karşılaştırmaya uygun {h['facts']['referenceN']} önceki gözleme göre: {h['claim']}")
    w = by.get("MET-02")
    if w and w["status"] != "insufficient_data":
        parts.append(f"Gözlem saatinde: {w['claim']}")
    lim = [f for f in findings if f["section"] == "meteorology" and f["status"] == "limited"]
    if lim:
        parts.append(f"Meteorolojik kısıt: {len(lim)} bulgu yorumu sınırlıyor.")
    return " ".join(parts) or None


def interpret(o, weather=None, numeric=None, classification=None, candidates=None, now=None):
    findings = (observed(o) + history(o, classification) + other_sensors(o, candidates) + meteorology(o, weather, numeric)
                + undetermined(o) + next_obs(o))
    refs = sorted({r for f in findings for r in f["referenceIds"]})
    order = {s: i for i, s in enumerate(SECTIONS)}
    findings.sort(key=lambda f: order[f["section"]])
    return {"method": METHOD, "policyVersion": policy.version(), "observationId": o["id"],
            "computedAt": store.iso(now or datetime.now(timezone.utc)), "summary": summary(findings),
            "findings": findings, "sections": list(SECTIONS),
            "references": [r for r in (policy.reference(x) for x in refs) if r],
            "llmUsed": False, "confidenceScore": None}
