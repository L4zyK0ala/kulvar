"""Telefonda canlı kuvvet antrenmanı (Hevy'nin antrenman ekranının karşılığı).

Rutinden başlatılır: setler plandan dolu gelir, yanında bir önceki antrenmanın değeri görünür. Her ✓ set
hemen kaydedilir ve dinlenme sayacı başlar; süre bitince telefonda ses, titreşim (Android) ve izin
verildiyse bildirim. Telegram'a mesaj gönderilmez (set başına mesaj fazla geldi).
"Bitir" ile yapılmayan setler atılır ve kayıt onaylanır (strength_log.confirm: plan yapıldı, Garmin/Strava).
Aynı anda saat de çalıştıysa Garmin senkronu saat kaydını bu antrenmana bağlar (ayrıca onay sorulmaz).
"""
from __future__ import annotations

import datetime as dt
import json
import logging

from sqlalchemy.orm import Session

from . import i18n, workout_library
from .clock import utcnow
from .models import ExerciseMedia, PlannedWorkout, Profile, StrengthRoutine, StrengthSet, StrengthWorkout

logger = logging.getLogger("kulvar.live_workout")


def active(db: Session, profile_id: str) -> StrengthWorkout | None:
    return (db.query(StrengthWorkout).filter(StrengthWorkout.profile_id == profile_id, StrengthWorkout.status == "live")
            .order_by(StrengthWorkout.start_time.desc()).first())


def todays_routine(db: Session, profile: Profile) -> StrengthRoutine | None:
    """Bugün planlanan kuvvet gününün rutini (varsa)."""
    plan = (db.query(PlannedWorkout).filter(PlannedWorkout.profile_id == profile.id, PlannedWorkout.workout_type == "strength",
                                            PlannedWorkout.date == dt.date.today()).first())
    return workout_library.routine_for_plan(db, plan) if plan is not None else None


def start(db: Session, profile: Profile, routine: StrengthRoutine) -> StrengthWorkout:
    existing = active(db, profile.id)
    if existing is not None:
        return existing
    plan = (db.query(PlannedWorkout).filter(PlannedWorkout.profile_id == profile.id, PlannedWorkout.workout_type == "strength",
                                            PlannedWorkout.date == dt.date.today(), PlannedWorkout.status.is_(None)).first())
    if plan is not None and workout_library.routine_for_plan(db, plan) is not routine:
        plan = None
    w = StrengthWorkout(profile_id=profile.id, start_time=dt.datetime.now().replace(microsecond=0), title=routine.title,
                        routine_id=routine.id, planned_workout_id=plan.id if plan else None, status="live", source="kulvar")
    for pos, e in enumerate(json.loads(routine.exercises_json or "[]")):
        warm = workout_library._is_cardio_or_warmup(e.get("title") or "")
        for i, s in enumerate(e.get("sets") or []):
            timed = bool(s.get("duration_seconds")) and not s.get("reps")
            w.sets.append(StrengthSet(
                position=pos, set_index=i, title=e.get("title") or "?", template_id=e.get("template_id"),
                set_type="warmup" if warm or s.get("type") == "warmup" else "normal",
                reps=None if timed else s.get("reps"), weight_kg=s.get("weight_kg"), duration_s=s.get("duration_seconds") if timed or warm else None,
                planned_reps=s.get("reps"), planned_weight_kg=s.get("weight_kg"), planned_duration_s=s.get("duration_seconds"),
                rest_s=e.get("rest_seconds")))
    db.add(w)
    db.commit()
    return w


def _previous(db: Session, workout: StrengthWorkout) -> dict:
    """{hareket anahtarı: [önceki antrenmandaki setler]} (bu antrenman hariç, en yeni onaylı kayıt)."""
    from .strength_log import exercises_of

    out: dict[str, list] = {}
    rows = (db.query(StrengthWorkout).filter(StrengthWorkout.profile_id == workout.profile_id, StrengthWorkout.status == "confirmed",
                                             StrengthWorkout.id != workout.id)
            .order_by(StrengthWorkout.start_time.desc()).limit(20).all())
    for w in rows:
        for e in exercises_of(w):
            for key in {e.get("exercise_template_id"), e["title"]} - {None}:
                out.setdefault(key, [s for s in e["sets"] if s.get("type") != "warmup"])
    return out


def _set_label(s: dict, lang: str) -> str:
    if s.get("duration_seconds") and not s.get("reps"):
        return f"{int(s['duration_seconds'])} {i18n.t(lang, 'sn')}"
    text = str(s.get("reps") if s.get("reps") is not None else "?")
    return text + (f" × {s['weight_kg']:g} kg" if s.get("weight_kg") else "")


def state(db: Session, workout: StrengthWorkout, lang: str) -> dict:
    """İstemcideki ekran için JSON: hareketler, görseller, setler ve önceki değerler."""
    routine = db.get(StrengthRoutine, workout.routine_id) if workout.routine_id else None
    notes = {}
    if routine is not None:
        for e in json.loads(routine.exercises_json or "[]"):
            notes[(e.get("title") or "")] = e.get("notes")
    prev = _previous(db, workout)
    groups: dict[int, dict] = {}
    for s in workout.sets:
        g = groups.get(s.position)
        if g is None:
            media = db.get(ExerciseMedia, s.template_id) if s.template_id else None
            history = prev.get(s.template_id) or prev.get(s.title) or []
            g = groups[s.position] = {
                "position": s.position, "title": s.title, "warmup": s.set_type == "warmup",
                "frames": json.loads(media.frames) if media and media.frames else [],
                "instructions": json.loads(media.instructions) if media and media.instructions else [],
                "notes": notes.get(s.title), "rest": s.rest_s or 60, "timed": False, "sets": [],
                "_prev": history,
            }
        timed = s.duration_s is not None and s.reps is None
        g["timed"] = g["timed"] or timed
        n = len(g["sets"])
        g["sets"].append({"id": s.id, "reps": s.reps, "kg": s.weight_kg, "sec": s.duration_s, "done": s.done_at is not None,
                          "prev": _set_label(g["_prev"][n], lang) if n < len(g["_prev"]) else None})
    out = [groups[k] for k in sorted(groups)]
    for g in out:
        g.pop("_prev", None)
    return {"id": workout.id, "title": workout.title, "started": workout.start_time.isoformat(), "exercises": out}


def update_set(workout: StrengthWorkout, set_id: str, data: dict) -> bool:
    s = next((x for x in workout.sets if x.id == set_id), None)
    if s is None:
        return False

    def num(value, cast):
        try:
            v = cast(float(value))
        except (TypeError, ValueError):
            return None
        return v if v >= 0 else None

    if "reps" in data:
        s.reps = num(data["reps"], int)
    if "kg" in data:
        s.weight_kg = num(data["kg"], float) or None
    if "sec" in data:
        s.duration_s = num(data["sec"], float)
    if "done" in data:
        s.done_at = utcnow() if data["done"] else None
    return True


def add_set(workout: StrengthWorkout, position: int) -> StrengthSet | None:
    same = [s for s in workout.sets if s.position == position]
    if not same:
        return None
    last = same[-1]
    new = StrengthSet(position=position, set_index=last.set_index + 1, title=last.title, template_id=last.template_id,
                      set_type=last.set_type, reps=last.reps, weight_kg=last.weight_kg, duration_s=last.duration_s,
                      planned_reps=last.planned_reps, planned_weight_kg=last.planned_weight_kg,
                      planned_duration_s=last.planned_duration_s, rest_s=last.rest_s)
    workout.sets.append(new)
    return new


def finish(db: Session, workout: StrengthWorkout) -> bool:
    """Yapılmayan setleri atar ve kaydı onaylar. Hiç set yapılmadıysa False (kayıt silinmez)."""
    from . import strength_log

    done = [s for s in workout.sets if s.done_at is not None]
    if not done:
        return False
    for s in [s for s in workout.sets if s.done_at is None]:
        workout.sets.remove(s)
    utc_offset = dt.datetime.now() - utcnow()  # start_time yerel, done_at UTC saklanıyor
    workout.duration_s = max(60.0, (max(s.done_at for s in done) - (workout.start_time - utc_offset)).total_seconds())
    strength_log.confirm(db, workout)
    return True
