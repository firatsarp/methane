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
CREATE TABLE IF NOT EXISTS plumes (
    id TEXT PRIMARY KEY,
    observed TEXT,
    lat REAL,
    lon REAL,
    tr INTEGER NOT NULL,
    max_ppmm REAL,
    props TEXT,
    geometry TEXT
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
