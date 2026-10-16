from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..clock import utcnow
from .. import background, health, i18n, meds, telegram_bot
from ..db import get_db
from ..models import HealthRecord, Medication, MedicationLog, Profile

router = APIRouter()

_BACK = "/coach-brief?tab=saglik"


def _parse_date(value: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(value.strip()) if value and value.strip() else None
    except ValueError:
        return None


@router.post("/health/{profile_id}/schedule")
def save_schedule(
    profile_id: str,
    blood_last: str = Form(""),
    blood_interval: int = Form(6),
    cardio_last: str = Form(""),
    cardio_interval: int = Form(12),
    db: Session = Depends(get_db),
):
    profile = db.get(Profile, profile_id)
    if profile is not None:
        for sched in health.ensure_schedules(db, profile):
            last, interval = (blood_last, blood_interval) if sched.kind == "blood" else (cardio_last, cardio_interval)
            new_last = _parse_date(last)
            if new_last != sched.last_date:
                sched.last_reminded_at = None
            sched.last_date = new_last
            sched.interval_months = max(1, min(int(interval), 36))
        db.commit()
    return RedirectResponse(_BACK, status_code=303)


@router.post("/health/{profile_id}/upload")
async def upload(
    request: Request,
    profile_id: str,
    files: list[UploadFile] = File(...),
    kind: str = Form("blood"),
    note: str = Form(""),
    db: Session = Depends(get_db),
):
    """Bir ya da birden fazla belge (aynı tahlilin sayfaları) tek kayıt olarak saklanır ve birlikte yorumlanır."""
    from urllib.parse import quote

    lang = request.state.lang
    profile = db.get(Profile, profile_id)
    files = [f for f in files if f.filename]
    if profile is None or not files:
        return RedirectResponse(_BACK, status_code=303)
    if len(files) > health.MAX_FILES:
        msg = i18n.t(lang, "Tek seferde en fazla {n} belge yükleyebilirsin.", n=health.MAX_FILES)
        return RedirectResponse(f"{_BACK}&health_error={quote(msg)}", status_code=303)
    record = HealthRecord(profile_id=profile.id, kind=kind if kind in health.KIND_LABELS else "other",
                          text_note=note.strip() or None, source="web")
    stored, total = [], 0
    try:
        for f in files:
            data = await f.read()
            total += len(data)
            if total > health.MAX_TOTAL_BYTES:
                raise ValueError("Belgeler toplamda çok büyük (maks. 22 MB). Daha az ya da sıkıştırılmış dosya yükle.")
            rel = health.store_file(profile.id, data, f.content_type or "")
            stored.append(rel)
            health.add_file(record, rel, f.content_type, (f.filename or "")[:120] or None)
    except ValueError as exc:
        for rel in stored:  # yarım kalan yüklemenin dosyalarını bırakma
            (health.DATA_DIR / rel).unlink(missing_ok=True)
        return RedirectResponse(f"{_BACK}&health_error={quote(i18n.t(lang, str(exc)))}", status_code=303)
    db.add(record)
    db.commit()
    health.analyze_in_background(db, record)  # sayfa hemen döner; yorum bitince kayıtta görünür
    return RedirectResponse(_BACK + "#kayitlar", status_code=303)


@router.post("/health/{profile_id}/doctor-note")
def doctor_note(profile_id: str, note: str = Form(...), record_date: str = Form(""), db: Session = Depends(get_db)):
    profile = db.get(Profile, profile_id)
    if profile is not None and note.strip():
        background.start(f"doctor-note-{profile.id}", _doctor_note_job, profile.id, note.strip(), _parse_date(record_date))
        return RedirectResponse(_BACK + "&notice=doctor_note", status_code=303)
    return RedirectResponse(_BACK, status_code=303)


def _doctor_note_job(profile_id: str, note: str, record_date: dt.date | None) -> None:
    from ..db import SessionLocal

    db = SessionLocal()
    try:
        profile = db.get(Profile, profile_id)
        if profile is not None:
            telegram_bot.record_doctor_note(db, profile, note, source="web", record_date=record_date)
    finally:
        db.close()


@router.post("/health/{profile_id}/meds")
def save_medication(
    profile_id: str,
    med_id: str = Form(""),
    name: str = Form(...),
    kind: str = Form("supplement"),
    status: str = Form("active"),
    dose: str = Form(""),
    times: str = Form(""),
    days: list[str] = Form([]),
    instructions: str = Form(""),
    reminders: str = Form(""),
    start_date: str = Form(""),
    end_date: str = Form(""),
    quantity: str = Form(""),
    db: Session = Depends(get_db),
):
    """Yeni ilaç/takviye ekler ya da med_id verilmişse günceller."""
    profile = db.get(Profile, profile_id)
    if profile is None or not name.strip():
        return RedirectResponse(_BACK + "#ilaclar", status_code=303)
    med = db.get(Medication, med_id) if med_id else None
    if med is None or med.profile_id != profile_id:
        med = Medication(profile_id=profile_id)
        db.add(med)
    med.name = name.strip()[:120]
    med.kind = kind if kind in meds.KINDS else "supplement"
    med.status = status if status in meds.STATUSES else "active"
    med.dose = dose.strip()[:80] or None
    med.times = ",".join(meds.parse_times(times)) or None
    med.days = meds.parse_days(days)
    med.instructions = instructions.strip()[:200] or None
    med.reminders = 1 if reminders else 0
    med.start_date, med.end_date = _parse_date(start_date), _parse_date(end_date)
    med.quantity = int(quantity) if quantity.strip().isdigit() and 0 < int(quantity) <= 10000 else None
    if med.quantity and med.end_date is None:  # tarayıcıda JS çalışmadıysa bitiş sunucuda hesaplanır
        med.start_date = med.start_date or dt.date.today()
        med.end_date = meds.end_date_for(med.quantity, med.dose, meds.times_of(med), meds.days_of(med), med.start_date)
    med.stopped_at = utcnow() if med.status == "stopped" else None
    db.commit()
    return RedirectResponse(_BACK + "#ilaclar", status_code=303)


@router.post("/health/{profile_id}/meds/{med_id}/{action}")
def medication_action(profile_id: str, med_id: str, action: str, db: Session = Depends(get_db)):
    """stop: bıraktım (geçmiş korunur) · resume: tekrar kullanıyorum · delete: tamamen sil."""
    med = db.get(Medication, med_id)
    if med is not None and med.profile_id == profile_id:
        if action == "stop":
            med.status, med.stopped_at = "stopped", utcnow()
        elif action == "resume":
            med.status, med.stopped_at = "active", None
        elif action == "delete":
            db.query(MedicationLog).filter(MedicationLog.medication_id == med.id).delete()
            db.delete(med)
        db.commit()
    return RedirectResponse(_BACK + "#ilaclar", status_code=303)


@router.post("/health/{profile_id}/meds-evaluate")
def evaluate_medications(request: Request, profile_id: str, db: Session = Depends(get_db)):
    profile = db.get(Profile, profile_id)
    if profile is not None:
        meds.evaluate_in_background(db, profile)
    return RedirectResponse(f"{_BACK}#ilaclar", status_code=303)


@router.post("/health/{profile_id}/record/{record_id}/analyze")
def reanalyze(profile_id: str, record_id: str, db: Session = Depends(get_db)):
    profile = db.get(Profile, profile_id)
    record = db.get(HealthRecord, record_id)
    if profile is not None and record is not None and record.profile_id == profile.id and record.file_path:
        health.analyze_in_background(db, record)
    return RedirectResponse(_BACK + "#kayitlar", status_code=303)


@router.post("/health/{profile_id}/record/{record_id}/delete")
def delete(profile_id: str, record_id: str, db: Session = Depends(get_db)):
    record = db.get(HealthRecord, record_id)
    if record is not None and record.profile_id == profile_id:
        health.delete_record(db, record)
    return RedirectResponse(_BACK, status_code=303)


@router.get("/health/file/{record_id}")
def view_file(record_id: str, i: int = 0, db: Session = Depends(get_db)):
    record = db.get(HealthRecord, record_id)
    path, mime = health.file_path_at(record, i) if record is not None else (None, None)
    if path is None:
        return RedirectResponse(_BACK, status_code=303)
    return FileResponse(path, media_type=mime)


@router.post("/settings/{profile_id}/telegram/code")
def telegram_code(profile_id: str, db: Session = Depends(get_db)):
    profile = db.get(Profile, profile_id)
    if profile is not None:
        telegram_bot.new_link_code(db, profile)
    return RedirectResponse(f"/settings?profile={profile_id}#telegram", status_code=303)


@router.post("/settings/{profile_id}/telegram/bot")
def telegram_bot_save(request: Request, profile_id: str, token: str = Form(""), db: Session = Depends(get_db)):
    from urllib.parse import quote

    profile = db.get(Profile, profile_id)
    message = (telegram_bot.save_profile_bot(db, profile, token, request.state.lang) if profile is not None
               else i18n.t(request.state.lang, "Profil bulunamadı."))
    return RedirectResponse(f"/settings?profile={profile_id}&tg_msg={quote(message)}#telegram", status_code=303)


@router.post("/settings/{profile_id}/telegram/bot/delete")
def telegram_bot_delete(profile_id: str, db: Session = Depends(get_db)):
    profile = db.get(Profile, profile_id)
    if profile is not None:
        telegram_bot.remove_profile_bot(db, profile)
    return RedirectResponse(f"/settings?profile={profile_id}#telegram", status_code=303)


@router.post("/settings/{profile_id}/telegram/unlink")
def telegram_unlink(profile_id: str, db: Session = Depends(get_db)):
    profile = db.get(Profile, profile_id)
    if profile is not None:
        profile.telegram_chat_id = None
        profile.telegram_link_code = None
        db.commit()
    return RedirectResponse(f"/settings?profile={profile_id}#telegram", status_code=303)
