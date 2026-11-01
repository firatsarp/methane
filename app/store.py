"""Kalıcı önbellek ve iş kuyruğu yardımcıları (SQLite)."""
import json
from datetime import datetime, timedelta, timezone

from . import db


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z") if dt else None


# ---- Önbellek ---------------------------------------------------------------------

def cache_get(key, allow_expired=False):
    """(değer, fetched_at, süresi_doldu_mu) ya da None."""
    with db.read() as con:
        row = con.execute("SELECT value, fetched_at, expires_at FROM cache WHERE key = ?", (key,)).fetchone()
    if not row:
        return None
    value, fetched, expires = row
    expired = bool(expires) and expires < iso(now())
    if expired and not allow_expired:
        return None
    return json.loads(value), fetched, expired


def cache_put(key, value, ttl=None):
    t = now()
    with db.tx() as con:
        con.execute(
            """INSERT INTO cache(key, value, fetched_at, expires_at) VALUES(?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, fetched_at=excluded.fetched_at,
               expires_at=excluded.expires_at""",
            (key, json.dumps(value, ensure_ascii=False), iso(t), iso(t + ttl) if ttl else None),
        )
    return iso(t)


# ---- İşler ---------------------------------------------------------------------------
# state: queued | submitted | running | done | failed | blocked
# "blocked": erişim/koşul eksik; kullanıcıya kesin neden gösterilir, yeniden denemek için yapılandırma gerekir.

ACTIVE = ("queued", "submitted", "running")


def job_get(job_id):
    with db.read() as con:
        row = con.execute(
            """SELECT id, kind, state, payload, remote_id, progress, error_class, error, attempts, created_at, updated_at
               FROM jobs WHERE id = ?""", (job_id,)).fetchone()
    return _job(row) if row else None


def _job(row):
    keys = ("id", "kind", "state", "payload", "remote_id", "progress", "error_class", "error", "attempts",
            "created_at", "updated_at")
    j = dict(zip(keys, row))
    j["payload"] = json.loads(j["payload"])
    return j


def job_enqueue(job_id, kind, payload, retry_failed_after=timedelta(hours=6)):
    """Aynı kimlikli iş varsa yeniden göndermez. Başarısız iş, belirli süre geçtiyse yeniden kuyruğa alınır."""
    t = iso(now())
    with db.tx() as con:
        row = con.execute("SELECT state, updated_at FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row:
            state, updated = row
            stale_fail = state in ("failed", "blocked") and updated < iso(now() - retry_failed_after)
            if not stale_fail:
                return False
            con.execute("UPDATE jobs SET state='queued', error_class=NULL, error=NULL, updated_at=? WHERE id=?",
                        (t, job_id))
            return True
        con.execute(
            "INSERT INTO jobs(id, kind, state, payload, created_at, updated_at) VALUES(?,?,?,?,?,?)",
            (job_id, kind, "queued", json.dumps(payload), t, t),
        )
    return True


def job_update(job_id, **fields):
    if not fields:
        return
    fields["updated_at"] = iso(now())
    if "payload" in fields:
        fields["payload"] = json.dumps(fields["payload"])
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db.tx() as con:
        con.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))


def jobs_by_state(kind, states, limit=50):
    marks = ",".join("?" * len(states))
    with db.read() as con:
        rows = con.execute(
            f"""SELECT id, kind, state, payload, remote_id, progress, error_class, error, attempts, created_at, updated_at
                FROM jobs WHERE kind = ? AND state IN ({marks}) ORDER BY created_at LIMIT ?""",
            (kind, *states, limit)).fetchall()
    return [_job(r) for r in rows]


def job_public(j):
    """İstemciye gönderilebilir iş özeti; sır veya yük ayrıntısı içermez."""
    if not j:
        return None
    return {"id": j["id"], "kind": j["kind"], "state": j["state"], "remoteId": j["remote_id"],
            "progress": j["progress"], "errorClass": j["error_class"], "updatedAt": j["updated_at"],
            "createdAt": j["created_at"]}
