"""Uydu çekimi (acquisition) kimliği. Aynı çekim farklı sağlayıcılardan gelirse tek bağımsız gözlemdir.

Kimlik yalnızca sağlayıcının yayımladığı özgün sahne/granül adlarından çıkarılır:
- Carbon Mapper EMIT plume_id: emi20220827t074030p05003-A  -> EMIT:20220827T074030:003
- NASA EMIT sahne adı:  EMIT_L2B_CH4ENH_V002_20220827T074030_2223905_003 -> aynı anahtar
- UNEP MARS tile:       EMIT_L1B_RAD_001_20220827T074030_2223905_003 -> aynı anahtar
Konum/zaman yakınlığı tek başına kimlik değildir; yalnızca "probable" eşleşme olarak işaretlenir (catalog.py).
"""
import re

_CM_EMIT = re.compile(r"^emi(\d{8})t(\d{6})p\d{2}(\d{3})", re.IGNORECASE)
_EMIT_SCENE = re.compile(r"EMIT_L\w+?_(\d{8})T(\d{6})_\d{7}_(\d{3})")
_ENMAP = re.compile(r"ENMAP01-_*L\w+-(DT\d+)_(\d{8}T\d{6})Z_(\d{3})")
_S2 = re.compile(r"^(S2[ABC])_MSI\w+_(\d{8}T\d{6})_\w+_(T\d{2}[A-Z]{3})")
_LANDSAT = re.compile(r"^(L[COT]0\d)_\w{4}_(\d{6})_(\d{8})")
_PRISMA = re.compile(r"^PRS_L\w+?_(\d{14})")
_CAMS = re.compile(r"^(\d+)_\d+_\d+_\d+_\d+_S5P$")
# Sentinel-5P L2 ürün adı: ..._<başlangıç>_<bitiş>_<yörünge>_<topla>_<işlemci>_<üretim>
_S5P = re.compile(r"^S5P_\w+?_L2__CH4_+(\d{8}T\d{6})_\d{8}T\d{6}_(\d{5})_")


def emit_key(ymd, hms, scene):
    return f"EMIT:{ymd}T{hms}:{scene}"


def from_cm(plume_id, instrument, scene_id):
    if instrument == "emi" and plume_id:
        m = _CM_EMIT.match(plume_id)
        if m:
            return emit_key(*m.groups())
    if scene_id:
        return f"{(instrument or 'cm').upper()}:{scene_id}"
    return None


def from_emit_scenes(scene_names):
    for s in scene_names or []:
        m = _EMIT_SCENE.search(s)
        if m:
            return emit_key(*m.groups())
    return None


def from_tile(tile):
    """MARS/CAMS ürün adları. Tanınmayan biçimde ham ürün adı kimlik olarak kullanılır (aynı ad = aynı ürün)."""
    if not tile:
        return None
    t = tile.strip()
    for rx, fmt in ((_EMIT_SCENE, lambda g: emit_key(*g)),
                    (_ENMAP, lambda g: f"ENMAP:{g[1]}:{g[2]}"),
                    (_S2, lambda g: f"S2:{g[0]}:{g[1]}:{g[2]}"),
                    (_LANDSAT, lambda g: f"LANDSAT:{g[0]}:{g[1]}:{g[2]}"),
                    (_PRISMA, lambda g: f"PRISMA:{g[0]}"),
                    (_S5P, lambda g: f"S5P:orbit{int(g[1])}"),
                    (_CAMS, lambda g: f"S5P:orbit{int(g[0])}")):
        m = rx.search(t)
        if m:
            return fmt(m.groups())
    return f"PRODUCT:{t}"
