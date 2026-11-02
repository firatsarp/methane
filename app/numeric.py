"""Sağlayıcının sayısal artış rasterını (GeoTIFF) indirir, okur ve morfoloji sonucunu önbelleğe yazar.

Yalnızca kullanım koşulu elveren ve kimlik gerektirmeyen ürünler (şu an Carbon Mapper L3A IME CMF) otomatik alınır.
Yanıt PNG/RGB ise sayısal hesap yapılmaz. Sonuç "dosya biriminde" raporlanır; birim dosyada yoksa None kalır.
"""
import hashlib
import logging

from . import net, raster, store
from .retention import RASTER_DIR

log = logging.getLogger("numeric")

AUTO_PROVIDERS = {"Carbon Mapper"}


def _key(obs_id):
    return f"raster:{obs_id}:{raster.METHOD}"


def cached(obs_id):
    hit = store.cache_get(_key(obs_id))
    return hit[0] if hit else None


def request(o):
    """İstek anında: önbellekte varsa sonuç; yoksa arka plan işi kuyruğa alınır, indirme beklenmez."""
    hit = cached(o["id"])
    if hit:
        return hit
    res = analyse(o, allow_fetch=False)
    if res["status"] == "pending":
        store.job_enqueue(f"raster:{o['id']}", "raster", {"obsId": o["id"]})
    return res


def process_jobs(limit=3):
    from . import catalog
    for job in store.jobs_by_state("raster", ("queued",), limit=limit):
        o, _, _ = catalog.get(job["payload"]["obsId"])
        if not o:
            store.job_update(job["id"], state="failed", error_class="not_found", error="Gözlem yok")
            continue
        store.job_claim(job["id"])
        res = analyse(o, allow_fetch=True)
        if res["status"] in ("ok", "insufficient_data", "not_available", "unreadable", "requires_credentials"):
            store.job_update(job["id"], state="done", progress=1.0, lease_until=None)
        else:
            store.job_fail(job["id"], "network", res.get("reason"), job["attempts"] + 1)


def analyse(o, allow_fetch=True):
    """Morfoloji sonucu ya da durum sözlüğü. Ağ çağrısı yalnızca allow_fetch iken (pipeline/arka plan)."""
    hit = cached(o["id"])
    if hit:
        return hit
    nr = o.get("numericRaster") or {}
    if not nr.get("url"):
        return {"status": "not_available", "reason": "Kayıtta sayısal artış rasterı yok (yalnızca görüntü olabilir)."}
    if o.get("provider") not in AUTO_PROVIDERS:
        return {"status": "requires_credentials", "reason": f"{nr.get('product')}: otomatik indirme bağlanmadı."}
    if not allow_fetch:
        return {"status": "pending", "reason": "Sayısal raster henüz işlenmedi; günlük iş hattında hesaplanır."}
    try:
        data = net.request(nr["url"], raw=True, timeout=90)
    except net.ProviderError as e:
        return {"status": "unavailable", "reason": f"İndirilemedi ({e.kind}); imzalı bağlantının süresi dolmuş olabilir."}
    try:
        arr, geo, meta = raster.read_geotiff(data)
        morph = raster.morphology(arr, geo, origin=(o["lat"], o["lon"]))
    except (raster.RasterError, KeyError, ValueError) as e:
        return {"status": "unreadable", "reason": f"GeoTIFF okunamadı: {e}"}
    sha = hashlib.sha256(data).hexdigest()
    RASTER_DIR.mkdir(parents=True, exist_ok=True)
    (RASTER_DIR / f"{hashlib.sha256(o['id'].encode()).hexdigest()[:24]}.tif").write_bytes(data)
    result = {"status": morph["status"], "product": nr.get("product"), "sha256": sha, "bytes": len(data),
              "file": meta, "georef": geo, "morphology": morph, "computedAt": store.iso(store.now()),
              "unit": meta.get("unitInFile"),
              "maskNote": "Dosyadaki sonlu pikseller sağlayıcının maskelenmiş alanıdır; ayrı bir bulut kenarı maskesi "
                          "değildir. Ağırlıklı merkez ve eksen negatif değerler sıfırlanarak hesaplandı.",
              "limitations": ["Dosya metadata'sında birim yok; toplamlar dosya biriminde, fiziksel birime çevrilmedi."
                              if not meta.get("unitInFile") else f"Birim dosyadan: {meta['unitInFile']}.",
                              "Yeni salım (Q) veya IME hesabı yapılmadı: kalibre edilmiş Ueff/L ve doğrulanmış birim yok."]}
    store.cache_put(_key(o["id"]), result)
    return result
