"""Kuvvet rutinleri: Kulvar'da oluşturma ve düzenleme.

Hevy'den gelen bir rutin düzenlenince değişiklik Hevy'ye de yazılır (günlük yenileme geri almasın).
Rutine Hevy'de karşılığı olmayan bir hareket (free-exercise-db) eklenirse rutin Kulvar'a taşınır:
kimliği "kulvar-…" olur, Hevy'deki eski kopyası yenilemede geri gelmez. Her kayıttan sonra bu rutini
kullanan, Garmin'e gönderilmiş gelecek kuvvet planları yeni hareketlerle güncellenir.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import uuid

from sqlalchemy.orm import Session

from . import exercise_catalog, workout_library
from .clock import utcnow
from .crypto import decrypt_payload
from .models import Credential, PlannedWorkout, Profile, StrengthRoutine

logger = logging.getLogger("kulvar.routines")

NATIVE_PREFIX = workout_library.NATIVE_PREFIX


def is_native(routine: StrengthRoutine) -> bool:
    return routine.hevy_id.startswith(NATIVE_PREFIX)


def exercises(routine: StrengthRoutine) -> list[dict]:
    return json.loads(routine.exercises_json or "[]")


def new_native(db: Session, profile: Profile, title: str, exercise_list: list[dict] | None = None) -> StrengthRoutine:
    routine = StrengthRoutine(profile_id=profile.id, hevy_id=NATIVE_PREFIX + uuid.uuid4().hex[:12], title=title[:120],
                              exercises_json=json.dumps(exercise_list or [], ensure_ascii=False), updated_at=utcnow())
    db.add(routine)
    db.flush()
    return routine


def hevy_compatible(exercise_list: list[dict]) -> bool:
    """Tüm hareketlerin Hevy'de karşılığı var mı (free-exercise-db'den eklenenlerin yok)."""
    return all(e.get("template_id") and not e["template_id"].startswith(exercise_catalog.FEDB_PREFIX) for e in exercise_list)


def save(db: Session, profile: Profile, routine: StrengthRoutine, title: str, exercise_list: list[dict]) -> str:
    """Rutini kaydeder. Sonuç: "native" | "hevy" (Hevy'ye de yazıldı) | "detached" (Kulvar'a taşındı)."""
    from . import routine_builder

    routine.title = (title or routine.title or "Rutin").strip()[:120]
    routine.exercises_json = json.dumps(exercise_list, ensure_ascii=False)
    routine.updated_at = utcnow()
    outcome = "native"
    if not is_native(routine):
        key = routine_builder.hevy_key(db, profile.id)
        if key and hevy_compatible(exercise_list):
            body = routine_builder._hevy_body({"title": routine.title, "notes": routine.notes, "exercises": exercise_list})
            routine_builder._hevy_send("PUT", key, f"/routines/{routine.hevy_id}", body)
            outcome = "hevy"
        else:
            routine.replaces_hevy_id, routine.hevy_id = routine.hevy_id, NATIVE_PREFIX + uuid.uuid4().hex[:12]
            outcome = "detached"
    db.commit()
    update_garmin_plans(db, profile, {workout_library._routine_key(routine.title)})
    return outcome


def delete(db: Session, routine: StrengthRoutine) -> bool:
    """Yalnız Kulvar rutinleri silinir (Hevy'nin açık API'sinde rutin silme yok)."""
    if not is_native(routine):
        return False
    db.delete(routine)
    db.commit()
    return True


def update_garmin_plans(db: Session, profile: Profile, keys: set[str]) -> int:
    """NOT'u bu rutin adlarından biriyle başlayan, Garmin'e gönderilmiş gelecek kuvvet planlarını günceller."""
    from .integrations import garmin as garmin_integration

    credential = db.query(Credential).filter(Credential.profile_id == profile.id, Credential.source == "garmin").first()
    if credential is None:
        return 0
    plans = (db.query(PlannedWorkout).filter(PlannedWorkout.profile_id == profile.id, PlannedWorkout.workout_type == "strength",
                                             PlannedWorkout.date >= dt.date.today(), PlannedWorkout.status.is_(None),
                                             PlannedWorkout.garmin_workout_id.isnot(None)).all())
    payload, updated = None, 0
    for w in plans:
        routine = workout_library.routine_for_plan(db, w)
        if routine is None or workout_library._routine_key(routine.title) not in keys:
            continue
        payload = payload or decrypt_payload(credential.encrypted_payload)
        w.garmin_error = garmin_integration.update_strength_routine(payload, w)
        updated += 1
    db.commit()
    return updated


# --------------------------------------------------------------------------- düzenleyici formu

def parse_form(old: list[dict], form: dict) -> list[dict]:
    """Düzenleyici formundaki değerleri hareket listesine yazar (ex_<i>_rest, ex_<i>_notes, set_<i>_<j>_reps/kg/sec)."""
    def num(key, cast):
        raw = (form.get(key) or "").strip().replace(",", ".")
        try:
            value = cast(float(raw)) if raw else None
        except ValueError:
            return None
        return value if value is None or value >= 0 else None

    out = []
    for i, e in enumerate(old):
        e = dict(e)
        if f"ex_{i}_rest" in form:
            e["rest_seconds"] = num(f"ex_{i}_rest", int)
        if f"ex_{i}_notes" in form:
            e["notes"] = (form.get(f"ex_{i}_notes") or "").strip()[:300] or None
        sets = []
        for j, s in enumerate(e.get("sets") or []):
            s = dict(s)
            if f"set_{i}_{j}_sec" in form:
                s.update(duration_seconds=num(f"set_{i}_{j}_sec", int), reps=None, weight_kg=None)
            else:
                if f"set_{i}_{j}_reps" in form:
                    s["reps"] = num(f"set_{i}_{j}_reps", int)
                if f"set_{i}_{j}_kg" in form:
                    s["weight_kg"] = num(f"set_{i}_{j}_kg", float) or None
            sets.append(s)
        e["sets"] = sets
        out.append(e)
    return out


def apply_action(db: Session, exercise_list: list[dict], action: str) -> list[dict]:
    """add_ex:<katalog kimliği> · del_ex:<i> · up:<i> · down:<i> · add_set:<i> · del_set:<i>:<j> · mode:<i>"""
    kind, _, arg = action.partition(":")
    items = list(exercise_list)
    if kind == "add_ex":
        media = exercise_catalog.ensure(db, arg)
        if media is not None:
            timed = exercise_catalog.is_timed(media.title)
            one = {"type": "normal", "duration_seconds": 30} if timed else {"type": "normal", "reps": 10, "weight_kg": None}
            items.append({"template_id": media.template_id, "title": media.title, "rest_seconds": 60, "notes": None,
                          "sets": [dict(one) for _ in range(3)]})
        return items
    parts = arg.split(":")
    if not parts or not parts[0].isdigit() or int(parts[0]) >= len(items):
        return items
    i = int(parts[0])
    if kind == "del_ex":
        items.pop(i)
    elif kind == "up" and i > 0:
        items[i - 1], items[i] = items[i], items[i - 1]
    elif kind == "down" and i < len(items) - 1:
        items[i + 1], items[i] = items[i], items[i + 1]
    elif kind == "add_set":
        sets = items[i].get("sets") or [{"type": "normal", "reps": 10}]
        items[i] = {**items[i], "sets": sets + [dict(sets[-1])]}
    elif kind == "del_set" and len(parts) == 2 and parts[1].isdigit():
        sets = list(items[i].get("sets") or [])
        if len(sets) > 1 and int(parts[1]) < len(sets):
            sets.pop(int(parts[1]))
        items[i] = {**items[i], "sets": sets}
    elif kind == "mode":  # tekrar × kg ↔ süre
        timed = any(s.get("duration_seconds") for s in items[i].get("sets") or [])
        new = {"type": "normal", "reps": 10, "weight_kg": None} if timed else {"type": "normal", "duration_seconds": 30}
        items[i] = {**items[i], "sets": [dict(new) for _ in items[i].get("sets") or [None]]}
    return items
