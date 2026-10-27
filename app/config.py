import os
from pathlib import Path

# Türkiye ve çevresini kapsayan kutu: (lon_min, lat_min, lon_max, lat_max)
BBOX = (25.5, 35.5, 45.0, 42.5)

# Grid hücre boyutu (derece). 0.1° ≈ 9 x 11 km, TROPOMI pikseli 5.5 x 7 km.
GRID = float(os.environ.get("GRID_DEG", "0.1"))
NX = round((BBOX[2] - BBOX[0]) / GRID)
NY = round((BBOX[3] - BBOX[1]) / GRID)

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "metan.sqlite"
TMP_DIR = DATA_DIR / "tmp"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# Kaç gün geriye veri çekilsin (ilk açılışta ve her çalışmada kontrol edilir)
BACKFILL_DAYS = int(os.environ.get("BACKFILL_DAYS", "365"))
# Son NRT_DAYS gün NRT (hızlı) üründen, daha eskisi OFFL (kesin) üründen alınır
NRT_DAYS = int(os.environ.get("NRT_DAYS", "5"))
INGEST_INTERVAL_MIN = int(os.environ.get("INGEST_INTERVAL_MIN", "90"))

# TROPOMI CH4 ürün kılavuzunun önerdiği kalite eşiği
QA_MIN = 0.5

TROPOMI_OFFL = ("S5P_L2__CH4____HiR", "2")
TROPOMI_NRT = ("S5P_L2__CH4____HiR_NRT", "2")
EMIT_PLUMES = ("EMITL2BCH4PLM", "002")

# Yörünge tahmini yapılan uydular: ad, NORAD no, kapsama kutusu (Türkiye'yi görebildiği alt-nokta alanı)
SATELLITES = {
    "s5p": {
        "name": "Sentinel-5P (TROPOMI)",
        "norad": 42969,
        # 2600 km genişliğinde tarama: alt nokta Türkiye'nin ~8° doğusu/batısında olsa da ülkeyi görür
        "coverage": (18.0, 33.0, 53.0, 45.0),
    },
    "iss": {
        "name": "ISS (NASA EMIT)",
        "norad": 25544,
        # EMIT şeridi ~75 km, sadece hedefli çekim yapar: alt nokta Türkiye kutusunda olmalı
        "coverage": BBOX,
    },
}
