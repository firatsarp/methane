"""Günlük iş hattı: katalog değişikliklerini al -> kanonikleştir/tekilleştir -> pik/yüksek olay sınıflandırması ->
gözlem anı meteorolojisi ve sayısal raster -> yorum/karşılaştırma -> kalıcı paket -> 90 gün temizliği ->
veri durumu ve gözlemden yayıma gecikme raporu.

Aynı anda tek çalışma (pipeline_runs kira kaydı). Tekrarlanan çalışma aynı olay/dosyayı çoğaltmaz (upsert, sha256).
Ağ yoğun adımlar tur başına sınırlıdır; kalanlar sonraki turda tamamlanır. Bildirim yalnızca site içidir.
"""
import json
import logging
import statistics
from datetime import timedelta

from . import (archive, cams, catalog, comparison, db, emit, ingest, interpret, mars, numeric, peaks, retention,
               sources, store, weather)

log = logging.getLogger("pipeline")

LEASE = timedelta(hours=2)
MAX_EVENT_ENRICH_PER_RUN = 40      # Open-Meteo ücretsiz kotası ve sağlayıcı nezaketi için
MAX_RECENT_ANALYSIS_PER_RUN = 40
HIGH_STATES = (peaks.CANDIDATE, peaks.REVIEWED, peaks.REFERENCE)


def _acquire(now):
    with db.tx() as con:
        busy = con.execute("SELECT id FROM pipeline_runs WHERE state = 'running' AND lease_until > ?",
                           (store.iso(now),)).fetchone()
        if busy:
            return None
        con.execute("UPDATE pipeline_runs SET state = 'abandoned' WHERE state = 'running'")
        cur = con.execute("INSERT INTO pipeline_runs(started_at, state, lease_until) VALUES(?, 'running', ?)",
                          (store.iso(now), store.iso(now + LEASE)))
        return cur.lastrowid


def _finish(run_id, state, report):
    with db.tx() as con:
        con.execute("UPDATE pipeline_runs SET state = ?, finished_at = ?, lease_until = NULL, report = ? WHERE id = ?",
                    (state, store.iso(store.now()), json.dumps(report, ensure_ascii=False, default=str), run_id))


def _stored(table):
    with db.read() as con:
        return con.execute(f"SELECT obs, tr FROM {table}").fetchall()


def build_items(cm_rows, emit_rows, mars_items, cams_items):
    """Bellekteki tam küme + saklanan kayıtlar + kalıcı arşiv -> Türkiye kanonik gözlemleri."""
    with db.read() as con:
        srcs = [json.loads(r[0]) for r in con.execute("SELECT obs FROM cm_sources")]
        if cm_rows is None:
            cm_rows = con.execute("SELECT id, png, bounds, obs, tr FROM cm_plumes").fetchall()
        else:
            # ingest_plumes tam satır döndürür: (id, observed, lat, lon, tr, ..., png, bounds, obs)
            cm_rows = [(r[0], r[10], r[11], r[12], r[4]) for r in cm_rows]
    source_of = {pid: s["id"] for s in srcs for pid in s.get("plumeIds", [])}
    if emit_rows is None:
        emit_rows = emit.stored_rows()

    def plain(items, table):
        if items is None:
            return [catalog._plain_item(*r) for r in _stored(table)]
        return [catalog._plain_item(json.dumps({k: v for k, v in o.items() if k != "tr"}, ensure_ascii=False),
                                    o.get("tr")) for o in items]
    items = catalog.compose([catalog._cm_item(r, source_of) for r in cm_rows],
                            [catalog._emit_item(r) for r in emit_rows],
                            plain(mars_items, "mars_plumes"), plain(cams_items, "cams_hotspots"))
    present = {o["id"] for o in items}
    for pid, (state, o) in catalog._archived().items():
        if pid not in present:
            items.append({**o, "fromArchive": True})
    return [o for o in items if o.get("tr")]


def save_analysis(o, method, result):
    with db.tx() as con:
        con.execute("INSERT OR REPLACE INTO analysis_results(obs_id, method_version, observed, computed_at, result) "
                    "VALUES(?,?,?,?,?)", (o["id"], method, o.get("observedAt"), store.iso(store.now()),
                                          json.dumps(result, ensure_ascii=False, default=str)))


def load_analysis(obs_id, method):
    with db.read() as con:
        row = con.execute("SELECT result FROM analysis_results WHERE obs_id = ? AND method_version = ?",
                          (obs_id, method)).fetchone()
    return json.loads(row[0]) if row else None


def enrich(o, items, cls, fetch_assets=True):
    """Gözlem anı meteorolojisi, sayısal raster ve yorum. Ağ hatası yorumu insufficient_data olarak tamamlar."""
    try:
        w = weather.observation_weather(o) if o.get("observedAt") else None
    except Exception as e:  # meteoroloji yoksa analiz eksik girdiyle tamamlanır, iş hattı durmaz
        log.warning("Meteoroloji alınamadı %s: %s", o["id"], type(e).__name__)
        w = {"status": "unavailable"}
    num = numeric.analyse(o, allow_fetch=True)
    analysis = interpret.interpret(o, w, num, cls, comparison.candidates(o, items))
    save_analysis(o, interpret.METHOD, analysis)
    assets = archive.fetch_assets(o) if fetch_assets else None
    return w, analysis, assets


def _neg(iso):
    """En yeni önce sıralamak için zaman anahtarı (zamansız kayıt sona)."""
    t = retention._parse(iso)
    return -t.timestamp() if t else float("inf")


def latency_report(items):
    """Gözlemden yayıma gecikme (gün), sağlayıcı başına medyan. Yalnızca iki zamanı da olan kayıtlar."""
    by = {}
    for o in items:
        a, b = o.get("observedAt"), o.get("publishedAt")
        if a and b:
            ta, tb = retention._parse(a), retention._parse(b)
            if ta and tb:
                by.setdefault(o.get("provider"), []).append((tb - ta).total_seconds() / 86400)
    out = {p: {"n": len(v), "medianPublicationLagDays": round(statistics.median(v), 1)} for p, v in by.items()}
    # Geri doldurma kapsamı: sağlayıcı başına gözlem tarih aralığı ve kayıt sayısı (eksikler görünür olsun)
    for o in items:
        p = out.setdefault(o.get("provider"), {"n": 0, "medianPublicationLagDays": None})
        t = o.get("observedAt")
        if t:
            p["firstObserved"] = min(p.get("firstObserved") or t, t)
            p["lastObserved"] = max(p.get("lastObserved") or t, t)
        p["records"] = p.get("records", 0) + 1
    return out


def run(now=None):
    now = now or store.now()
    run_id = _acquire(now)
    if run_id is None:
        log.info("İş hattı zaten çalışıyor; bu tur atlandı")
        return None
    report = {"startedAt": store.iso(now)}
    try:
        store.recover_expired_leases()
        protected = retention.protected_ids()
        cm_rows = ingest.ingest_plumes(protected)
        emit_rows = emit.ingest(protected)
        mars_items = mars.ingest(protected, now)
        cams_items = cams.ingest(protected, now)
        report["fetched"] = {"carbonmapper": None if cm_rows is None else len(cm_rows),
                             "emit": None if emit_rows is None else len(emit_rows),
                             "mars": "unchanged_or_failed" if mars_items is None else len(mars_items),
                             "cams": None if cams_items is None else len(cams_items)}
        items = build_items(cm_rows, emit_rows, mars_items, cams_items)
        cls, group_providers = peaks.classify(items, archive.reviews())
        # Tam listesi bu turda alınamayan kaynağın serileri eksiktir: o serilerde yeni referans açılmaz, mevcut
        # referans devre dışı bırakılmaz (ör. MARS dosyası değişmedi/indirilemedi, Carbon Mapper hatası)
        complete = {p for p, ok in (("Carbon Mapper", cm_rows is not None), ("UNEP IMEO MARS", mars_items is not None),
                                    ("CAMS (Copernicus)", cams_items is not None)) if ok}
        evaluated = {g for g, p in group_providers.items() if p in complete}
        report["completeProviders"] = sorted(complete)
        with db.read() as con:
            first_run = not con.execute("SELECT 1 FROM pipeline_runs WHERE state = 'done' LIMIT 1").fetchone()
        for c in cls.values():
            c["backfill"] = first_run
        report["backfill"] = first_run
        for o in items:
            save_analysis(o, peaks.METHOD, cls[o["id"]])
        with db.read() as con:
            already = {r[0] for r in con.execute("SELECT id FROM peak_events WHERE active = 1")}

        def eligible(o):
            c = cls[o["id"]]
            if c["state"] not in HIGH_STATES:
                return False
            if c["state"] != peaks.REFERENCE or o["id"] in already:
                return True
            return any(r.get("group") in evaluated for r in c["reasons"] if r["rule"] == "sparse_top_n")
        high = [o for o in items if eligible(o)]
        report["classification"] = {s: sum(1 for c in cls.values() if c["state"] == s)
                                    for s in (peaks.ORDINARY, *HIGH_STATES, peaks.REJECTED)}
        with db.read() as con:
            # Paketinde yorum analizi olan olay yeniden zenginleştirilmez (meteoroloji/dosya tekrar indirilmez)
            existing = {r[0]: bool(r[1]) for r in con.execute(
                "SELECT id, json_extract(bundle, '$.analysis') IS NOT NULL FROM peak_events")}
        results, enriched = {}, 0
        # Önce henüz zenginleştirilmemiş olaylar, en yeni gözlem önce
        for o in sorted(high, key=lambda x: (existing.get(x["id"], False), _neg(x.get("observedAt")))):
            need = not existing.get(o["id"], False)
            w = analysis = assets = None
            if need and enriched < MAX_EVENT_ENRICH_PER_RUN:
                w, analysis, assets = enrich(o, items, cls[o["id"]])
                enriched += 1
            r = archive.upsert(o, cls[o["id"]], weather=w, analysis=analysis, assets=assets, now=now)
            results[r] = results.get(r, 0) + 1
        report["archive"] = {**results, "enriched": enriched}
        # Değerlendirilen serilerde artık top-N'de olmayan referanslar etkin olmaktan çıkar
        high_ids = {o["id"] for o in high}
        with db.read() as con:
            stale = [r[0] for r in con.execute(
                "SELECT id, group_key FROM peak_events WHERE active = 1 AND state = 'historical_high_reference'")
                if r[0] not in high_ids and r[1] in evaluated]
        report["deactivated"] = archive.deactivate(stale)
        # Bu turda arşive giren eski kayıtlar kaynak tablolarına da yazılır (pencere dışı olsalar bile korunur)
        protected = retention.protected_ids()
        report["persistedProtected"] = {"carbonmapper": ingest.persist_protected_cm(cm_rows, protected),
                                        "emit": emit.persist_protected(protected),
                                        "mars": mars.persist_protected(mars_items, protected, now),
                                        "cams": cams.persist_protected(cams_items, protected, now)}
        # Pencere içi normal gözlemler için yorum (sınırlı sayıda; kalanlar istek anında veya sonraki turda)
        recent = [o for o in items if not o.get("fromArchive") and not retention.is_expired(o.get("observedAt"), now)
                  and o["id"] not in high_ids and load_analysis(o["id"], interpret.METHOD) is None]
        recent.sort(key=lambda x: x.get("observedAt") or "", reverse=True)
        for o in recent[:MAX_RECENT_ANALYSIS_PER_RUN]:
            enrich(o, items, cls[o["id"]], fetch_assets=False)
        report["recentAnalysed"] = min(len(recent), MAX_RECENT_ANALYSIS_PER_RUN)
        report["retention"] = retention.run(now)
        report["latency"] = latency_report(items)
        if report["retention"]["verificationErrors"]:
            sources.failed("retention", "Arşiv paketi bütünlük hatası; sahipsiz dosya temizliği ertelendi",
                           error_class="maintenance")
        else:
            sources.succeeded("retention", last_cutoff=report["retention"]["cutoff"])
        _finish(run_id, "done", report)
        sources.succeeded("pipeline", last_report_id=run_id)
        log.info("İş hattı tamamlandı: %s", {k: report[k] for k in ("fetched", "classification", "archive")})
        return report
    except Exception as e:
        log.exception("İş hattı başarısız")
        report["error"] = f"{type(e).__name__}: {e}"[:300]
        _finish(run_id, "failed", report)
        sources.failed("pipeline", e, error_class="maintenance")
        return report


def last_runs(limit=5):
    with db.read() as con:
        rows = con.execute("SELECT id, started_at, finished_at, state, report FROM pipeline_runs ORDER BY id DESC LIMIT ?",
                           (limit,)).fetchall()
    return [{"id": r[0], "startedAt": r[1], "finishedAt": r[2], "state": r[3],
             "report": json.loads(r[4]) if r[4] else None} for r in rows]
