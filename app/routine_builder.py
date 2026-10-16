"""Koçun kuvvet rutini hazırlaması (ör. evdeki ekipmanla) ve onaydan sonra Hevy'ye kaydı.

Akış: form (gün sayısı, süre, ekipman) → arka planda Claude, Hevy'nin hareket kataloğundan seçerek
taslak hazırlar → Koç sayfasında animasyonlu önizleme → "Hevy'ye kaydet" ile rutinler Hevy'de
"Kulvar" klasörüne yazılır (aynı adlı rutin varsa yerinde güncellenir), rutinler yeniden çekilir ve
NOT'u bu rutin adıyla başlayan, Garmin'deki kuvvet planları yeni hareketlerle güncellenir.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re

import anthropic
import requests
from sqlalchemy.orm import Session

from .clock import utcnow
from . import exercise_catalog, i18n, training, workout_library
from .config import settings
from .crypto import decrypt_payload
from .db import SessionLocal
from .models import CoachMessage, Credential, ExerciseMedia, PlannedWorkout, Profile, RoutineDraft

logger = logging.getLogger("kulvar.routine_builder")

HEVY_URL = workout_library.HEVY_URL
FOLDER_TITLE = "Kulvar"

# Hevy ekipman anahtarı → etiket. Vücut ağırlığı ("none") her zaman dahil.
EQUIPMENT = {
    "dumbbell": "Dambıl",
    "kettlebell": "Kettlebell",
    "resistance_band": "Direnç bandı",
    "suspension": "TRX / askı",
    "barbell": "Halter",
    "plate": "Plaka",
    "other": "Diğer (bench, barfiks barı vb.)",
    "machine": "Makineler (salon)",
}
_SUPPORTED_TYPES = {"weight_reps", "reps_only", "bodyweight_reps", "bodyweight_weighted", "bodyweight_assisted", "duration"}
_WEIGHTED_TYPES = {"weight_reps", "bodyweight_weighted", "bodyweight_assisted"}


def default_prefix(lang: str) -> str:
    return "Home D" if lang == "en" else "Ev D"


def default_params(profile: Profile) -> dict:
    return {"days": 3, "minutes": 30, "equipment": ["dumbbell"], "weights": "", "notes": "",
            "prefix": default_prefix(i18n.normalize(profile.language))}


def latest_draft(db: Session, profile_id: str) -> RoutineDraft | None:
    return (db.query(RoutineDraft).filter(RoutineDraft.profile_id == profile_id, RoutineDraft.status != "discarded")
            .order_by(RoutineDraft.created_at.desc()).first())


def last_params(db: Session, profile: Profile) -> dict:
    """Formu son kullanılan değerlerle doldurmak için (taslak atılmış olsa bile)."""
    last = db.query(RoutineDraft).filter(RoutineDraft.profile_id == profile.id).order_by(RoutineDraft.created_at.desc()).first()
    params = default_params(profile)
    if last is not None and last.params_json:
        params.update(json.loads(last.params_json))
    from . import progression

    inv = progression.inventory(profile)  # evdeki ağırlıklar tanımlıysa form onlarla dolar
    if inv:
        params["weights"] = progression.inventory_text(inv, i18n.normalize(profile.language))
        params["equipment"] = sorted(set(params.get("equipment") or []) | set(inv))
    return params


def clean_params(profile: Profile, days, minutes, equipment: list[str], weights: str, prefix: str, notes: str) -> dict:
    def clamp(value, lo, hi, default):
        try:
            return max(lo, min(hi, int(value)))
        except (TypeError, ValueError):
            return default
    return {
        "days": clamp(days, 1, 5, 3),
        "minutes": clamp(minutes, 10, 120, 30),
        "equipment": [e for e in equipment if e in EQUIPMENT],
        "weights": (weights or "").strip()[:200],
        "prefix": (prefix or "").strip()[:20] or default_prefix(i18n.normalize(profile.language)),
        "notes": (notes or "").strip()[:500],
    }


def view(db: Session, draft: RoutineDraft | None, lang: str = "tr") -> list[dict]:
    """Önizleme: [{title, notes, exercises: exercises_view}]"""
    if draft is None or not draft.routines_json:
        return []
    return [{"title": r["title"], "notes": r.get("notes"), "exercises": workout_library.exercises_view(db, r["exercises"], lang)}
            for r in json.loads(draft.routines_json)]


def reset_interrupted(db: Session) -> None:
    """Servis yeniden başlarken yarıda kalan taslak/kayıt işlerini başarısız işaretler (tekrar denenebilsin)."""
    for draft in db.query(RoutineDraft).filter(RoutineDraft.status.in_(["running", "saving"])).all():
        draft.status = "failed" if draft.status == "running" else "save_failed"
        draft.error = "Servis yeniden başlatıldığı için iş yarıda kaldı; tekrar dene."
    db.commit()


# --------------------------------------------------------------------------- Hevy

def hevy_key(db: Session, profile_id: str) -> str | None:
    credential = db.query(Credential).filter(Credential.profile_id == profile_id, Credential.source == "hevy").first()
    return decrypt_payload(credential.encrypted_payload).get("api_key") if credential is not None else None


def _hevy_send(method: str, api_key: str, path: str, body: dict) -> dict:
    resp = requests.request(method, f"{HEVY_URL}{path}", json=body, timeout=30,
                            headers={"api-key": api_key, "Accept": "application/json", "Content-Type": "application/json"})
    if resp.status_code >= 400:
        raise RuntimeError(f"Hevy {method} {path.split('/')[1]}: {resp.status_code} {resp.text[:200]}")
    return resp.json() if resp.content else {}


def fetch_templates(api_key: str) -> list[dict]:
    out, page = [], 1
    while page <= 20:
        data = workout_library._hevy_get(api_key, "/exercise_templates", page=page, pageSize=100)
        out += data.get("exercise_templates") or []
        if page >= (data.get("page_count") or 1):
            break
        page += 1
    return out


def _ensure_folder(api_key: str) -> int | None:
    page = 1
    while page <= 10:
        data = workout_library._hevy_get(api_key, "/routine_folders", page=page, pageSize=10)
        for f in data.get("routine_folders") or []:
            if (f.get("title") or "").strip() == FOLDER_TITLE:
                return f.get("id")
        if page >= (data.get("page_count") or 1):
            break
        page += 1
    created = _hevy_send("POST", api_key, "/routine_folders", {"routine_folder": {"title": FOLDER_TITLE}})
    folder = created.get("routine_folder", created)
    if isinstance(folder, list):
        folder = folder[0] if folder else {}
    return folder.get("id")


# --------------------------------------------------------------------------- taslak (Claude)

_PROMPT = {
    "tr": """Bu kişi için {days} günlük bir kuvvet rutini seti hazırla; rutinler Hevy'ye kaydedilecek.

- Her antrenman yaklaşık {minutes} dakika (ısınma dahil).
- Kullanılabilir ekipman: {equipment}.{weights}
- Haftalık kuvvet hedefi {target} gün; rutinler haftalar boyunca sırayla dönüşümlü uygulanır, bu yüzden birlikte dengeli olsun (alt/üst vücut, itiş/çekiş, core).
- Koşucu: koşuyu destekleyen hareketlere (kalça, arka zincir, tek bacak, baldır, core) ağırlık ver; koç notlarındaki sakatlık ve kısıtlara uy.{notes}
- Rutin adları "{prefix}1 (…)", "{prefix}2 (…)" … biçiminde olsun; parantez içine o rutinin odağını 2-5 kelimeyle yaz, ör. "{prefix}1 (Alt vücut + itiş)".
- SADECE aşağıdaki Hevy hareket listesindeki kimlikleri kullan. Tip "duration" olanlarda tekrar yerine süre (sn) ver; ağırlık sadece weight_reps / bodyweight_weighted hareketlerde ve elde olan ağırlıklarla.
- Notlar ve özet Türkçe, kısa olsun.

Sadece şu JSON'u döndür:
{{"summary": "2-3 cümle: neden bu yapı", "routines": [{{"title": "{prefix}1 (Alt vücut + itiş)", "notes": "rutin notu", "exercises": [{{"id": "ŞABLON_ID", "sets": 3, "reps": 10, "duration_seconds": null, "weight_kg": 5, "rest_seconds": 60, "notes": "form ipucu"}}]}}]}}

Hevy hareket listesi (id | ad | tip | ana kas):
{catalog}""",
    "en": """Build a set of {days} strength routines for this person; they will be saved to Hevy.

- Each session takes about {minutes} minutes (including warm-up).
- Available equipment: {equipment}.{weights}
- The weekly strength target is {target} days; the routines rotate in order across weeks, so together they must be balanced (lower/upper body, push/pull, core).
- This is a runner: favour movements that support running (hips, posterior chain, single-leg, calves, core); respect injuries and limits in the coach notes.{notes}
- Name the routines "{prefix}1 (…)", "{prefix}2 (…)" …; put the routine's focus in the parentheses in 2-5 words, e.g. "{prefix}1 (Lower body + push)".
- Use ONLY ids from the Hevy exercise list below. For type "duration" give seconds instead of reps; give weight only for weight_reps / bodyweight_weighted exercises and only with the weights available.
- Write the notes and summary in English, briefly.

Return only this JSON:
{{"summary": "2-3 sentences: why this structure", "routines": [{{"title": "{prefix}1 (Lower body + push)", "notes": "routine note", "exercises": [{{"id": "TEMPLATE_ID", "sets": 3, "reps": 10, "duration_seconds": null, "weight_kg": 5, "rest_seconds": 60, "notes": "form cue"}}]}}]}}

Hevy exercise list (id | name | type | primary muscle):
{catalog}""",
}


def builder_catalog(db: Session, profile: Profile, allowed_eq: set[str]) -> list[dict]:
    """Koçun seçeceği hareketler: Hevy bağlıysa Hevy kataloğu, değilse free-exercise-db (Hevy'siz kullanım)."""
    api_key = hevy_key(db, profile.id)
    if api_key:
        return [t for t in fetch_templates(api_key) if t.get("equipment") in allowed_eq and t.get("type") in _SUPPORTED_TYPES]
    out = []
    for e in workout_library.load_fedb():
        if e.get("category") not in ("strength", "plyometrics", "powerlifting"):
            continue
        eq = exercise_catalog._EQUIPMENT.get(e.get("equipment") or "", "other")
        if eq not in allowed_eq:
            continue
        kind = "duration" if exercise_catalog.is_timed(e["name"]) else ("weight_reps" if eq != "none" else "reps_only")
        out.append({"id": exercise_catalog.FEDB_PREFIX + e["id"], "title": e["name"], "type": kind, "equipment": eq,
                    "primary_muscle_group": (e.get("primaryMuscles") or [None])[0]})
    return out


def start(db: Session, profile: Profile, params: dict) -> RoutineDraft | None:
    """Taslağı arka planda hazırlatır; aynı profil için çalışan bir iş varsa None döner."""
    from . import background

    if background.is_running(f"routines-{profile.id}"):
        return None
    for old in db.query(RoutineDraft).filter(RoutineDraft.profile_id == profile.id, RoutineDraft.status.in_(["ready", "failed", "save_failed"])):
        old.status = "discarded"
    draft = RoutineDraft(profile_id=profile.id, status="running", params_json=json.dumps(params, ensure_ascii=False))
    db.add(draft)
    db.commit()
    background.start(f"routines-{profile.id}", _generate_job, draft.id)
    return draft


def _generate_job(draft_id: str) -> None:
    db = SessionLocal()
    try:
        draft = db.get(RoutineDraft, draft_id)
        if draft is None:
            return
        try:
            generate(db, draft)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Rutin taslağı hazırlanamadı")
            db.rollback()
            draft = db.get(RoutineDraft, draft_id)
            draft.status, draft.error = "failed", str(exc)[:300]
            db.commit()
    finally:
        db.close()


def generate(db: Session, draft: RoutineDraft) -> None:
    from . import coach_chat
    from .routers.coach import build_profile_brief

    profile = db.get(Profile, draft.profile_id)
    lang = i18n.normalize(profile.language)
    params = json.loads(draft.params_json)

    def fail(message: str) -> None:
        draft.status, draft.error = "failed", message
        db.commit()

    if not coach_chat.api_key_configured():
        return fail(i18n.t(lang, "Claude API anahtarı tanımlı değil."))
    if coach_chat.month_cost(db) >= settings.coach_monthly_budget_usd:
        return fail(i18n.t(lang, "Bu ayki Koç bütçesi doldu (${spent} / ${budget}). Bütçeyi .env'de KULVAR_COACH_MONTHLY_BUDGET_USD ile artırabilirsin.",
                           spent=f"{coach_chat.month_cost(db):.2f}", budget=f"{settings.coach_monthly_budget_usd:.2f}"))
    allowed_eq = {"none", *params["equipment"]}
    catalog = builder_catalog(db, profile, allowed_eq)
    by_id = {t["id"]: t for t in catalog}
    eq_names = [i18n.t(lang, "vücut ağırlığı")] + [i18n.t(lang, EQUIPMENT[e]) for e in params["equipment"]]
    prompt = _PROMPT[lang].format(
        days=params["days"], minutes=params["minutes"], equipment=", ".join(eq_names),
        weights=(f" {i18n.t(lang, 'Ağırlıklar')}: {params['weights']}." if params.get("weights") else ""),
        notes=(f"\n- {i18n.t(lang, 'Ek istek')}: {params['notes']}" if params.get("notes") else ""),
        target=training.targets(training.week_template(profile))["strength"], prefix=params["prefix"],
        catalog="\n".join(f"{t['id']} | {t['title']} | {t['type']} | {t.get('primary_muscle_group') or '-'}" for t in catalog),
    )
    brief = build_profile_brief(db, profile, include_prompt=False)
    client = anthropic.Anthropic()
    with client.beta.messages.stream(
        model=settings.coach_model,
        max_tokens=16000,
        system=coach_chat._system_blocks(profile),  # koç talimatları ve notları
        messages=[{"role": "user", "content": f"Kulvar verisi:\n\n{brief}\n\n{prompt}"}],
        output_config={"effort": settings.coach_effort},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    ) as stream:
        final = stream.get_final_message()
    cost = coach_chat._cost(final.usage, final.model)
    thread = coach_chat.current_thread(db, profile)
    db.add(CoachMessage(  # bütçe ve kullanım tablosuna yansısın; sohbet geçmişine girmez
        thread_id=thread.id, profile_id=profile.id, seq=-1, role="health", content_json="[]",
        model=final.model, stop_reason=final.stop_reason, cost_usd=cost,
        input_tokens=final.usage.input_tokens, output_tokens=final.usage.output_tokens,
        cache_read_tokens=final.usage.cache_read_input_tokens, cache_write_tokens=final.usage.cache_creation_input_tokens,
    ))
    text = "".join(b.text for b in final.content if b.type == "text")
    try:
        raw = json.loads(text[text.find("{"): text.rfind("}") + 1])
    except ValueError:
        return fail(i18n.t(lang, "Koçun cevabı okunamadı; tekrar dene."))
    routines = normalize(raw, by_id, params["prefix"])
    if not routines:
        return fail(i18n.t(lang, "Koç listeden geçerli hareket seçmedi; tekrar dene."))

    for tid in {e["template_id"] for r in routines for e in r["exercises"]}:  # önizlemede animasyon için
        if tid.startswith(exercise_catalog.FEDB_PREFIX):
            exercise_catalog.ensure(db, tid)
        elif db.get(ExerciseMedia, tid) is None:
            t = by_id[tid]
            db.add(ExerciseMedia(template_id=tid, title=t["title"], primary_muscle=t.get("primary_muscle_group"),
                                 equipment=t.get("equipment"), thumbnail_url=t.get("thumbnail_url")))
    draft.routines_json = json.dumps(routines, ensure_ascii=False)
    draft.summary = (raw.get("summary") or "").strip()[:1000] or None
    draft.cost_usd = cost
    db.commit()
    workout_library.map_pending(db)
    draft.status, draft.error = "ready", None
    db.commit()


_PLACEHOLDERS = {"kısa odak", "odak", "short focus", "focus", "…", "...", ""}


def focus_from_notes(notes: str, limit: int = 50) -> str:
    """Rutin notunun ilk cümlesi ("Squat paterni + itiş + yan core.") → kısa odak etiketi."""
    first = re.split(r"[.!?\n]", notes.strip(), maxsplit=1)[0].strip(" —-:")
    while len(first) > limit and " + " in first:
        first = first.rsplit(" + ", 1)[0]
    return first if len(first) <= limit else ""


def fix_title(title: str, notes: str) -> str:
    """Koç parantez içine yer tutucuyu ("kısa odak") kopyaladıysa odağı nottan türetir."""
    m = re.match(r"^(.*?)\s*\(([^)]*)\)\s*$", title)
    if not m or m.group(2).strip().lower() not in _PLACEHOLDERS:
        return title
    focus = focus_from_notes(notes)
    return f"{m.group(1)} ({focus})" if focus else m.group(1)


def normalize(raw: dict, by_id: dict, prefix: str) -> list[dict]:
    """Claude'un JSON'unu StrengthRoutine.exercises_json biçimine çevirir; listede olmayan kimlikleri atar."""
    def num(value, lo, hi, default):
        try:
            return max(lo, min(hi, int(float(value))))
        except (TypeError, ValueError):
            return default

    out = []
    for i, r in enumerate((raw.get("routines") or [])[:5], start=1):
        title = fix_title((r.get("title") or "").strip(), str(r.get("notes") or ""))
        if not title.lower().startswith(f"{prefix}{i}".lower()):
            title = f"{prefix}{i}" + (f" ({title})" if title else "")
        exercises = []
        for e in (r.get("exercises") or [])[:12]:
            t = by_id.get(str(e.get("id") or "").strip())
            if t is None:
                continue
            if t["type"] == "duration":
                one = {"type": "normal", "duration_seconds": num(e.get("duration_seconds"), 5, 600, 30)}
            else:
                one = {"type": "normal", "reps": num(e.get("reps"), 1, 50, 10)}
                try:
                    weight = float(e.get("weight_kg") or 0)
                except (TypeError, ValueError):
                    weight = 0
                if t["type"] in _WEIGHTED_TYPES and 0 < weight <= 300:
                    one["weight_kg"] = weight
            exercises.append({
                "template_id": t["id"], "title": t["title"], "rest_seconds": num(e.get("rest_seconds"), 0, 300, 60),
                "notes": (str(e.get("notes") or "").strip()[:200] or None),
                "sets": [dict(one) for _ in range(num(e.get("sets"), 1, 6, 3))],
            })
        if exercises:
            out.append({"title": title[:120], "notes": (str(r.get("notes") or "").strip()[:500] or None), "exercises": exercises})
    return out


# --------------------------------------------------------------------------- Hevy'ye kayıt

def save_in_background(db: Session, draft: RoutineDraft) -> bool:
    from . import background

    if draft.status not in ("ready", "save_failed"):
        return False
    draft.status, draft.error = "saving", None
    db.commit()
    return background.start(f"routines-{draft.profile_id}", _save_job, draft.id)


def _save_job(draft_id: str) -> None:
    db = SessionLocal()
    try:
        draft = db.get(RoutineDraft, draft_id)
        if draft is None:
            return
        try:
            save_to_hevy(db, draft)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Rutinler Hevy'ye kaydedilemedi")
            db.rollback()
            draft = db.get(RoutineDraft, draft_id)
            draft.status, draft.error = "save_failed", str(exc)[:300]
            db.commit()
    finally:
        db.close()


def _hevy_body(routine: dict) -> dict:
    return {"routine": {
        "title": routine["title"],
        "notes": routine.get("notes") or "",
        "exercises": [{
            "exercise_template_id": e["template_id"], "superset_id": None, "rest_seconds": e.get("rest_seconds"),
            "notes": e.get("notes"),
            "sets": [{"type": s.get("type") or "normal", "weight_kg": s.get("weight_kg"), "reps": s.get("reps"),
                      "duration_seconds": s.get("duration_seconds")} for s in e["sets"]],
        } for e in routine["exercises"]],
    }}


def save_to_hevy(db: Session, draft: RoutineDraft) -> None:
    from .integrations import garmin as garmin_integration

    from . import routines as routine_ops, strength_log

    profile = db.get(Profile, draft.profile_id)
    api_key = hevy_key(db, profile.id)
    routines = json.loads(draft.routines_json)
    if not api_key or strength_log.source_of(db, profile) == "kulvar" or not routine_ops.hevy_compatible(
            [e for r in routines for e in r["exercises"]]):
        # Kulvar'a kaydet: aynı adlı rutin varsa güncellenir (Hevy'ye bağlıysa ve uygunsa Hevy'de de), yoksa yeni Kulvar rutini.
        existing = {workout_library._routine_key(r.title): r for r in workout_library.routines(db, profile.id)}
        for r in routines:
            old = existing.get(workout_library._routine_key(r["title"]))
            if old is not None:
                routine_ops.save(db, profile, old, r["title"], r["exercises"])
            else:
                routine_ops.new_native(db, profile, r["title"], r["exercises"])
                db.commit()
        routine_ops.update_garmin_plans(db, profile, {workout_library._routine_key(r["title"]) for r in routines})
        draft.status, draft.error, draft.saved_at = "saved", None, utcnow()
        db.commit()
        return
    existing = {workout_library._routine_key(r.title): r for r in workout_library.routines(db, profile.id)}
    folder_id = None
    for r in routines:
        old = existing.get(workout_library._routine_key(r["title"]))
        body = _hevy_body(r)
        if old is not None:  # aynı adlı rutin (ör. "Ev D1") varsa yerinde güncellenir, kopya oluşmaz
            _hevy_send("PUT", api_key, f"/routines/{old.hevy_id}", body)
        else:
            if folder_id is None:
                folder_id = _ensure_folder(api_key)
            body["routine"]["folder_id"] = folder_id
            _hevy_send("POST", api_key, "/routines", body)
    workout_library.refresh_routines(db, profile, api_key)

    # NOT'u bu rutinlerden biriyle başlayan, Garmin'deki gelecek kuvvet planları yeni hareketlerle güncellenir
    keys = {workout_library._routine_key(r["title"]) for r in routines}
    credential = db.query(Credential).filter(Credential.profile_id == profile.id, Credential.source == "garmin").first()
    if credential is not None:
        payload = decrypt_payload(credential.encrypted_payload)
        plans = (db.query(PlannedWorkout).filter(PlannedWorkout.profile_id == profile.id, PlannedWorkout.workout_type == "strength",
                                                 PlannedWorkout.date >= dt.date.today(), PlannedWorkout.status.is_(None),
                                                 PlannedWorkout.garmin_workout_id.isnot(None)).all())
        for w in plans:
            routine = workout_library.routine_for_plan(db, w)
            if routine is not None and workout_library._routine_key(routine.title) in keys:
                w.garmin_error = garmin_integration.update_strength_routine(payload, w)
    draft.status, draft.error, draft.saved_at = "saved", None, utcnow()
    db.commit()
