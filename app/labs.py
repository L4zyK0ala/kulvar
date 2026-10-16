"""Kan değerlerinin seyri: tahlil belgelerinden sayısal sonuçları çıkarır (Claude), saklar ve grafikler.

Tahlil yorumlandıktan sonra ayrı ve kısa bir çağrıyla her sonuç kendi numune tarihiyle çıkarılır; rapor önceki
sonuçları da listeliyorsa onlar da alınır (tek tahlilden bile seyir oluşur). Önemli değerler standart anahtarla
eşlenir (aşağıdaki MARKERS); diğerleri "other:<ad>" olarak saklanır. Aynı değer (anahtar + tarih) farklı
belgelerde tekrar ederse bir kez tutulur.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re

import anthropic
from sqlalchemy.orm import Session

from . import i18n
from .clock import utcnow
from .config import settings
from .models import CoachMessage, HealthRecord, LabResult, Profile
from .ring_utils import sparkline_points

logger = logging.getLogger("kulvar.labs")

# anahtar → (Türkçe ad, İngilizce ad, grup). Sıra, ekrandaki ve koç özetindeki sıradır.
MARKERS = {
    "ferritin": ("Ferritin", "Ferritin", "iron"),
    "iron": ("Demir", "Iron", "iron"),
    "transferrin_sat": ("Transferrin satürasyonu", "Transferrin saturation", "iron"),
    "b12": ("B12 vitamini", "Vitamin B12", "vitamins"),
    "folate": ("Folat", "Folate", "vitamins"),
    "vitamin_d": ("D vitamini (25-OH)", "Vitamin D (25-OH)", "vitamins"),
    "hemoglobin": ("Hemoglobin", "Hemoglobin", "blood"),
    "hematocrit": ("Hematokrit", "Hematocrit", "blood"),
    "wbc": ("Lökosit", "White blood cells", "blood"),
    "platelets": ("Trombosit", "Platelets", "blood"),
    "alt": ("ALT", "ALT", "liver"),
    "ast": ("AST", "AST", "liver"),
    "ggt": ("GGT", "GGT", "liver"),
    "alp": ("ALP", "ALP", "liver"),
    "bilirubin": ("Total bilirubin", "Total bilirubin", "liver"),
    "glucose": ("Açlık glukozu", "Fasting glucose", "metabolic"),
    "hba1c": ("HbA1c", "HbA1c", "metabolic"),
    "insulin": ("İnsülin", "Insulin", "metabolic"),
    "total_cholesterol": ("Total kolesterol", "Total cholesterol", "lipids"),
    "ldl": ("LDL", "LDL", "lipids"),
    "hdl": ("HDL", "HDL", "lipids"),
    "triglycerides": ("Trigliserit", "Triglycerides", "lipids"),
    "tsh": ("TSH", "TSH", "thyroid"),
    "ft4": ("Serbest T4", "Free T4", "thyroid"),
    "ft3": ("Serbest T3", "Free T3", "thyroid"),
    "creatinine": ("Kreatinin", "Creatinine", "kidney"),
    "egfr": ("eGFR", "eGFR", "kidney"),
    "urea": ("Üre", "Urea", "kidney"),
    "uric_acid": ("Ürik asit", "Uric acid", "kidney"),
    "crp": ("CRP", "CRP", "inflammation"),
    "esr": ("Sedimantasyon", "ESR", "inflammation"),
    "ck": ("CK", "CK", "muscle"),
    "sodium": ("Sodyum", "Sodium", "electrolytes"),
    "potassium": ("Potasyum", "Potassium", "electrolytes"),
    "calcium": ("Kalsiyum", "Calcium", "electrolytes"),
    "magnesium": ("Magnezyum", "Magnesium", "electrolytes"),
    "testosterone": ("Testosteron", "Testosterone", "hormones"),
    "cortisol": ("Kortizol", "Cortisol", "hormones"),
}

_PROMPT = """Ekteki tahlil belgelerindeki SAYISAL sonuçları çıkar. Yorum yapma; sadece JSON döndür.

- Her sonuç için numunenin alındığı tarihi yaz (YYYY-MM-DD). Rapor önceki sonuçları da tarihleriyle listeliyorsa onları da ayrı satır olarak ekle.
- "key" alanında şu listeden uygun olanı kullan, listede yoksa null: {keys}
- Değer sayı olmalı (ondalık için nokta). "<5" gibi değerlerde sayıyı yaz. Sayı olmayan (pozitif/negatif) sonuçları atla.
- Referans aralığı varsa ref_low / ref_high (tek sınır varsa diğeri null).

[{{"key": "ferritin", "name": "Ferritin", "value": 45.2, "unit": "ng/mL", "ref_low": 30, "ref_high": 400, "date": "2026-10-03"}}, ...]
Belgenin varsayılan tarihi: {default_date}"""


# Kan tahlili yorumunun sonuna eklenen blok: PDF'ler yorum için zaten gönderildiğinden değerler aynı çağrıda
# çıkarılır (ayrı çağrı belgeleri ikinci kez gönderip maliyeti ikiye katlıyordu).
_BLOCK_RE = re.compile(r"<<<\s*DE[ĞG]ERLER\s*\n(.*?)\n?\s*>>>", re.S | re.I)
ANALYSIS_ADDENDUM = """

Yanıtının EN SONUNA, yorumdan ayrı olarak belgedeki tüm SAYISAL sonuçları şu blokla ekle (blok kullanıcıya gösterilmez):
<<<DEĞERLER
[{{"key": "ferritin", "name": "Ferritin", "value": 45.2, "unit": "ng/mL", "ref_low": 30, "ref_high": 400, "date": "YYYY-MM-DD"}}, ...]
>>>
- date: numunenin alındığı tarih; rapor önceki sonuçları tarihleriyle listeliyorsa onları da ayrı satır olarak ekle.
- key: şu listeden uygun olan, listede yoksa null: {keys}
- Değer sayı olmalı (ondalık için nokta); sayı olmayan sonuçları atla; referans tek sınırlıysa diğeri null."""


def analysis_addendum() -> str:
    return ANALYSIS_ADDENDUM.format(keys=", ".join(MARKERS))


def split_block(text: str) -> tuple[str | None, str]:
    """(değer bloğunun içeriği ya da None, bloğu çıkarılmış metin)."""
    m = _BLOCK_RE.search(text or "")
    if not m:
        return None, text
    return m.group(1), (text[:m.start()] + text[m.end():]).strip()


def label(key: str, lang: str) -> str:
    if key in MARKERS:
        return MARKERS[key][0 if lang == "tr" else 1]
    return key.split(":", 1)[-1]


def _num(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(value))
    return float(m.group(0).replace(",", ".")) if m else None


def parse(text: str, default_date: dt.date) -> list[dict]:
    """Claude'un JSON'unu temizler: geçersiz satırlar atılır, anahtarlar standartlaşır."""
    try:
        raw = json.loads(text[text.find("["): text.rfind("]") + 1])
    except ValueError:
        return []
    out, seen = [], set()
    for r in raw if isinstance(raw, list) else []:
        if not isinstance(r, dict):
            continue
        value, name = _num(r.get("value")), str(r.get("name") or "").strip()[:80]
        if value is None or not name:
            continue
        key = r.get("key") if r.get("key") in MARKERS else "other:" + re.sub(r"\s+", " ", name.lower())
        try:
            day = dt.date.fromisoformat(str(r.get("date"))[:10])
        except ValueError:
            day = default_date
        if (key, day) in seen:
            continue
        seen.add((key, day))
        out.append({"key": key, "name": name, "value": value, "unit": (str(r.get("unit") or "").strip()[:20] or None),
                    "ref_low": _num(r.get("ref_low")), "ref_high": _num(r.get("ref_high")), "date": day})
    return out


def extract(db: Session, profile: Profile, record: HealthRecord) -> int:
    """Belgedeki değerleri çıkarıp kaydeder (bu belgenin önceki çıkarımı silinir). Kaydedilen sayı."""
    from . import coach_chat, health

    blocks = health.content_blocks(record)
    if not blocks or not coach_chat.api_key_configured() or coach_chat.month_cost(db) >= settings.coach_monthly_budget_usd:
        return 0
    default_date = record.record_date or record.created_at.date()
    client = anthropic.Anthropic()
    with client.beta.messages.stream(
        model=settings.coach_model, max_tokens=16000,
        messages=[{"role": "user", "content": [*blocks, {"type": "text", "text": _PROMPT.format(
            keys=", ".join(MARKERS), default_date=default_date.isoformat())}]}],
        betas=["server-side-fallback-2026-07-01"], fallbacks="default",
    ) as stream:
        final = stream.get_final_message()
    thread = coach_chat.current_thread(db, profile)
    db.add(CoachMessage(thread_id=thread.id, profile_id=profile.id, seq=-1, role="health", content_json="[]",
                        model=final.model, stop_reason=final.stop_reason, cost_usd=coach_chat._cost(final.usage, final.model),
                        input_tokens=final.usage.input_tokens, output_tokens=final.usage.output_tokens))
    rows = parse("".join(b.text for b in final.content if b.type == "text"), default_date)
    record.labs_extracted_at = utcnow()
    return store(db, profile, record, rows)


def store(db: Session, profile: Profile, record: HealthRecord | None, rows: list[dict]) -> int:
    if record is not None:
        db.query(LabResult).filter(LabResult.record_id == record.id).delete()
    existing = {(r.key, r.date) for r in db.query(LabResult.key, LabResult.date).filter(LabResult.profile_id == profile.id)}
    added = 0
    for r in rows:
        if (r["key"], r["date"]) in existing:  # başka bir belgede zaten var (rapordaki önceki sonuç)
            continue
        db.add(LabResult(profile_id=profile.id, record_id=record.id if record else None, **r))
        existing.add((r["key"], r["date"]))
        added += 1
    db.commit()
    return added


def extract_quietly(record_id: str) -> None:
    """Arka plan: yorumdan sonra ya da ilk taramada; hata belgenin yorumunu etkilemez."""
    from .db import SessionLocal

    db = SessionLocal()
    try:
        record = db.get(HealthRecord, record_id)
        profile = db.get(Profile, record.profile_id) if record else None
        if record is not None and profile is not None and record.kind == "blood":
            n = extract(db, profile, record)
            logger.info("Tahlilden %d değer çıkarıldı (%s)", n, profile.name)
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("Tahlil değerleri çıkarılamadı")
    finally:
        db.close()


def backfill() -> None:
    """Açılışta bir kez: değeri çıkarılmamış kan tahlilleri taranır."""
    from .db import SessionLocal

    db = SessionLocal()
    try:
        pending = [r.id for r in db.query(HealthRecord).filter(HealthRecord.kind == "blood", HealthRecord.file_path.isnot(None),
                                                               HealthRecord.labs_extracted_at.is_(None))]
    finally:
        db.close()
    for rid in pending:
        extract_quietly(rid)


# --------------------------------------------------------------------------- görünüm ve koç

def series(db: Session, profile_id: str) -> dict[str, list[LabResult]]:
    rows = db.query(LabResult).filter(LabResult.profile_id == profile_id).order_by(LabResult.date).all()
    out: dict[str, list[LabResult]] = {}
    for r in rows:
        out.setdefault(r.key, []).append(r)
    return out


def flag(r: LabResult) -> str | None:
    if r.ref_high is not None and r.value > r.ref_high:
        return "high"
    if r.ref_low is not None and r.value < r.ref_low:
        return "low"
    return None


def view(db: Session, profile_id: str, lang: str) -> dict:
    """{"main": [...], "other": [...]} — her değer: ad, son sonuç, değişim, işaret, grafik verisi."""
    data = series(db, profile_id)

    def card(key, rows):
        last = rows[-1]
        prev = rows[-2] if len(rows) > 1 else None
        return {"key": key, "label": label(key, lang), "value": last.value, "unit": last.unit, "date": last.date,
                "flag": flag(last), "ref_low": last.ref_low, "ref_high": last.ref_high,
                "change": (last.value - prev.value) if prev else None, "prev_date": prev.date if prev else None,
                "points": [{"d": r.date.isoformat(), "v": r.value} for r in rows],
                "spark": sparkline_points([r.value for r in rows]) if len(rows) > 1 else None}

    main = [card(k, data[k]) for k in MARKERS if k in data]
    other = [card(k, v) for k, v in sorted(data.items()) if k not in MARKERS]
    return {"main": main, "other": other}


def brief_lines(db: Session, profile: Profile) -> list[str]:
    """Koç verisi: önemli değerlerin seyri (referans dışı olanlar işaretli)."""
    data = series(db, profile.id)
    lines = []
    for key in MARKERS:
        rows = data.get(key)
        if not rows:
            continue
        hist = " → ".join(f"{r.value:g} ({r.date:%d.%m.%Y})" for r in rows[-4:])
        last = rows[-1]
        ref = ""
        if last.ref_low is not None or last.ref_high is not None:
            ref = f", ref {'' if last.ref_low is None else f'{last.ref_low:g}'}–{'' if last.ref_high is None else f'{last.ref_high:g}'}"
        mark = {"high": " ↑ yüksek", "low": " ↓ düşük"}.get(flag(last) or "", "")
        lines.append(f"- {MARKERS[key][0]}: {hist} {last.unit or ''}{ref}{mark}".rstrip())
    return (["## Kan Değerleri (seyir)"] + lines + [""]) if lines else []
