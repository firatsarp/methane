"""Sürümlü bilimsel kurallar, kaynakça ve veri kaynağı envanteri (app/policy/*.json).

Sayısal eşikler proje başlangıç tercihleridir (thresholdsAreOperationalDefaults); makalelerden alınmış evrensel
bilimsel sınır değildir. Değer değiştirilirse JSON'daki schemaVersion artırılır ve gerekçe yazılır.
"""
import json
import os
from functools import lru_cache
from pathlib import Path

POLICY_DIR = Path(__file__).resolve().parent / "policy"


@lru_cache(maxsize=None)
def _load(name):
    return json.loads((POLICY_DIR / f"{name}.json").read_text(encoding="utf-8"))


def rules():
    return _load("scientific-rules")


def references():
    return _load("references")


def data_sources():
    return _load("data-sources")


def version():
    return f"rules-{rules()['schemaVersion']}"


def retention_days():
    return int(rules()["retention"]["ordinaryObservationDays"])


def peak():
    """Pik politikası; mutlak kg/sa eşiği yalnızca ortam değişkeniyle açıkça verilirse etkinleşir."""
    p = dict(rules()["peakPolicy"])
    raw = os.environ.get("PEAK_ABSOLUTE_KGH", "").strip()
    p["absoluteEmissionThresholdKgh"] = float(raw) if raw else p.get("absoluteEmissionThresholdKgh")
    return p


def meteorology():
    return rules()["meteorology"]


def comparisons():
    return rules()["comparisons"]


def reference(ref_id):
    """Kullanıcıya gösterilecek kısa kaynak kaydı; iç kod (G21 vb.) metne konmaz, yalnızca bağlantı için kullanılır."""
    refs = references()
    for s in refs["studies"]:
        if s["id"] == ref_id:
            return {"id": s["id"], "title": s["title"], "author": s["author"], "year": s["year"], "url": s["url"]}
    for s in refs["officialSources"]:
        if s["id"] == ref_id:
            return {"id": s["id"], "title": s.get("title") or s.get("name"), "url": s.get("url")}
    return None
