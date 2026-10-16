"""Antrenman sonrası "nasıl hissettirdi?": algılanan zorluk (RPE 1–10) ve kısa not.

Panel'de son antrenman için, aktivite sayfasında ve kuvvet antrenmanı ekranında tek dokunuşla girilir
(Telegram mesajı yok). Koç her antrenmanın puanını ve notunu görür; ayrıca puan × dakika ile haftalık
"algılanan yük" (session-RPE) hesaplanır: haftadan haftaya ani artış sakatlık riskinin erken işaretidir.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from .clock import utcnow
from .models import Activity, Profile, StrengthWorkout, WorkoutFeedback

SAME_SESSION = dt.timedelta(minutes=20)
ASK_WINDOW = dt.timedelta(hours=48)  # Panel bu süre içindeki son antrenmanı sorar
LOAD_JUMP = 0.30  # haftalık yük %30'dan fazla artarsa koça uyarı


def refs_for_activity(db: Session, activity: Activity) -> list[str]:
    """Aynı antrenmanın tüm kopyaları (Garmin/Strava/Hevy) ve bağlı Kulvar kuvvet kaydı; ilki birincil."""
    siblings = (db.query(Activity).filter(Activity.profile_id == activity.profile_id,
                                          Activity.start_time.between(activity.start_time - SAME_SESSION, activity.start_time + SAME_SESSION)).all())
    refs = [f"act:{activity.id}"] + [f"act:{a.id}" for a in siblings if a.id != activity.id]
    ext = {a.external_id for a in siblings} | {activity.external_id}
    native = db.query(StrengthWorkout).filter(StrengthWorkout.profile_id == activity.profile_id,
                                              (StrengthWorkout.garmin_activity_id.in_(ext)) | (StrengthWorkout.hevy_id.in_(ext))).first()
    if native is not None:
        refs.insert(0, f"sw:{native.id}")
    return refs


def refs_for_workout(db: Session, workout: StrengthWorkout) -> list[str]:
    refs = [f"sw:{workout.id}"]
    ext = [x for x in (workout.garmin_activity_id, workout.hevy_id) if x]
    if ext:
        refs += [f"act:{a.id}" for a in db.query(Activity).filter(Activity.profile_id == workout.profile_id, Activity.external_id.in_(ext))]
    return refs


def find(db: Session, profile_id: str, refs: list[str]) -> WorkoutFeedback | None:
    if not refs:
        return None
    return db.query(WorkoutFeedback).filter(WorkoutFeedback.profile_id == profile_id, WorkoutFeedback.ref.in_(refs)).first()


def save(db: Session, profile: Profile, refs: list[str], date: dt.date, minutes: float | None, title: str | None,
         rpe: int | None, note: str | None) -> WorkoutFeedback:
    fb = find(db, profile.id, refs)
    if fb is None:
        fb = WorkoutFeedback(profile_id=profile.id, ref=refs[0], date=date)
        db.add(fb)
    if rpe is not None:
        fb.rpe = max(1, min(10, int(rpe)))
    if note is not None:
        fb.note = note.strip()[:300] or None
    fb.minutes, fb.title, fb.updated_at = minutes, (title or fb.title), utcnow()
    db.commit()
    return fb


def week_load(db: Session, profile_id: str, start: dt.date) -> float:
    """Haftanın (start = Pazartesi) algılanan yükü: Σ RPE × dakika."""
    rows = db.query(WorkoutFeedback).filter(WorkoutFeedback.profile_id == profile_id, WorkoutFeedback.date >= start,
                                            WorkoutFeedback.date < start + dt.timedelta(days=7)).all()
    return sum((f.rpe or 0) * (f.minutes or 0) for f in rows)


def brief_lines(db: Session, profile: Profile, today: dt.date | None = None) -> list[str]:
    """Koç verisi: son 14 günün puanları/notları ve haftalık algılanan yük."""
    today = today or dt.date.today()
    rows = (db.query(WorkoutFeedback).filter(WorkoutFeedback.profile_id == profile.id,
                                             WorkoutFeedback.date >= today - dt.timedelta(days=14))
            .order_by(WorkoutFeedback.date.desc()).all())
    if not rows:
        return []
    lines = ["## Antrenman Sonrası His (RPE 1–10, kullanıcının girdiği)"]
    for f in rows:
        text = f"- {f.date:%d.%m} {f.title or 'antrenman'}"
        if f.rpe:
            text += f" · RPE {f.rpe}"
        if f.minutes:
            text += f" · {f.minutes:.0f} dk"
        if f.note:
            text += f" · not: {f.note}"
        lines.append(text)
    monday = today - dt.timedelta(days=today.weekday())
    this, last = week_load(db, profile.id, monday), week_load(db, profile.id, monday - dt.timedelta(days=7))
    if this or last:
        line = f"- Algılanan yük (RPE × dk): bu hafta {this:.0f}, geçen hafta {last:.0f}"
        if last and this > last * (1 + LOAD_JUMP):
            line += f" — %{(this / last - 1) * 100:.0f} artış, yükü dengele"
        lines.append(line)
    lines.append("")
    return lines


def pending_activity(db: Session, profile: Profile, activity: Activity | None) -> bool:
    """Panel sorusu: son antrenman 48 saat içindeyse ve puanlanmadıysa."""
    if activity is None or dt.datetime.now() - activity.start_time > ASK_WINDOW:
        return False
    return find(db, profile.id, refs_for_activity(db, activity)) is None
