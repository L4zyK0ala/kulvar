from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..clock import utcnow
from .. import exercise_catalog, feedback, i18n, live_workout, progression, routines, strength_log, workout_library
from ..db import get_db
from ..deps import get_current_profile
from ..measurements import measurement_overview
from ..models import Activity, BodyMeasurement, Profile, StrengthRoutine, StrengthWorkout
from ..templating import templates

router = APIRouter()

def _parse_workout(row: Activity, lang: str = "tr") -> dict:
    try:
        raw = json.loads(row.raw_json) if row.raw_json else {}
    except (TypeError, ValueError):
        raw = {}
    exercises = []
    total_sets = 0
    volume = 0.0
    for ex in raw.get("exercises") or []:
        sets = ex.get("sets") or []
        if not workout_library._is_cardio_or_warmup(ex.get("title") or ""):
            total_sets += sum(1 for s in sets if s.get("type") != "warmup")  # Strava açıklamasıyla aynı sayım
        for s in sets:
            if s.get("weight_kg") and s.get("reps"):
                volume += s["weight_kg"] * s["reps"]
        exercises.append({"title": ex.get("title") or "Egzersiz", "summary": workout_library._sets_summary(sets, lang) or "—", "set_count": len(sets)})
    return {
        "activity": row,
        "title": raw.get("title") or row.activity_type or "Antrenman",
        "exercises": exercises,
        "total_sets": total_sets,
        "volume": round(volume),
        "source": "hevy",
    }


def _native_workout(w: StrengthWorkout, lang: str) -> dict:
    """Kulvar'da onaylanan kayıt, Hevy kartlarıyla aynı biçimde."""
    sets, volume = strength_log.totals(w)
    return {
        "activity": SimpleNamespace(start_time=w.start_time),
        "title": w.title,
        "exercises": [{"title": e["title"], "summary": workout_library._sets_summary(e["sets"], lang) or "—"}
                      for e in strength_log.exercises_of(w)],
        "total_sets": sets,
        "volume": round(volume),
        "source": w.source,
        "id": w.id if w.source == "kulvar" else None,  # Hevy kopyası Hevy'de düzenlenir
    }


def _localized_overview(body: dict | None, lang: str) -> dict | None:
    """Grafik başlıkları ve seri adları JS'e JSON olarak gittiği için burada çevrilir."""
    if body:
        for chart in body.get("charts", []):
            chart["title"] = i18n.t(lang, chart["title"])
            for series in chart.get("series", []):
                if series.get("name"):
                    series["name"] = i18n.t(lang, series["name"])
    return body


@router.get("/strength")
def strength(
    request: Request,
    tab: str = "workouts",
    db: Session = Depends(get_db),
    current: Profile | None = Depends(get_current_profile),
):
    profiles = db.query(Profile).order_by(Profile.created_at).all()
    if current is None:
        return templates.TemplateResponse(request, "profile_picker.html", {"profiles": profiles})

    if tab == "routines":
        return templates.TemplateResponse(request, "strength.html", {
            "tab": "routines", "profiles": profiles, "active_profile": current,
            "routine_list": [{"r": r, "native": routines.is_native(r), "count": len(routines.exercises(r))}
                             for r in workout_library.routines(db, current.id)],
            "home_equipment": {k: ", ".join(f"{w:g}" for w in v) for k, v in progression.inventory(current).items()},
        })

    if tab == "measurements":
        measurements = (
            db.query(BodyMeasurement)
            .filter(BodyMeasurement.profile_id == current.id)
            .order_by(BodyMeasurement.date.desc())
            .all()
        )
        return templates.TemplateResponse(
            request,
            "strength.html",
            {
                "tab": "measurements",
                "profiles": profiles,
                "active_profile": current,
                "measurements": measurements,
                "body": _localized_overview(measurement_overview(measurements, current), request.state.lang),
            },
        )

    rows = (
        db.query(Activity)
        .filter(Activity.profile_id == current.id, Activity.source == "hevy")
        .order_by(Activity.start_time.desc())
        .limit(30)
        .all()
    )
    # Kulvar kaydı esas: onaylananlar ve Hevy kopyaları; henüz kopyalanmamış Hevy antrenmanları yedek olarak.
    native = (db.query(StrengthWorkout).filter(StrengthWorkout.profile_id == current.id)
              .order_by(StrengthWorkout.start_time.desc()).limit(40).all())
    drafts = [w for w in native if w.status == "draft" and w.source == "kulvar"]
    mirrored = {w.hevy_id for w in native if w.hevy_id}
    workouts = [_native_workout(w, request.state.lang) for w in native if w.status == "confirmed"]
    workouts += [_parse_workout(row, request.state.lang) for row in rows if row.external_id not in mirrored]
    workouts.sort(key=lambda w: w["activity"].start_time, reverse=True)
    workouts = workouts[:30]

    month_start = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_workouts = [w for w in workouts if w["activity"].start_time >= month_start]
    summary = None
    if month_workouts:
        summary = {
            "count": len(month_workouts),
            "total_sets": sum(w["total_sets"] for w in month_workouts),
            "volume": sum(w["volume"] for w in month_workouts),
        }

    return templates.TemplateResponse(
        request,
        "strength.html",
        {"tab": "workouts", "workouts": workouts, "summary": summary, "active_profile": current, "profiles": profiles,
         "drafts": drafts, "strength_source": strength_log.source_of(db, current), "strength_sources": strength_log.SOURCES,
         "suggestions": [{"s": s, "text": progression.describe(s, request.state.lang)} for s in progression.pending(db, current)],
         "live": live_workout.active(db, current.id), "routine_options": workout_library.routines(db, current.id),
         "today_routine": live_workout.todays_routine(db, current)},
    )


@router.post("/strength/{profile_id}/source")
def set_strength_source(profile_id: str, source: str = Form(...), db: Session = Depends(get_db)):
    """Kuvvet kaydının kaynağı: hevy (Hevy'deki antrenman kopyalanır) | kulvar (saatten taslak + onay)."""
    profile = db.get(Profile, profile_id)
    if profile is not None and source in strength_log.SOURCES:
        profile.strength_source = source
        db.commit()
    return RedirectResponse("/strength", status_code=303)


@router.get("/strength/workouts/{workout_id}")
def strength_workout(request: Request, workout_id: str, db: Session = Depends(get_db),
                     current: Profile | None = Depends(get_current_profile)):
    workout = db.get(StrengthWorkout, workout_id)
    if workout is None:
        return RedirectResponse("/strength", status_code=303)
    profile = db.get(Profile, workout.profile_id)
    profiles = db.query(Profile).order_by(Profile.created_at).all()
    sets, volume = strength_log.totals(workout)
    return templates.TemplateResponse(request, "strength_workout.html", {
        "profiles": profiles, "active_profile": profile, "workout": workout,
        "groups": strength_log.view(db, workout), "total_sets": sets, "volume": round(volume),
        "saved": request.query_params.get("saved"),
        "rpe": {"kind": "sw", "id": workout.id, "fb": feedback.find(db, profile.id, feedback.refs_for_workout(db, workout))}
        if workout.status == "confirmed" else None,
    })


@router.post("/strength/workouts/{workout_id}")
async def update_strength_workout(request: Request, workout_id: str, db: Session = Depends(get_db)):
    """Onay ekranı: değerleri kaydeder, ardından action'a göre set ekler/siler, onaylar ya da taslağı atar."""
    workout = db.get(StrengthWorkout, workout_id)
    if workout is None:
        return RedirectResponse("/strength", status_code=303)
    form = await request.form()
    action = str(form.get("action") or "save")
    strength_log.apply_form(workout, {k: str(v) for k, v in form.items()})
    anchor = ""
    if action.startswith("add:"):
        position = int(action[4:]) if action[4:].isdigit() else -1
        strength_log.add_set(workout, position)
        anchor = f"#ex-{position}"
    elif action.startswith("del:"):
        target = next((s for s in workout.sets if s.id == action[4:]), None)
        if target is not None:
            anchor = f"#ex-{target.position}"
            workout.sets.remove(target)
    elif action == "discard" and workout.status == "draft":
        db.delete(workout)
        db.commit()
        return RedirectResponse("/strength", status_code=303)
    db.commit()
    if action == "confirm":
        strength_log.confirm(db, workout)
        return RedirectResponse(f"/strength/workouts/{workout.id}?saved=confirmed", status_code=303)
    return RedirectResponse(f"/strength/workouts/{workout.id}{'?saved=1' if action == 'save' else ''}{anchor}", status_code=303)


# --------------------------------------------------------------------------- rutin düzenleyici

@router.post("/strength/{profile_id}/routines/new")
def new_routine(profile_id: str, db: Session = Depends(get_db)):
    profile = db.get(Profile, profile_id)
    if profile is None:
        return RedirectResponse("/strength?tab=routines", status_code=303)
    routine = routines.new_native(db, profile, i18n.t(i18n.normalize(profile.language), "Yeni rutin"))
    db.commit()
    return RedirectResponse(f"/strength/routines/{routine.id}", status_code=303)


@router.get("/strength/routines/{routine_id}")
def edit_routine(request: Request, routine_id: str, q: str = "", saved: str = "", db: Session = Depends(get_db)):
    routine = db.get(StrengthRoutine, routine_id)
    if routine is None:
        return RedirectResponse("/strength?tab=routines", status_code=303)
    profile = db.get(Profile, routine.profile_id)
    items = routines.exercises(routine)
    return templates.TemplateResponse(request, "strength_routine.html", {
        "profiles": db.query(Profile).order_by(Profile.created_at).all(), "active_profile": profile,
        "routine": routine, "native": routines.is_native(routine),
        "items": [{"e": e, "view": v, "timed": any(st.get("duration_seconds") for st in e.get("sets") or [])}
                  for e, v in zip(items, workout_library.exercises_view(db, items, request.state.lang))],
        "q": q, "results": exercise_catalog.search(db, q) if q else [], "saved": saved,
    })


@router.post("/strength/routines/{routine_id}")
async def update_routine(request: Request, routine_id: str, db: Session = Depends(get_db)):
    """Düzenleyici: değerleri yazar; action'a göre hareket/set ekler, siler, sıralar ya da kaydeder.
    Ara adımlar yalnız Kulvar'a yazılır; Hevy'ye ve saatteki planlara "Kaydet" ile gider."""
    routine = db.get(StrengthRoutine, routine_id)
    if routine is None:
        return RedirectResponse("/strength?tab=routines", status_code=303)
    profile = db.get(Profile, routine.profile_id)
    form = {k: str(v) for k, v in (await request.form()).items()}
    action = form.get("action") or "stay"
    items = routines.parse_form(routines.exercises(routine), form)
    title = (form.get("title") or routine.title).strip() or routine.title
    if action == "delete":
        routines.delete(db, routine)
        return RedirectResponse("/strength?tab=routines", status_code=303)
    if action == "save":
        try:
            outcome = routines.save(db, profile, routine, title, items)
        except Exception as exc:  # noqa: BLE001 - Hevy'ye yazılamadıysa Kulvar'daki hali yine kaydedilir
            routine.title, routine.exercises_json = title[:120], json.dumps(items, ensure_ascii=False)
            db.commit()
            outcome = "error"
            logging.getLogger("kulvar.routines").warning("Rutin Hevy'ye yazılamadı: %s", exc)
        return RedirectResponse(f"/strength/routines/{routine.id}?saved={outcome}", status_code=303)
    if action not in ("stay", "search"):
        items = routines.apply_action(db, items, action)
    routine.title, routine.exercises_json = title[:120], json.dumps(items, ensure_ascii=False)
    db.commit()
    query = form.get("q", "").strip() if action == "search" else ""
    anchor = "#ekle" if action in ("search",) or action.startswith("add_ex") else ""
    return RedirectResponse(f"/strength/routines/{routine.id}" + (f"?q={quote(query)}" if query else "") + anchor, status_code=303)


@router.post("/strength/suggestions/{suggestion_id}/{action}")
def decide_suggestion(suggestion_id: str, action: str, db: Session = Depends(get_db)):
    """Kademeli yüklenme önerisi: apply (rutine uygula) | reject."""
    from ..models import ProgressionSuggestion

    s = db.get(ProgressionSuggestion, suggestion_id)
    if s is not None:
        progression.decide(db, db.get(Profile, s.profile_id), s, apply=action == "apply")
    return RedirectResponse("/strength", status_code=303)


# --------------------------------------------------------------------------- canlı antrenman (telefon)

@router.post("/strength/{profile_id}/live/start")
def live_start(profile_id: str, routine_id: str = Form(...), db: Session = Depends(get_db)):
    profile, routine = db.get(Profile, profile_id), db.get(StrengthRoutine, routine_id)
    if profile is None or routine is None or routine.profile_id != profile_id:
        return RedirectResponse("/strength", status_code=303)
    workout = live_workout.start(db, profile, routine)
    return RedirectResponse(f"/strength/live/{workout.id}", status_code=303)


@router.get("/strength/live/{workout_id}")
def live_page(request: Request, workout_id: str, db: Session = Depends(get_db)):
    workout = db.get(StrengthWorkout, workout_id)
    if workout is None:
        return RedirectResponse("/strength", status_code=303)
    if workout.status != "live":
        return RedirectResponse(f"/strength/workouts/{workout.id}", status_code=303)
    profile = db.get(Profile, workout.profile_id)
    return templates.TemplateResponse(request, "strength_live.html", {
        "profiles": db.query(Profile).order_by(Profile.created_at).all(), "active_profile": profile, "workout": workout,
        "state": live_workout.state(db, workout, request.state.lang),
    })


@router.post("/strength/live/{workout_id}/api")
async def live_api(request: Request, workout_id: str, db: Session = Depends(get_db)):
    """Ekranın arka planda çağırdığı uç: set güncelle ya da ekle."""
    workout = db.get(StrengthWorkout, workout_id)
    if workout is None or workout.status != "live":
        return JSONResponse({"ok": False, "error": "not_live"}, status_code=409)
    data = await request.json()
    op = data.get("op")
    if op == "set":
        ok = live_workout.update_set(workout, str(data.get("set_id")), data)
        db.commit()
        return JSONResponse({"ok": ok})
    if op == "add_set":
        new = live_workout.add_set(workout, int(data.get("position", -1)))
        db.commit()
        if new is None:
            return JSONResponse({"ok": False}, status_code=400)
        return JSONResponse({"ok": True, "set": {"id": new.id, "reps": new.reps, "kg": new.weight_kg, "sec": new.duration_s,
                                                  "done": False, "prev": None}})
    return JSONResponse({"ok": False, "error": "unknown_op"}, status_code=400)


@router.post("/strength/live/{workout_id}/finish")
def live_finish(workout_id: str, db: Session = Depends(get_db)):
    workout = db.get(StrengthWorkout, workout_id)
    if workout is None or workout.status != "live":
        return RedirectResponse("/strength", status_code=303)
    if not live_workout.finish(db, workout):
        return RedirectResponse(f"/strength/live/{workout.id}?empty=1", status_code=303)
    return RedirectResponse(f"/strength/workouts/{workout.id}?saved=confirmed", status_code=303)


@router.post("/strength/live/{workout_id}/discard")
def live_discard(workout_id: str, db: Session = Depends(get_db)):
    workout = db.get(StrengthWorkout, workout_id)
    if workout is not None and workout.status == "live":
        db.delete(workout)
        db.commit()
    return RedirectResponse("/strength", status_code=303)


@router.post("/strength/{profile_id}/equipment")
def save_equipment(profile_id: str, dumbbell: str = Form(""), kettlebell: str = Form(""), db: Session = Depends(get_db)):
    """Evdeki ağırlıklar: kademeli yüklenme bir sonraki mevcut ağırlığı önerir, rutin oluşturucu bunlarla çalışır."""
    profile = db.get(Profile, profile_id)
    if profile is not None:
        data = {k: progression.parse_weights(v) for k, v in (("dumbbell", dumbbell), ("kettlebell", kettlebell))}
        data = {k: v for k, v in data.items() if v}
        profile.home_equipment = json.dumps(data) if data else None
        db.commit()
    return RedirectResponse("/strength?tab=routines#ekipman", status_code=303)
