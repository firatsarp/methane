import sqlite3
import threading
from contextlib import contextmanager

from .config import DB_PATH

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS granules (
    id TEXT PRIMARY KEY,
    product TEXT NOT NULL,
    day TEXT NOT NULL,
    n_pixels INTEGER NOT NULL,
    processed_at TEXT NOT NULL
);
-- Günlük grid toplamları. source: 'NRTI' veya 'OFFL'. Aynı gün için OFFL varsa o kullanılır.
CREATE TABLE IF NOT EXISTS cells (
    day TEXT NOT NULL,
    source TEXT NOT NULL,
    j INTEGER NOT NULL,
    i INTEGER NOT NULL,
    tr INTEGER NOT NULL,
    s REAL NOT NULL,
    n INTEGER NOT NULL,
    PRIMARY KEY (day, source, j, i)
);
-- Eski NASA EMIT bulut tablosu, yerini Carbon Mapper aldı
DROP TABLE IF EXISTS plumes;
-- Carbon Mapper bulutları. Carbon Mapper tahmini yayımlamadıysa emission NULL.
-- png süreli imzalı bir bağlantı olduğu için tablo her gün yenilenir. bounds: JSON [batı, güney, doğu, kuzey].
CREATE TABLE IF NOT EXISTS cm_plumes (
    id TEXT PRIMARY KEY,
    observed TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    tr INTEGER NOT NULL,
    emission REAL,
    uncertainty REAL,
    sector TEXT,
    platform TEXT,
    wind_speed REAL,
    png TEXT,
    bounds TEXT,
    obs TEXT NOT NULL
);
-- Bir bulut kaydının sisteme ilk alındığı zaman. cm_plumes her gün yeniden yazıldığı için ayrı tutulur.
-- baseline=1: ilk doldurmada görülen kayıtlar, "yeni kayıt" uyarısı üretmez.
CREATE TABLE IF NOT EXISTS plume_seen (
    id TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL,
    baseline INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
-- NASA EMIT incelenmiş bulut kompleksleri (EMITL2BCH4PLM). footprint: GeoJSON Polygon. obs: normalize gözlem.
CREATE TABLE IF NOT EXISTS emit_plumes (
    id TEXT PRIMARY KEY,
    observed TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    tr INTEGER NOT NULL,
    footprint TEXT,
    obs TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
-- Sağlayıcının bir kaydı değiştirmesi (modified/sürüm/emisyon) ayrı güncelleme olayı olarak izlenir
CREATE TABLE IF NOT EXISTS plume_versions (
    id TEXT NOT NULL,
    modified TEXT NOT NULL,
    version TEXT,
    emission REAL,
    seen_at TEXT NOT NULL,
    PRIMARY KEY (id, modified)
);
-- Carbon Mapper kaynak özetleri (birden çok bulutun sağlayıcı tarafından kümelenmesi). Tek bulutlardan ayrı tutulur.
CREATE TABLE IF NOT EXISTS cm_sources (
    id TEXT PRIMARY KEY,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    tr INTEGER NOT NULL,
    obs TEXT NOT NULL
);
-- Sağlayıcı yanıt önbelleği (meteoroloji, iklim, yüzey görüntüsü metadata'sı). expires_at NULL: kalıcı.
CREATE TABLE IF NOT EXISTS cache (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    expires_at TEXT
);
-- Uzun süren arka plan işleri (CDS ERA5, iklim serisi, yüzey görüntüsü). key aynı iş tekrar gönderilmesin diye tekildir.
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    payload TEXT NOT NULL,
    remote_id TEXT,
    progress REAL,
    error_class TEXT,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_kind_state ON jobs(kind, state);
-- UNEP IMEO MARS yayımlanmış bulutları ve CAMS TROPOMI sıcak noktaları (normal kayıtlar; 90 gün penceresi)
CREATE TABLE IF NOT EXISTS mars_plumes (
    id TEXT PRIMARY KEY,
    observed TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    tr INTEGER NOT NULL,
    obs TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cams_hotspots (
    id TEXT PRIMARY KEY,
    observed TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    tr INTEGER NOT NULL,
    obs TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
-- Artımlı alma: işlenmiş sağlayıcı öğesi ve revizyonu. Ölçüm değeri içermez.
CREATE TABLE IF NOT EXISTS ingest_checkpoints (
    source TEXT NOT NULL,
    item_id TEXT NOT NULL,
    revision TEXT,
    processed_at TEXT NOT NULL,
    PRIMARY KEY (source, item_id)
);
-- KALICI yüksek olay arşivi. Sağlayıcı önbelleklerinden (cm_plumes vb.) bağımsızdır; günlük yeniden yazım ve
-- 90 gün temizliği bu tablolara dokunmaz. active=0: top-N'den çıkmış ve başka koruma gerekçesi olmayan kayıt.
CREATE TABLE IF NOT EXISTS peak_events (
    id TEXT PRIMARY KEY,
    group_key TEXT NOT NULL,
    state TEXT NOT NULL,
    reasons TEXT NOT NULL,
    quantity_kind TEXT,
    value REAL,
    unit TEXT,
    observed TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    tr INTEGER NOT NULL,
    bundle TEXT NOT NULL,
    bundle_sha TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    review TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS peak_events_group ON peak_events(group_key, active);
-- Sağlayıcı revizyonları: özgün ve sonraki sürümler düzeltme geçmişiyle korunur
CREATE TABLE IF NOT EXISTS peak_event_revisions (
    event_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    provider_modified TEXT,
    content_sha TEXT NOT NULL,
    obs TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (event_id, revision)
);
-- Arşiv dosyaları içerik özetiyle (sha256) tekil tutulur; birden çok olay aynı dosyayı paylaşabilir
CREATE TABLE IF NOT EXISTS archive_assets (
    sha256 TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    size INTEGER NOT NULL,
    media_type TEXT,
    license TEXT,
    fetched_at TEXT NOT NULL
);
-- Olay -> dosya bağı. sha256 NULL: dosya arşivlenemedi; neden status/note alanındadır
CREATE TABLE IF NOT EXISTS archive_asset_refs (
    event_id TEXT NOT NULL,
    role TEXT NOT NULL,
    sha256 TEXT,
    status TEXT NOT NULL,
    source_url TEXT,
    note TEXT,
    PRIMARY KEY (event_id, role)
);
-- Yorum motoru sonuçları (normal gözlemler için 90 gün penceresine tabidir)
CREATE TABLE IF NOT EXISTS analysis_results (
    obs_id TEXT NOT NULL,
    method_version TEXT NOT NULL,
    observed TEXT,
    computed_at TEXT NOT NULL,
    result TEXT NOT NULL,
    PRIMARY KEY (obs_id, method_version)
);
CREATE TABLE IF NOT EXISTS maintenance_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    state TEXT NOT NULL,
    lease_until TEXT,
    report TEXT
);
"""

# Var olan tabloya yıkıcı olmayan sütun eklemeleri (tablo, sütun, tanım)
COLUMN_MIGRATIONS = [
    ("jobs", "next_attempt_at", "TEXT"),
    ("jobs", "lease_until", "TEXT"),
]
SCHEMA_VERSION = "2"


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init():
    with tx() as con:
        # cm_plumes her gün baştan yazılan bir önbellek; eski sürümde obs sütunu yoksa yeniden kurulur
        cols = {r[1] for r in con.execute("PRAGMA table_info(cm_plumes)")}
        if cols and "obs" not in cols:
            con.execute("DROP TABLE cm_plumes")
        con.executescript(SCHEMA)
        for table, col, decl in COLUMN_MIGRATIONS:
            if col not in {r[1] for r in con.execute(f"PRAGMA table_info({table})")}:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        con.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (SCHEMA_VERSION,))


@contextmanager
def tx():
    with _lock:
        con = connect()
        try:
            yield con
            con.commit()
        finally:
            con.close()


@contextmanager
def read():
    con = connect()
    try:
        yield con
    finally:
        con.close()


def set_meta(key, value):
    with tx() as con:
        con.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def get_meta():
    with read() as con:
        return dict(con.execute("SELECT key, value FROM meta").fetchall())
