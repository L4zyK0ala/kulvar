"""Garmin kuvvet aktivitesinin setlerini Hevy'deki gerçek tekrar ve ağırlıklarla düzeltir.

Saat tekrarları kendisi sayar (tek bacak hareketlerinde iki katı, ısınmada rastgele sayılar), ağırlığı
plandan alır ve hareketi kendisi tanımaya çalışır (yan bacak kaldırmayı "deadlift" sanabilir).
Hevy'de kullanıcının girdiği değerler esas alınır. Süre, nabız ve set zamanlaması değişmez.

Eşleme iki yoldan biriyle yapılır:
  1. Adım numarası (tercih edilen): antrenman saate Kulvar'dan gittiyse her set, saatteki antrenmanın
     hangi adımında yapıldığını (wktStepIndex) taşır. Adım → planlanan hareket → Hevy'deki aynı hareketin
     setleri. Saatin yanlış tanıdığı hareket adı planlanana döndürülür, ısınma "ısınma" olur ve antrenman
     bittikten sonra saatin kaydettiği anlamsız setler (ör. 999 tekrar) silinir.
  2. Kategori (yedek): serbest kaydedilmiş seanslarda hareket grupları sırayla ve Garmin kategorisine
     göre eşleştirilir; yalnızca tekrar ve ağırlık düzeltilir.
Garmin senkronunun sonunda çalışır; aynı Hevy içeriği bir kez yazılır.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging

from sqlalchemy.orm import Session

from . import strength_log, workout_library
from .clock import utcnow
from .models import Activity, ExerciseMedia, GarminSetSync, PlannedWorkout, Profile
from .strava_merge import match_hevy

logger = logging.getLogger("kulvar.garmin_sets")

LOOKBACK = dt.timedelta(days=3)
VERSION = "steps-v1"  # eşleme kuralı değişince önceden düzeltilen aktiviteler yeniden işlensin
LOOKAHEAD_GROUPS = 2  # Hevy hareketi için Garmin'de en fazla kaç grup ileriye bakılır (atlanmış/ek hareket)


def _exercises(hevy: Activity) -> list[dict]:
    return json.loads(hevy.raw_json or "{}").get("exercises") or []


def _hevy_items(db: Session, exercises: list[dict]) -> list[dict]:
    """Isınma/kardiyo dışındaki hareketler: Garmin kategorisi ve çalışma setleri, sırayla."""
    items = []
    for e in exercises:
        if workout_library._is_cardio_or_warmup(e.get("title") or ""):
            continue
        media = db.get(ExerciseMedia, e.get("exercise_template_id")) if e.get("exercise_template_id") else None
        sets = [s for s in e.get("sets") or [] if s.get("type") != "warmup"]
        if sets:
            items.append({"title": e.get("title") or "?", "category": (media.garmin_category if media else None) or "UNKNOWN", "sets": sets})
    return items


def _garmin_groups(payload: dict) -> list[dict]:
    """Ardışık ACTIVE setleri hareket (kategori, ad) değiştikçe gruplara ayırır; dinlenme setleri grubu bölmez."""
    groups: list[dict] = []
    for s in payload.get("exerciseSets") or []:
        if s.get("setType") != "ACTIVE":
            continue
        ex = (s.get("exercises") or [{}])[0]
        key = (ex.get("category") or "UNKNOWN", ex.get("name"))
        if groups and groups[-1]["key"] == key:
            groups[-1]["sets"].append(s)
        else:
            groups.append({"key": key, "sets": [s]})
    return groups


def apply_hevy(payload: dict, items: list[dict]) -> list[str]:
    """payload'u (get_activity_exercise_sets cevabı) yerinde düzeltir; yapılan değişikliklerin listesini döner."""
    groups, changes, j = _garmin_groups(payload), [], 0
    for item in items:
        match = next((k for k in range(j, min(j + 1 + LOOKAHEAD_GROUPS, len(groups))) if groups[k]["key"][0] == item["category"]), None)
        if match is None:
            continue
        j = match + 1
        for i, (g, h) in enumerate(zip(groups[match]["sets"], item["sets"]), start=1):
            if h.get("reps") is not None and g.get("repetitionCount") != h["reps"]:
                changes.append(f"{item['title']} {i}. set: {g.get('repetitionCount')} → {h['reps']} tekrar")
                g["repetitionCount"] = h["reps"]
            if h.get("weight_kg") is not None:
                grams = float(h["weight_kg"]) * 1000
                if g.get("weight") != grams:
                    changes.append(f"{item['title']} {i}. set: {(g.get('weight') or 0) / 1000:g} → {h['weight_kg']:g} kg")
                    g["weight"] = grams
    return changes


def _digest(exercises: list[dict]) -> str:
    return hashlib.sha1((VERSION + json.dumps(exercises, sort_keys=True)).encode()).hexdigest()


# --------------------------------------------------------------------------- adım numarasıyla eşleme

def fit_order(workout: dict) -> list[dict]:
    """Saatteki adım sırası (wktStepIndex bu listeye işaret eder): tekrar grubunun işareti, grubun
    adımlarından SONRA gelir (FIT biçimi)."""
    out = []
    for segment in workout.get("workoutSegments") or []:
        for step in segment.get("workoutSteps") or []:
            if step.get("type") == "RepeatGroupDTO":
                out.extend(step.get("workoutSteps") or [])
            out.append(step)
    return out


def _hevy_sets_by_template(exercises: list[dict]) -> tuple[dict, set]:
    """{hareket kimliği: [çalışma setleri]} ve ısınma/kardiyo dışı tüm hareket kimlikleri (kimlik yoksa ad)."""
    by_template, templates = {}, set()
    for e in exercises:
        tid = e.get("exercise_template_id") or e.get("title")
        if not tid or workout_library._is_cardio_or_warmup(e.get("title") or ""):
            continue
        templates.add(tid)
        by_template.setdefault(tid, []).extend(s for s in e.get("sets") or [] if s.get("type") != "warmup")
    return by_template, templates


def apply_by_steps(payload: dict, steps: list[dict], title_to_template: dict, hevy_sets: dict, hevy_templates: set,
                   trim: bool = False) -> list[str]:
    """Setleri saatteki antrenman adımlarına göre düzeltir (payload yerinde değişir); değişiklikleri döner.
    trim: kayıtta bir hareketin daha az seti varsa (kullanıcı onay ekranında sildiyse) fazlası Garmin'den de silinir."""
    sets = payload.get("exerciseSets") or []
    changes, counters, drop = [], {}, set()
    step_templates = set()
    for st in steps:
        if (st.get("stepType") or {}).get("stepTypeKey") == "interval":
            step_templates.add(title_to_template.get((st.get("description") or "")[:100]))
    for s in sets:
        idx = s.get("wktStepIndex")
        if s.get("setType") != "ACTIVE" or idx is None or not 0 <= idx < len(steps):
            continue
        step = steps[idx]
        kind = (step.get("stepType") or {}).get("stepTypeKey")
        title = (step.get("description") or "").strip() or "?"
        ex = (s.get("exercises") or [{}])[0]
        current = (ex.get("category") or "UNKNOWN", ex.get("name"))
        if kind == "warmup":
            if current != ("WARM_UP", None) or s.get("repetitionCount") is not None:
                changes.append(f"{title}: {current[0]} {s.get('repetitionCount')} tekrar → ısınma")
                s["exercises"] = [{"category": "WARM_UP", "name": None, "probability": 100.0}]
                s["repetitionCount"] = None
            continue
        if kind != "interval":
            continue
        n = counters.get(idx, 0)
        counters[idx] = n + 1
        planned = (step.get("category") or "UNKNOWN", step.get("exerciseName"))
        if current != planned:
            changes.append(f"{title} {n + 1}. set: {current[0]} → {title}")
            s["exercises"] = [{"category": planned[0], "name": planned[1], "probability": 100.0}]
        hevy = hevy_sets.get(title_to_template.get(title[:100]), [])
        h = hevy[n] if n < len(hevy) else None
        if h is None:
            if trim and hevy:
                drop.add(id(s))
                changes.append(f"{title} {n + 1}. set silindi (kayıtta yok)")
            continue
        if h.get("reps") is not None and s.get("repetitionCount") != h["reps"]:
            changes.append(f"{title} {n + 1}. set: {s.get('repetitionCount')} → {h['reps']} tekrar")
            s["repetitionCount"] = h["reps"]
        if h.get("weight_kg") is not None and s.get("weight") != float(h["weight_kg"]) * 1000:
            changes.append(f"{title} {n + 1}. set: {(s.get('weight') or 0) / 1000:g} → {h['weight_kg']:g} kg")
            s["weight"] = float(h["weight_kg"]) * 1000
    if drop:  # silinen setin hemen ardındaki dinlenme de gider
        kept, skip_rest = [], False
        for s in sets:
            if id(s) in drop:
                skip_rest = True
                continue
            if skip_rest and s.get("setType") == "REST":
                skip_rest = False
                continue
            skip_rest = False
            kept.append(s)
        sets = payload["exerciseSets"] = kept
    # Antrenman bittikten sonra saatin kaydettikleri: Hevy'de antrenman dışı bir hareket yoksa anlamsızdır.
    last = max((i for i, s in enumerate(sets) if s.get("wktStepIndex") is not None), default=None)
    if last is not None and hevy_templates <= step_templates:
        tail = sets[last + 1:]
        junk = [s for s in tail if s.get("setType") == "ACTIVE"]
        if junk:
            changes.append("antrenman sonrası anlamsız set silindi: "
                           + ", ".join(f"{s.get('repetitionCount')} tekrar/{round(s.get('duration') or 0)} sn" for s in junk))
            payload["exerciseSets"] = sets[:last + 1]
    return changes


def _title_to_template(db: Session, profile: Profile, workout_id: str | None) -> dict:
    """Saatteki adım başlığı (rutindeki hareket adı) → Hevy şablon kimliği; önce o güne planlanan rutin."""
    routines = []
    if workout_id:
        plan = db.query(PlannedWorkout).filter(PlannedWorkout.profile_id == profile.id,
                                               PlannedWorkout.garmin_workout_id == str(workout_id)).first()
        routine = workout_library.routine_for_plan(db, plan) if plan is not None else None
        if routine is not None:
            routines.append(routine)
    routines += workout_library.routines(db, profile.id)
    out = {}
    for r in reversed(routines):  # planlanan rutin en son yazılır, öncelikli olur
        for e in json.loads(r.exercises_json or "[]"):
            if e.get("title") and e.get("template_id"):
                out[e["title"][:100]] = e["template_id"]
    return out


def fix_recent(db: Session, profile: Profile, client, now: dt.datetime | None = None, dry_run: bool = False) -> list[str]:
    """Son 3 günün Garmin kuvvet aktivitelerini eşleşen Hevy antrenmanıyla düzeltir; değişiklikleri döner."""
    now = now or dt.datetime.now()
    since = now - LOOKBACK
    rows = db.query(Activity).filter(Activity.profile_id == profile.id, Activity.start_time >= since - dt.timedelta(minutes=30),
                                     Activity.source.in_(["garmin", "hevy"])).all()
    hevy_rows = [a for a in rows if a.source == "hevy"]
    all_changes = []
    for garmin in (a for a in rows if a.source == "garmin" and "strength" in (a.activity_type or "").lower() and a.start_time >= since):
        # Doğru değerler: Kulvar'da onaylanan kayıt; yoksa Hevy'deki antrenman.
        native = strength_log.confirmed_for_garmin(db, garmin.external_id)
        trim = native is not None and native.source == "kulvar"
        if trim:
            exercises, ref = strength_log.exercises_of(native), f"kulvar:{native.id}"
            titles = {(e["title"] or "")[:100]: e.get("exercise_template_id") or e["title"] for e in exercises}
        else:
            hevy = match_hevy(garmin, hevy_rows)
            if hevy is None:
                continue
            exercises, ref, titles = _exercises(hevy), hevy.external_id, None
        digest = _digest(exercises)
        record = db.get(GarminSetSync, garmin.external_id)
        if record is not None and record.digest == digest:
            continue
        payload = client.get_activity_exercise_sets(int(garmin.external_id))
        workout_id = json.loads(garmin.raw_json or "{}").get("workoutId")
        steps = []
        if workout_id and any(s.get("wktStepIndex") is not None for s in payload.get("exerciseSets") or []):
            try:
                steps = fit_order(client.get_workout_by_id(workout_id))
            except Exception:  # noqa: BLE001 - antrenman silinmiş olabilir: kategoriyle eşlemeye düş
                logger.info("Garmin antrenmanı %s okunamadı; kategoriyle eşleniyor", workout_id)
        if steps:
            by_template, templates = _hevy_sets_by_template(exercises)
            changes = apply_by_steps(payload, steps, titles or _title_to_template(db, profile, workout_id), by_template, templates, trim)
        else:
            changes = apply_hevy(payload, _hevy_items(db, exercises))
        all_changes += [f"{garmin.start_time:%d.%m %H:%M} · {c}" for c in changes]
        if dry_run:
            continue
        if changes:
            client.set_activity_exercise_sets(int(garmin.external_id), payload)
            logger.info("Garmin %s setleri %s ile düzeltildi (%d değişiklik)", garmin.external_id, ref, len(changes))
        if record is None:
            record = GarminSetSync(garmin_id=garmin.external_id, profile_id=profile.id)
            db.add(record)
        record.hevy_id, record.digest, record.changes, record.updated_at = ref, digest, len(changes), utcnow()
    if not dry_run:
        db.commit()
    return all_changes
