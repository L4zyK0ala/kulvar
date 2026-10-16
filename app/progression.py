"""Kademeli yüklenme: rutindeki bir hareketin hedefi son iki antrenmanda tamamlandıysa bir sonraki adımı önerir.

Kurallar (çift aşamalı ilerleme):
  - Ağırlıklı hareket: tekrar 15'in altındaysa +2 tekrar; 15'e ulaştıysa ağırlık +2,5 kg ve tekrar -4.
  - Vücut ağırlığı: +2 tekrar (25'e kadar).
  - Süreli hareket: +10 sn (120 sn'ye kadar).
Öneri onaylanınca rutin güncellenir (routines.save: Hevy'ye bağlıysa Hevy'ye de yazılır, saate gitmiş
planlar da güncellenir). Aynı hareket için bekleyen bir öneri varsa yenisi oluşmaz.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re

from sqlalchemy.orm import Session

from . import i18n, workout_library
from .clock import utcnow
from .models import ExerciseMedia, Profile, ProgressionSuggestion, StrengthRoutine, StrengthWorkout

logger = logging.getLogger("kulvar.progression")

WEIGHT_STEP = 2.5
REPS_STEP, REPS_CAP, BODYWEIGHT_CAP = 2, 15, 25
TIME_STEP, TIME_CAP = 10, 120
WINDOW_DAYS = 21  # iki başarılı seans bu süre içinde olmalı (aylar arayla yapılanlar ilerleme sayılmaz)


INVENTORY_TYPES = ("dumbbell", "kettlebell")
TOP_WEIGHT_REPS_CAP = 20  # elindeki en ağır ağırlıktaysan tekrar buraya kadar artar


def inventory(profile: Profile) -> dict[str, list[float]]:
    """Evdeki ağırlıklar: {"dumbbell": [5.0, 10.0], "kettlebell": [12.0]} (boş liste = sınır yok)."""
    try:
        data = json.loads(profile.home_equipment or "{}")
    except ValueError:
        return {}
    return {k: sorted({float(w) for w in data.get(k) or [] if float(w) > 0}) for k in INVENTORY_TYPES if data.get(k)}


def parse_weights(raw: str) -> list[float]:
    """ "5, 10 kg; 7,5" → [5.0, 7.5, 10.0]"""
    out = set()
    for m in re.finditer(r"\d+(?:[.,]\d+)?", raw or ""):
        value = float(m.group(0).replace(",", "."))
        if 0 < value <= 200:
            out.add(value)
    return sorted(out)


def inventory_text(inv: dict[str, list[float]], lang: str) -> str:
    """Rutin oluşturucu formu için: "dambıl 5, 10 kg · kettlebell 12 kg" """
    names = {"dumbbell": i18n.t(lang, "Dambıl"), "kettlebell": "Kettlebell"}
    return " · ".join(f"{names[k]} {', '.join(f'{w:g}' for w in v)} kg" for k, v in inv.items())


def _work(sets: list[dict]) -> list[dict]:
    return [s for s in sets or [] if s.get("type") != "warmup"]


def _key(e: dict) -> str:
    return e.get("template_id") or e.get("exercise_template_id") or e.get("title") or ""


def routine_of(db: Session, workout: StrengthWorkout) -> StrengthRoutine | None:
    if workout.routine_id:
        r = db.get(StrengthRoutine, workout.routine_id)
        if r is not None:
            return r
    key = workout_library._routine_key(workout.title)
    return next((r for r in workout_library.routines(db, workout.profile_id) if workout_library._routine_key(r.title) == key), None)


def _done_sets(workout: StrengthWorkout, key: str, title: str) -> list[dict]:
    from .strength_log import exercises_of

    for e in exercises_of(workout):
        if (e.get("exercise_template_id") or e["title"]) == key or e["title"] == title:
            return _work(e["sets"])
    return []


def met(planned: list[dict], done: list[dict]) -> bool:
    """Planlanan her set yapıldı ve hedefe ulaşıldı mı."""
    if not planned or len(done) < len(planned):
        return False
    for p, d in zip(planned, done):
        if p.get("duration_seconds") and not p.get("reps"):
            if (d.get("duration_seconds") or 0) < p["duration_seconds"]:
                return False
        else:
            if (d.get("reps") or 0) < (p.get("reps") or 0):
                return False
            if p.get("weight_kg") and (d.get("weight_kg") or 0) < p["weight_kg"]:
                return False
    return True


def next_step(planned: list[dict], weights: list[float] | None = None) -> list[dict] | None:
    """Bir sonraki adımın setleri; ilerleme sınırına gelindiyse None.
    weights: hareketin ekipmanı için evdeki ağırlıklar (yoksa ağırlık serbestçe +2,5 kg artar)."""
    out = []
    for p in planned:
        s = dict(p)
        if p.get("duration_seconds") and not p.get("reps"):
            if p["duration_seconds"] >= TIME_CAP:
                return None
            s["duration_seconds"] = min(TIME_CAP, int(p["duration_seconds"]) + TIME_STEP)
        elif p.get("weight_kg"):
            reps, weight = p.get("reps") or 0, float(p["weight_kg"])
            heavier = [w for w in weights or [] if w > weight]
            if reps < REPS_CAP:
                s["reps"] = reps + REPS_STEP
            elif weights and not heavier:  # en ağır ağırlıktasın: tekrar artmaya devam
                if reps >= TOP_WEIGHT_REPS_CAP:
                    return None
                s["reps"] = min(TOP_WEIGHT_REPS_CAP, reps + REPS_STEP)
            elif weights:  # bir sonraki mevcut ağırlık; tekrar, ağırlık artışı oranında azalır
                s["weight_kg"] = heavier[0]
                s["reps"] = max(6, min(reps - 2, round(reps * weight / heavier[0])))
            else:
                s["weight_kg"] = weight + WEIGHT_STEP
                s["reps"] = max(6, reps - 4)
        else:
            if (p.get("reps") or 0) >= BODYWEIGHT_CAP:
                return None
            s["reps"] = (p.get("reps") or 0) + REPS_STEP
        out.append(s)
    return out


def check(db: Session, profile: Profile, workout: StrengthWorkout) -> list[ProgressionSuggestion]:
    """Onaylanan bir antrenmandan sonra: rutindeki hareketler için öneri oluşturur ve Telegram'a gönderir."""
    routine = routine_of(db, workout)
    if routine is None:
        return []
    recent = (db.query(StrengthWorkout).filter(StrengthWorkout.profile_id == profile.id, StrengthWorkout.status == "confirmed",
                                               StrengthWorkout.start_time <= workout.start_time,
                                               StrengthWorkout.start_time >= workout.start_time - dt.timedelta(days=WINDOW_DAYS))
              .order_by(StrengthWorkout.start_time.desc()).limit(12).all())
    pending = {(s.routine_id, s.exercise_key) for s in db.query(ProgressionSuggestion).filter(
        ProgressionSuggestion.profile_id == profile.id, ProgressionSuggestion.status == "pending")}
    created, inv = [], inventory(profile)
    for e in json.loads(routine.exercises_json or "[]"):
        title, key, planned = e.get("title") or "?", _key(e), _work(e.get("sets"))
        if not planned or workout_library._is_cardio_or_warmup(title) or (routine.id, key) in pending:
            continue
        history = [d for d in (_done_sets(w, key, title) for w in recent) if d][:2]
        if len(history) < 2 or not all(met(planned, d) for d in history):
            continue
        media = db.get(ExerciseMedia, e.get("template_id")) if e.get("template_id") else None
        new = next_step(planned, inv.get(media.equipment) if media and media.equipment else None)
        if new is None:
            continue
        s = ProgressionSuggestion(profile_id=profile.id, routine_id=routine.id, exercise_key=key, title=title,
                                  old_json=json.dumps(planned), new_json=json.dumps(new))
        db.add(s)
        created.append(s)
    db.commit()
    for s in created:
        _send(db, profile, s)
    return created


def describe(s: ProgressionSuggestion, lang: str) -> str:
    old = workout_library._sets_summary(json.loads(s.old_json), lang)
    new = workout_library._sets_summary(json.loads(s.new_json), lang)
    return f"{s.title}: {old} → {new}"


def _send(db: Session, profile: Profile, s: ProgressionSuggestion) -> None:
    from . import telegram_bot as tb

    lang = i18n.normalize(profile.language)
    text = "📈 " + tb.T(profile, "Son iki antrenmanda hedefi tamamladın. Bir sonraki adım:") + f"\n**{describe(s, lang)}**"
    s.telegram_message_id = tb.send_to_profile(profile, text, [[
        {"text": tb.T(profile, "✅ Rutine uygula"), "callback_data": f"pg:{s.id}:ok"},
        {"text": tb.T(profile, "❌ Şimdilik kalsın"), "callback_data": f"pg:{s.id}:no"},
    ]])
    db.commit()


def decide(db: Session, profile: Profile, s: ProgressionSuggestion, apply: bool) -> str:
    """"applied" | "rejected" | "stale" (rutin değişmiş) | "done" (zaten karar verilmiş)."""
    from . import routines

    if s.profile_id != profile.id or s.status != "pending":
        return "done"
    s.decided_at = utcnow()
    if not apply:
        s.status = "rejected"
        db.commit()
        return "rejected"
    routine = db.get(StrengthRoutine, s.routine_id)
    items = json.loads(routine.exercises_json or "[]") if routine else []
    target = next((e for e in items if _key(e) == s.exercise_key), None)
    if target is None or _work(target.get("sets")) != json.loads(s.old_json):
        s.status = "stale"
        db.commit()
        return "stale"
    warm = [x for x in target.get("sets") or [] if x.get("type") == "warmup"]
    target["sets"] = warm + json.loads(s.new_json)
    s.status = "applied"
    try:
        routines.save(db, profile, routine, routine.title, items)
    except Exception:  # noqa: BLE001 - Hevy'ye yazılamazsa Kulvar'daki rutin yine güncellenir
        logger.exception("Önerilen ilerleme Hevy'ye yazılamadı")
        routine.exercises_json = json.dumps(items, ensure_ascii=False)
        db.commit()
    return "applied"


def pending(db: Session, profile: Profile) -> list[ProgressionSuggestion]:
    return (db.query(ProgressionSuggestion).filter(ProgressionSuggestion.profile_id == profile.id,
                                                   ProgressionSuggestion.status == "pending")
            .order_by(ProgressionSuggestion.created_at.desc()).all())
