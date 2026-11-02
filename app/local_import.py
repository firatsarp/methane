"""Yetkili/lisanslı yerel ürün dosyalarını içe aktarma (EnMAP, PRISMA, Landsat): kayıt, bütünlük, seviye tespiti.

Kullanıcı, hesabıyla resmî portaldan indirdiği dosyayı sunucuda DATA_DIR/import/<kaynak>/ altına koyar. Bu modül:
- dosyanın sha256 özetini, boyutunu, biçim imzasını (HDF5/ZIP/TIFF) ve ad kuralından ürün seviyesini kaydeder;
- ürün seviyesine göre yöntem uygunluğunu yazar (L2A yansıma, L1 radyans gibi işlenmez);
- metan çıkarımı yapmaz: sensöre özgü doğrulanmış tarif, SRF/kalibrasyon ve değerlendirme olmadan sonuç üretmez.
Otomatik indirme endpoint'i veya API anahtarı varsayılmaz.
"""
import hashlib
import re

from . import acquisition, store
from .config import DATA_DIR

IMPORT_DIR = DATA_DIR / "import"
KINDS = ("enmap", "prisma", "landsat")
MAGIC = {b"\x89HDF\r\n\x1a\n": "hdf5", b"PK\x03\x04": "zip", b"II*\x00": "tiff", b"MM\x00*": "tiff"}
LEVEL_PATTERNS = {
    "enmap": re.compile(r"ENMAP01-_*(L1B|L1C|L2A)", re.IGNORECASE),
    "prisma": re.compile(r"PRS_(L1|L2[BCD]?)_", re.IGNORECASE),
    "landsat": re.compile(r"^L[COT]0\d_(L1TP|L1GT|L1GS|L2SP|L2SR)", re.IGNORECASE),
}


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def eligibility(kind, level):
    """Ürün seviyesine göre hangi işlemin ön koşulu karşılanıyor. Hiçbiri doğrulanmış salım üretmez."""
    if level is None:
        return {"status": "unknown_level", "note": "Ürün seviyesi ad kuralından belirlenemedi; işlenmez."}
    lv = level.upper()
    if lv.startswith("L2"):
        return {"status": "not_radiance", "radianceMethods": False,
                "note": f"{lv} yüzey yansıma/türetilmiş üründür; matched filter gibi radyans yöntemlerine L1 radyans "
                        "gibi verilmez. Doğrulanmış sensöre özgü tarif gerekir."}
    if kind == "landsat":
        return {"status": "requires_validation", "radianceMethods": False,
                "note": "Çok bantlı SWIR yöntemleri (ör. bant oranı) yalnızca güçlü bulutlar için ve doğrulanmış "
                        "eşik/kalibrasyonla; doğal renk görüntü metan ürünü değildir."}
    return {"status": "requires_validation", "radianceMethods": True,
            "note": "L1 radyans: SRF, kalibrasyon, sahne geometrisi ve su buharı ile doğrulanmış tarif kurulmadan "
                    "metan çıkarımı çalıştırılmaz."}


def scan():
    """İçe aktarma dizinlerini tarar; yeni dosyaları kaydeder. Kayıt listesi döner (sır içermez)."""
    out = []
    for kind in KINDS:
        d = IMPORT_DIR / kind
        if not d.exists():
            continue
        for p in sorted(d.iterdir()):
            if not p.is_file() or p.suffix in (".part", ".tmp"):
                continue
            key = f"import:{kind}:{p.name}"
            hit = store.cache_get(key)
            size = p.stat().st_size
            if hit and hit[0].get("size") == size:
                out.append(hit[0])
                continue
            with open(p, "rb") as f:
                head = f.read(8)
            fmt = next((v for k, v in MAGIC.items() if head.startswith(k)), "unknown")
            m = LEVEL_PATTERNS[kind].search(p.name)
            level = m.group(1).upper() if m else None
            rec = {"kind": kind, "file": p.name, "size": size, "sha256": _sha(p), "format": fmt, "level": level,
                   "acquisitionId": acquisition.from_tile(p.stem), "registeredAt": store.iso(store.now()),
                   "eligibility": eligibility(kind, level), "methaneProduct": None}
            store.cache_put(key, rec)
            out.append(rec)
    return out
