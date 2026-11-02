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
# Kimlik/lisans hatası zamanla düzelmez: yalnızca başarılı kimlik doğrulamasından sonra yeniden kuyruğa alınır
CREDENTIAL_ERRORS = ("auth", "terms")
# Geçici hatalarda üstel geri çekilme tabanı ve toplam deneme sınırı
BACKOFF_BASE = {"quota": timedelta(minutes=30), "network": timedelta(minutes=5), "server": timedelta(minutes=10)}
BACKOFF_MAX = timedelta(hours=12)
MAX_TRANSIENT_ATTEMPTS = 6

_COLS = ("id", "kind", "state", "payload", "remote_id", "progress", "error_class", "error", "attempts",
         "created_at", "updated_at", "next_attempt_at", "lease_until")
_SELECT = f"SELECT {', '.join(_COLS)} FROM jobs"


def job_get(job_id):
    with db.read() as con:
        row = con.execute(f"{_SELECT} WHERE id = ?", (job_id,)).fetchone()
    return _job(row) if row else None


def _job(row):
    j = dict(zip(_COLS, row))
    j["payload"] = json.loads(j["payload"])
    return j


def backoff_until(error_class, attempts, t=None):
    """Geçici hata sonrası bir sonraki deneme zamanı; kimlik/lisans hatasında None (süreyle yeniden denenmez)."""
    base = BACKOFF_BASE.get(error_class)
    if base is None:
        return None
    return iso((t or now()) + min(BACKOFF_MAX, base * (2 ** max(0, attempts - 1))))


def job_fail(job_id, error_class, error, attempts):
    """Hata sınıfına göre durum: kimlik/lisans -> blocked; geçici -> geri çekilmeli queued; sınır aşılırsa failed."""
    if error_class in CREDENTIAL_ERRORS:
        job_update(job_id, state="blocked", error_class=error_class, error=str(error)[:200], attempts=attempts,
                   next_attempt_at=None, lease_until=None)
        return "blocked"
    if error_class in BACKOFF_BASE and attempts < MAX_TRANSIENT_ATTEMPTS:
        job_update(job_id, state="queued", error_class=error_class, error=str(error)[:200], attempts=attempts,
                   next_attempt_at=backoff_until(error_class, attempts), lease_until=None)
        return "queued"
    job_update(job_id, state="failed", error_class=error_class, error=str(error)[:200], attempts=attempts,
               lease_until=None)
    return "failed"


def requeue_after_credentials_ok(kinds):
    """Kimlik doğrulaması gerçekten başarılı olduktan sonra, yalnızca bu kaynağın kimlik/lisans hatasıyla
    bekleyen işlerini yeniden kuyruğa alır. Kota/ağ/boş sonuç işlerine dokunmaz. Kuyruğa alınan sayıyı döner."""
    marks = ",".join("?" * len(kinds))
    with db.tx() as con:
        cur = con.execute(
            f"""UPDATE jobs SET state='queued', next_attempt_at=NULL, lease_until=NULL, updated_at=?
                WHERE kind IN ({marks}) AND state='blocked' AND error_class IN ('auth','terms')""",
            (iso(now()), *kinds))
        return cur.rowcount


def job_claim(job_id, lease=timedelta(minutes=15)):
    """İşi çalışıyor olarak işaretler; süreç düşerse kira bitince recover_expired_leases yeniden kuyruğa alır."""
    job_update(job_id, state="running", lease_until=iso(now() + lease))


def recover_expired_leases():
    with db.tx() as con:
        return con.execute("UPDATE jobs SET state='queued', lease_until=NULL, updated_at=? "
                           "WHERE state='running' AND lease_until IS NOT NULL AND lease_until < ?",
                           (iso(now()), iso(now()))).rowcount


def job_enqueue(job_id, kind, payload, retry_failed_after=timedelta(hours=6)):
    """Aynı kimlikli iş varsa yeniden göndermez. Kimlik/lisans nedeniyle engellenen iş süreyle yeniden denenmez
    (bkz. requeue_after_credentials_ok); deneme sınırını aşmış geçici hata belirli süre sonra bir kez daha denenir."""
    t = iso(now())
    with db.tx() as con:
        row = con.execute("SELECT state, updated_at, error_class FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row:
            state, updated, error_class = row
            stale_fail = (state == "failed" and error_class not in CREDENTIAL_ERRORS
                          and updated < iso(now() - retry_failed_after))
            if not stale_fail:
                return False
            con.execute("UPDATE jobs SET state='queued', error_class=NULL, error=NULL, attempts=0, "
                        "next_attempt_at=NULL, updated_at=? WHERE id=?", (t, job_id))
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
    """Geri çekilme süresi dolmamış kuyruk işleri döndürülmez."""
    marks = ",".join("?" * len(states))
    with db.read() as con:
        rows = con.execute(
            f"""{_SELECT} WHERE kind = ? AND state IN ({marks})
                AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY created_at LIMIT ?""",
            (kind, *states, iso(now()), limit)).fetchall()
    return [_job(r) for r in rows]


def job_public(j):
    """İstemciye gönderilebilir iş özeti; sır veya yük ayrıntısı içermez."""
    if not j:
        return None
    return {"id": j["id"], "kind": j["kind"], "state": j["state"], "remoteId": j["remote_id"],
            "progress": j["progress"], "errorClass": j["error_class"], "updatedAt": j["updated_at"],
            "createdAt": j["created_at"], "nextAttemptAt": j.get("next_attempt_at")}
