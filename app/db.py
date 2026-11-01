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
"""


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
