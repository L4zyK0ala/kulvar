"""Sağlık kontrolleri: periyodik hatırlatmalar, tahlil/rapor saklama ve Claude ile yorumlama.

Claude teşhis koymaz: sonuçları önceki kayıtlarla karşılaştırır, doktora sorulacak soruları
hazırlar ve koça antrenman açısından kısa bir not bırakır. Doktor notları koçun tıbbi
kurallarının önüne geçer (bkz. coach_chat._BASE_SYSTEM).
"""
from __future__ import annotations

import base64
import calendar
import datetime as dt
import json
import logging
import re
import uuid
from pathlib import Path

import anthropic
from sqlalchemy.orm import Session

from .clock import utcnow
from . import i18n
from .config import settings
from .models import CoachMessage, HealthRecord, HealthSchedule, Profile

logger = logging.getLogger("kulvar.health")

DATA_DIR = Path(__file__).resolve().parent / "data" / "health"

_FORMAT_RULES = {
    "tr": """Yanıtını Türkçe ve tam olarak şu bölümlerle yaz:
TARİH: belgedeki tahlil/muayene tarihi (YYYY-AA-GG); okunamıyorsa "bilinmiyor"
## Özet
3-5 madde.
## Tablo
| Parametre | Değer | Referans | Önceki | Değişim |
(Sadece belgede gerçekten olan değerler; tahmin etme.)
## Yorum
Parametre bazında kısa yorum; önceki sonuçlarla trendi belirt. Teşhis koyma.
## Doktora sorulacak sorular
3-6 soru.
KOÇ İÇİN NOT: antrenmanı etkileyen 1-3 cümle (yoksa "Antrenmana etkisi yok, mevcut kurallar geçerli.")""",
    "en": """Yanıtını İngilizce (English) ve tam olarak şu bölümlerle yaz (başlıkları aynen kullan):
DATE: belgedeki tahlil/muayene tarihi (YYYY-AA-GG); okunamıyorsa "unknown"
## Summary
3-5 madde.
## Table
| Parameter | Value | Reference | Previous | Change |
(Sadece belgede gerçekten olan değerler; tahmin etme.)
## Interpretation
Parametre bazında kısa yorum; önceki sonuçlarla trendi belirt. Teşhis koyma.
## Questions for your doctor
3-6 soru.
COACH NOTE: antrenmanı etkileyen 1-3 cümle (yoksa "No effect on training, current rules apply.")""",
}

KIND_LABELS = {
    "blood": "Kan tahlili",
    "cardio": "Kardiyoloji kontrolü",
    "doctor_note": "Doktor notu / raporu",
    "other": "Diğer sağlık belgesi",
}
DEFAULT_INTERVALS = {"blood": 6, "cardio": 12}
SUPPORTED_MIME = {"application/pdf", "image/jpeg", "image/png", "image/webp", "image/gif"}
_MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_FILES = 10
# Claude isteği 32 MB ile sınırlı; base64 ~%33 büyüttüğü için tek analizdeki toplam ham boyut bunu aşmamalı.
MAX_TOTAL_BYTES = 22 * 1024 * 1024


def add_months(d: dt.date, months: int) -> dt.date:
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    return dt.date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def ensure_schedules(db: Session, profile: Profile) -> list[HealthSchedule]:
    rows = {s.kind: s for s in db.query(HealthSchedule).filter(HealthSchedule.profile_id == profile.id)}
    for kind, months in DEFAULT_INTERVALS.items():
        if kind not in rows:
            rows[kind] = HealthSchedule(profile_id=profile.id, kind=kind, interval_months=months)
            db.add(rows[kind])
    db.commit()
    return [rows[k] for k in DEFAULT_INTERVALS]


def schedule_status(db: Session, profile: Profile, today: dt.date | None = None) -> list[dict]:
    today = today or dt.date.today()
    out = []
    for s in ensure_schedules(db, profile):
        due = add_months(s.last_date, s.interval_months) if s.last_date else None
        if due is None:
            state = "unknown"
        elif due < today:
            state = "overdue"
        elif (due - today).days <= 21:
            state = "soon"
        else:
            state = "ok"
        out.append({
            "kind": s.kind, "label": KIND_LABELS[s.kind], "interval_months": s.interval_months,
            "last_date": s.last_date, "due_date": due, "state": state,
            "days": (due - today).days if due else None, "schedule": s,
        })
    return out


def due_reminders(db: Session, profile: Profile, now: dt.datetime | None = None) -> list[str]:
    """Zamanı gelen/geçen kontroller için hatırlatma metinleri; aynı kontrol haftada bir kez hatırlatılır."""
    now = now or utcnow()
    messages = []
    for item in schedule_status(db, profile, now.date()):
        s = item["schedule"]
        if item["state"] not in ("overdue", "soon", "unknown"):
            continue
        if s.last_reminded_at and now - s.last_reminded_at < dt.timedelta(days=7):
            continue
        lang = i18n.normalize(profile.language)
        label = i18n.t(lang, item["label"])
        if item["state"] == "unknown":
            text = "🩺 " + i18n.t(lang, "{label} için son tarih kayıtlı değil. Kulvar → Koç → Sağlık'tan son tarihi girersen her {n} ayda bir hatırlatırım.",
                                 label=label, n=item["interval_months"])
        elif item["state"] == "overdue":
            text = "🩺 " + i18n.t(lang, "{label} zamanı geçti ({days} gün). Son: {last}, her {n} ayda bir yapılmalı. Randevu almayı unutma; sonucu buraya gönderirsen yorumlarım.",
                                 label=label, days=-item["days"], last=f"{item['last_date']:%d.%m.%Y}", n=item["interval_months"])
        else:
            text = "🩺 " + i18n.t(lang, "{label} {days} gün sonra ({due}). Randevu almayı unutma.",
                                 label=label, days=item["days"], due=f"{item['due_date']:%d.%m.%Y}")
        messages.append(text)
        s.last_reminded_at = now
    db.commit()
    return messages


def store_file(profile_id: str, data: bytes, mime_type: str) -> str:
    if mime_type not in SUPPORTED_MIME:
        raise ValueError("Desteklenmeyen dosya türü. PDF veya fotoğraf (JPEG/PNG) gönder.")
    if len(data) > _MAX_FILE_BYTES:
        raise ValueError("Dosya çok büyük (maks. 20 MB).")
    ext = {"application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}[mime_type]
    folder = DATA_DIR / profile_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{uuid.uuid4().hex}.{ext}"
    path.write_bytes(data)
    return str(path.relative_to(DATA_DIR))


def record_files(record: HealthRecord) -> list[dict]:
    """Kaydın tüm belgeleri (ilk belge + aynı tahlilin ek belgeleri): [{"path", "mime", "name"}]."""
    files = [{"path": record.file_path, "mime": record.mime_type, "name": record.file_name}] if record.file_path else []
    try:
        files += json.loads(record.extra_files or "[]")
    except ValueError:
        pass
    return files


def add_file(record: HealthRecord, rel: str, mime: str, name: str | None = None) -> None:
    """Kayda belge ekler (ilk belge file_path'e, sonrakiler extra_files'a)."""
    if not record.file_path:
        record.file_path, record.mime_type, record.file_name = rel, mime, name
        return
    extra = record_files(record)[1:]
    extra.append({"path": rel, "mime": mime, "name": name})
    record.extra_files = json.dumps(extra, ensure_ascii=False)


def _safe_path(rel: str | None):
    if not rel:
        return None
    path = (DATA_DIR / rel).resolve()
    return path if path.is_relative_to(DATA_DIR.resolve()) and path.exists() else None


def file_path_at(record: HealthRecord, index: int = 0):
    files = record_files(record)
    return (_safe_path(files[index]["path"]), files[index]["mime"]) if 0 <= index < len(files) else (None, None)


def file_bytes(record: HealthRecord, index: int = 0) -> bytes | None:
    path, _ = file_path_at(record, index)
    return path.read_bytes() if path else None


def content_blocks(record: HealthRecord) -> list[dict]:
    """Claude'a gidecek belge blokları (PDF → document, fotoğraf → image); okunamayanlar atlanır."""
    blocks = []
    for f in record_files(record):
        path = _safe_path(f["path"])
        if path is None:
            continue
        b64 = base64.standard_b64encode(path.read_bytes()).decode()
        blocks.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}}
                      if f["mime"] == "application/pdf"
                      else {"type": "image", "source": {"type": "base64", "media_type": f["mime"], "data": b64}})
    return blocks


def total_bytes(record: HealthRecord) -> int:
    return sum(p.stat().st_size for f in record_files(record) if (p := _safe_path(f["path"])))


def delete_record(db: Session, record: HealthRecord) -> None:
    for f in record_files(record):
        path = _safe_path(f["path"])
        if path is not None:
            path.unlink()
    db.delete(record)
    db.commit()


def analyze_in_background(db: Session, record: HealthRecord) -> None:
    """Yorumu arka planda başlatır; durum record.analysis_status'ta (running → None ya da failed)."""
    from . import background

    record.analysis_status, record.analysis_error = "running", None
    db.commit()
    background.start(f"health-{record.id}", _analyze_job, record.id)


def _analyze_job(record_id: str) -> None:
    from .db import SessionLocal

    db = SessionLocal()
    try:
        record = db.get(HealthRecord, record_id)
        profile = db.get(Profile, record.profile_id) if record else None
        if record is None or profile is None:
            return
        try:
            outcome = analyze_record(db, profile, record)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Belge yorumu başarısız")
            outcome = {"ok": False, "message": str(exc)[:300]}
        db.refresh(record)
        record.analysis_status = None if outcome.get("ok") else "failed"
        record.analysis_error = None if outcome.get("ok") else outcome.get("message")
        db.commit()
        if outcome.get("ok") and record.kind == "blood" and record.labs_extracted_at is None:
            from . import labs  # yorumda değer bloğu yoksa (nadiren) ayrı çağrıyla çıkar

            labs.extract_quietly(record.id)
    finally:
        db.close()


def reset_interrupted(db: Session) -> None:
    """Servis yeniden başlarken yarıda kalan arka plan işlerini "başarısız" olarak işaretler (tekrar denenebilsin)."""
    for record in db.query(HealthRecord).filter(HealthRecord.analysis_status == "running").all():
        record.analysis_status, record.analysis_error = "failed", "Servis yeniden başlatıldığı için yorum yarıda kaldı; tekrar yorumlat."
    for profile in db.query(Profile).filter(Profile.meds_eval_status == "running").all():
        profile.meds_eval_status = "failed"
        profile.meds_eval_summary = "Servis yeniden başlatıldığı için değerlendirme yarıda kaldı; tekrar dene."
    db.commit()


def brief_lines(db: Session, profile: Profile) -> list[str]:
    """Koç özetine eklenen sağlık bölümü: kontrol tarihleri, doktor notları, son yorumların koç notu."""
    lines = []
    for item in schedule_status(db, profile):
        last = f"{item['last_date']:%d.%m.%Y}" if item["last_date"] else "bilinmiyor"
        due = f", sıradaki: {item['due_date']:%d.%m.%Y}" if item["due_date"] else ""
        lines.append(f"- {item['label']}: son {last}{due}")
    notes = (
        db.query(HealthRecord)
        .filter(HealthRecord.profile_id == profile.id, HealthRecord.kind == "doctor_note", HealthRecord.text_note.isnot(None))
        .order_by(HealthRecord.created_at.desc())
        .limit(3)
        .all()
    )
    for n in notes:
        when = f"{n.record_date:%d.%m.%Y}" if n.record_date else f"{n.created_at:%d.%m.%Y}"
        lines.append(f"- Doktor notu ({when}): {n.text_note}")
    for kind in ("blood", "cardio", "doctor_note"):
        rec = (
            db.query(HealthRecord)
            .filter(HealthRecord.profile_id == profile.id, HealthRecord.kind == kind, HealthRecord.coach_note.isnot(None))
            .order_by(HealthRecord.created_at.desc())
            .first()
        )
        if rec:
            when = f"{rec.record_date:%d.%m.%Y}" if rec.record_date else f"{rec.created_at:%d.%m.%Y}"
            lines.append(f"- {KIND_LABELS[kind]} yorumu ({when}): {rec.coach_note}")
    if not lines:
        return []
    return ["## Sağlık Kayıtları (doktor notları tıbbi kuralların önüne geçer)", *lines, ""]


_ANALYSIS_SYSTEM = """Sen {name} için bir sağlık verisi asistanısın; doktor değilsin ve teşhis koymazsın. Görevin, gönderilen {kind} belgesini okuyup kişinin önceki kayıtlarıyla karşılaştırmak, doktoruyla konuşmasına hazırlanmasına yardım etmek ve antrenman koçuna kısa bir not bırakmak.

Kişinin geçmişi ve koç talimatları:
{history}

Önceki kayıtların yorumları:
{previous}

{format_rule}

Acil değerlendirme gerektirebilecek bir bulgu görürsen bunu en başta açıkça yaz ve doktoruna/acil servise başvurmasını öner."""


def analyze_record(db: Session, profile: Profile, record: HealthRecord) -> dict:
    """Belgeyi Claude'a yorumlatır. {"ok": bool, "summary": str, "message": str} döner."""
    from . import coach_chat  # döngüsel import olmasın diye geç import

    lang = i18n.normalize(profile.language)
    if not coach_chat.api_key_configured():
        return {"ok": False, "message": i18n.t(lang, "Claude API anahtarı tanımlı değil.")}
    spent = coach_chat.month_cost(db)
    if spent >= settings.coach_monthly_budget_usd:
        return {"ok": False, "message": i18n.t(lang, "Bu ayki Claude bütçesi doldu (${spent}). Belge kaydedildi ama yorumlanmadı.", spent=f"{spent:.2f}")}
    blocks = content_blocks(record)
    if not blocks:
        return {"ok": False, "message": i18n.t(lang, "Belge dosyası bulunamadı.")}
    if total_bytes(record) > MAX_TOTAL_BYTES:
        return {"ok": False, "message": i18n.t(lang, "Belgeler toplamda çok büyük (maks. 22 MB). Daha az ya da sıkıştırılmış dosya yükle.")}

    previous = (
        db.query(HealthRecord)
        .filter(HealthRecord.profile_id == profile.id, HealthRecord.id != record.id, HealthRecord.ai_summary.isnot(None))
        .order_by(HealthRecord.created_at.desc())
        .limit(4)
        .all()
    )
    prev_text = "\n\n".join(
        f"[{KIND_LABELS.get(p.kind, p.kind)} — {p.record_date or p.created_at.date()}]\n{p.ai_summary}" for p in previous
    ) or "Önceki yorumlanmış kayıt yok."
    user_text = f"Ekteki {KIND_LABELS.get(record.kind, 'sağlık belgesi').lower()}ni yorumla."
    if len(blocks) > 1:
        user_text += (f" Aynı tahlile ait {len(blocks)} belge var (ör. hemogram ve biyokimya ayrı sayfalarda); "
                      "hepsini birlikte tek bir değerlendirme olarak yorumla, tabloyu birleştir.")
    if record.text_note:
        user_text += f"\nKişinin notu: {record.text_note}"
    if record.kind == "blood":  # seyir grafiği için sayısal değerler aynı çağrıda (belgeler ikinci kez gönderilmez)
        from . import labs

        user_text += labs.analysis_addendum()

    client = anthropic.Anthropic()
    try:
        with client.beta.messages.stream(
            model=settings.coach_model,
            max_tokens=32000,
            system=_ANALYSIS_SYSTEM.format(
                name=profile.name, kind=KIND_LABELS.get(record.kind, "sağlık"), format_rule=_FORMAT_RULES[lang],
                history=(profile.coach_instructions or "Kayıtlı geçmiş yok.").strip(), previous=prev_text,
            ),
            messages=[{"role": "user", "content": [*blocks, {"type": "text", "text": user_text}]}],
            output_config={"effort": settings.coach_effort},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            final = stream.get_final_message()
    except anthropic.APIStatusError as exc:
        logger.warning("Sağlık belgesi yorumlanamadı: %s", exc)
        return {"ok": False, "message": i18n.t(lang, "Claude API hatası ({code}). Belge kaydedildi, sonra tekrar deneyebilirsin.", code=exc.status_code)}
    except anthropic.APIConnectionError:
        return {"ok": False, "message": i18n.t(lang, "Claude API'ye bağlanılamadı. Belge kaydedildi, sonra tekrar deneyebilirsin.")}

    cost = coach_chat._cost(final.usage, final.model)
    thread = coach_chat.current_thread(db, profile)
    db.add(CoachMessage(  # bütçe ve kullanım tablosuna yansısın; sohbet geçmişine girmez
        thread_id=thread.id, profile_id=profile.id, seq=-1, role="health", content_json="[]",
        model=final.model, stop_reason=final.stop_reason, cost_usd=cost,
        input_tokens=final.usage.input_tokens, output_tokens=final.usage.output_tokens,
        cache_read_tokens=final.usage.cache_read_input_tokens, cache_write_tokens=final.usage.cache_creation_input_tokens,
    ))
    if final.stop_reason == "refusal":
        db.commit()
        return {"ok": False, "message": i18n.t(lang, "Claude bu belgeyi yorumlayamadı.")}

    text = "".join(b.text for b in final.content if b.type == "text").strip()
    values_block = None
    if record.kind == "blood":
        from . import labs

        values_block, text = labs.split_block(text)
    m = re.search(r"(?:TARİH|DATE):\s*(\d{4}-\d{2}-\d{2})", text)
    if m:
        try:
            record.record_date = dt.date.fromisoformat(m.group(1))
        except ValueError:
            pass
    note = re.search(r"(?:KOÇ İÇİN NOT|COACH NOTE):\s*(.+)", text, re.S)
    record.coach_note = note.group(1).strip()[:1000] if note else None
    record.ai_summary = text
    record.cost_usd = cost
    if values_block is not None:
        from . import labs

        labs.store(db, profile, record, labs.parse(values_block, record.record_date or record.created_at.date()))
        record.labs_extracted_at = utcnow()
    if record.kind in DEFAULT_INTERVALS and record.record_date:
        sched = next(s for s in ensure_schedules(db, profile) if s.kind == record.kind)
        if not sched.last_date or record.record_date > sched.last_date:
            sched.last_date = record.record_date
            sched.last_reminded_at = None
    db.commit()
    return {"ok": True, "summary": text, "cost_usd": cost}
