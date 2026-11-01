"""Veri kaynağı başına son kontrol, son başarılı alma ve son gözlem zamanları (meta tablosunda JSON)."""
import json
from datetime import datetime, timezone

from . import db

KEY = "source:{}"


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def update(name, **fields):
    """Var olan durumun üstüne yazar; None verilen alanlar silinmez."""
    current = get(name)
    current.update({k: v for k, v in fields.items() if v is not None})
    db.set_meta(KEY.format(name), json.dumps(current, ensure_ascii=False))


def checked(name, **fields):
    update(name, last_check=now_iso(), **fields)


def succeeded(name, **fields):
    t = now_iso()
    update(name, last_check=t, last_ok=t, state="ok", error="", error_class="", **fields)


def failed(name, error, **fields):
    # Sağlayıcı hatasının sınıfı (auth, quota, terms...) ayrı alanda tutulur; metin istemciye kısaltılarak gider
    fields.setdefault("error_class", getattr(error, "kind", "error"))
    update(name, last_check=now_iso(), state="error", error=str(error)[:300], **fields)


def get(name):
    raw = db.get_meta().get(KEY.format(name))
    return json.loads(raw) if raw else {}
