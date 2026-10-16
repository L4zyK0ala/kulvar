"""Kulvar'ın kendi kuvvet antrenmanı kaydı: Hevy olmadan da tekrar/ağırlık bilgisinin doğru kaynağı.

Akış (kaynak "kulvar"): antrenman saatle yapılır → Garmin senkronunda saatin kaydından bir taslak
oluşur: o güne planlanan rutinin hareketleri, saatin kaydettiği set sayısıyla; tekrar/ağırlık plandan,
süreli hareketlerde süre saatten (saatin ölçtüğü süre doğrudur, saydığı tekrar değil). Telegram'a
"Onayla" mesajı gider; farklı bir şey yapıldıysa onay ekranında düzeltilir. Onaylanan kayıt Garmin'deki
setleri (garmin_sets) ve Strava açıklamasını (strava_merge) besler.

Kaynak "hevy" iken (geçiş dönemi) Hevy'deki antrenman sessizce onaylı kayıt olarak kopyalanır.
"""
from __future__ import annotations

import datetime as dt
import json
import logging

from sqlalchemy.orm import Session

from . import i18n, workout_library
from .clock import utcnow
from .models import Activity, Credential, ExerciseMedia, PlannedWorkout, Profile, StrengthSet, StrengthWorkout

logger = logging.getLogger("kulvar.strength_log")

SOURCES = {"hevy": "Hevy", "kulvar": "Kulvar"}
LOOKBACK = dt.timedelta(days=3)
MATCH_TOLERANCE = dt.timedelta(minutes=20)
LIVE_TOLERANCE = dt.timedelta(minutes=45)  # telefonda başlatıp saati sonradan açmak olağan


def source_of(db: Session, profile: Profile) -> str:
    if profile.strength_source in SOURCES:
        return profile.strength_source
    has_hevy = db.query(Credential).filter(Credential.profile_id == profile.id, Credential.source == "hevy").first() is not None
    return "hevy" if has_hevy else "kulvar"


# --------------------------------------------------------------------------- doğru değerler

def exercises_of(workout: StrengthWorkout) -> list[dict]:
    """Hevy antrenmanıyla aynı biçim: [{title, exercise_template_id, sets: [{type, reps, weight_kg, duration_seconds}]}]."""
    out: dict[int, dict] = {}
    for s in workout.sets:
        e = out.setdefault(s.position, {"title": s.title, "exercise_template_id": s.template_id, "sets": []})
        e["sets"].append({"type": s.set_type, "reps": s.reps, "weight_kg": s.weight_kg, "duration_seconds": s.duration_s,
                          "wkt_step_index": s.wkt_step_index})
    return [out[k] for k in sorted(out)]


def confirmed_for_garmin(db: Session, garmin_external_id: str) -> StrengthWorkout | None:
    return (db.query(StrengthWorkout).filter(StrengthWorkout.garmin_activity_id == garmin_external_id,
                                             StrengthWorkout.status == "confirmed").first())


def confirmed_near(db: Session, profile_id: str, start: dt.datetime) -> StrengthWorkout | None:
    """Strava kaydıyla eşleşen onaylı kayıt (başlangıcı 20 dk içinde)."""
    rows = (db.query(StrengthWorkout).filter(StrengthWorkout.profile_id == profile_id, StrengthWorkout.status == "confirmed",
                                             StrengthWorkout.start_time.between(start - MATCH_TOLERANCE, start + MATCH_TOLERANCE)).all())
    return min(rows, key=lambda w: abs(w.start_time - start), default=None)


# --------------------------------------------------------------------------- taslak

def _garmin_names() -> dict:
    from garminconnect import exercises as gx

    out = {}
    for e in gx.EXERCISES:
        e = e if isinstance(e, dict) else {"name": e[0], "category": e[1], "exercise": e[2]}
        out[(e["category"], e["exercise"])] = e["name"]
    return out


def _media_by_garmin(db: Session) -> dict:
    return {(m.garmin_category, m.garmin_name): m for m in db.query(ExerciseMedia).filter(ExerciseMedia.garmin_name.isnot(None))}


def _routine_for_activity(db: Session, profile: Profile, workout_id) -> tuple:
    if not workout_id:
        return None, None
    plan = (db.query(PlannedWorkout).filter(PlannedWorkout.profile_id == profile.id,
                                            PlannedWorkout.garmin_workout_id == str(workout_id)).first())
    return plan, (workout_library.routine_for_plan(db, plan) if plan is not None else None)


def build_draft(db: Session, profile: Profile, garmin: Activity, payload: dict, steps: list[dict], plan, routine) -> StrengthWorkout:
    """Saatin kayıtlarından (exerciseSets) ve planlanan rutinden onay taslağı."""
    sets = payload.get("exerciseSets") or []
    w = StrengthWorkout(profile_id=profile.id, start_time=garmin.start_time, duration_s=garmin.duration_s,
                        title=(routine.title if routine else (plan.title if plan else None)) or i18n.t(profile.language or "tr", "Kuvvet"),
                        routine_id=routine.id if routine else None, planned_workout_id=plan.id if plan else None,
                        garmin_activity_id=garmin.external_id, status="draft", source="kulvar")
    position = 0
    if routine is not None and steps:
        planned = {(e.get("title") or "")[:100]: e for e in json.loads(routine.exercises_json or "[]")}
        for idx, step in enumerate(steps):
            kind = (step.get("stepType") or {}).get("stepTypeKey")
            if kind not in ("warmup", "interval"):
                continue
            watch = [s for s in sets if s.get("setType") == "ACTIVE" and s.get("wktStepIndex") == idx]
            title = (step.get("description") or "").strip() or "?"
            plan_e = planned.get(title[:100], {})
            plan_sets = [s for s in plan_e.get("sets") or [] if s.get("type") != "warmup"] or plan_e.get("sets") or []
            if kind == "warmup":
                secs = sum(s.get("duration") or 0 for s in watch) or (plan_sets[0].get("duration_seconds") if plan_sets else None)
                w.sets.append(StrengthSet(position=position, set_index=0, title=title, template_id=plan_e.get("template_id"),
                                          set_type="warmup", duration_s=round(secs) if secs else None, wkt_step_index=idx))
                position += 1
                continue
            for i, g in enumerate(watch):
                p = plan_sets[min(i, len(plan_sets) - 1)] if plan_sets else {}
                timed = bool(p.get("duration_seconds")) and not p.get("reps")
                w.sets.append(StrengthSet(
                    position=position, set_index=i, title=title, template_id=plan_e.get("template_id"),
                    reps=None if timed else p.get("reps"), weight_kg=p.get("weight_kg"),
                    duration_s=round(g.get("duration") or 0) if timed else None,
                    planned_reps=p.get("reps"), planned_weight_kg=p.get("weight_kg"), planned_duration_s=p.get("duration_seconds"),
                    watch_reps=g.get("repetitionCount"), watch_weight_kg=(g.get("weight") or 0) / 1000 or None, wkt_step_index=idx))
            position += 1
    else:  # serbest kayıt: saatin tanıdığı hareketler, saatin değerleriyle
        names, media = _garmin_names(), _media_by_garmin(db)
        last_key, i = None, 0
        for g in sets:
            if g.get("setType") != "ACTIVE":
                continue
            ex = (g.get("exercises") or [{}])[0]
            key = (ex.get("category") or "UNKNOWN", ex.get("name"))
            if key != last_key:
                if last_key is not None:
                    position += 1
                last_key, i = key, 0
            m = media.get(key)
            title = (m.title if m else None) or names.get(key) or key[0].replace("_", " ").title()
            w.sets.append(StrengthSet(position=position, set_index=i, title=title, template_id=m.template_id if m else None,
                                      reps=g.get("repetitionCount"), weight_kg=(g.get("weight") or 0) / 1000 or None,
                                      watch_reps=g.get("repetitionCount"), watch_weight_kg=(g.get("weight") or 0) / 1000 or None,
                                      wkt_step_index=g.get("wktStepIndex")))
            i += 1
    return w


def from_hevy(profile: Profile, hevy: Activity, garmin: Activity | None) -> StrengthWorkout:
    return from_hevy_data(profile, json.loads(hevy.raw_json or "{}"), hevy.external_id, hevy.start_time, hevy.duration_s,
                          hevy.activity_type, garmin)


def from_hevy_data(profile: Profile, data: dict, hevy_id: str, start: dt.datetime, duration_s: float | None,
                   fallback_title: str | None = None, garmin: Activity | None = None) -> StrengthWorkout:
    w = StrengthWorkout(profile_id=profile.id, start_time=start, duration_s=duration_s,
                        title=data.get("title") or fallback_title or "Hevy", hevy_id=hevy_id,
                        garmin_activity_id=garmin.external_id if garmin else None, status="confirmed", source="hevy",
                        confirmed_at=utcnow())
    for pos, e in enumerate(data.get("exercises") or []):
        warm = workout_library._is_cardio_or_warmup(e.get("title") or "")
        for i, s in enumerate(e.get("sets") or []):
            w.sets.append(StrengthSet(position=pos, set_index=i, title=e.get("title") or "?", template_id=e.get("exercise_template_id"),
                                      set_type="warmup" if warm or s.get("type") == "warmup" else "normal",
                                      reps=s.get("reps"), weight_kg=s.get("weight_kg"), duration_s=s.get("duration_seconds")))
    return w


def ingest_recent(db: Session, profile: Profile, client, now: dt.datetime | None = None) -> list[StrengthWorkout]:
    """Garmin senkronunda: son 3 günün saat kuvvet kayıtlarına Kulvar kaydı oluşturur. Yeni taslakları döner."""
    from .garmin_sets import fit_order
    from .strava_merge import match_hevy

    now = now or dt.datetime.now()
    since = now - LOOKBACK
    rows = db.query(Activity).filter(Activity.profile_id == profile.id, Activity.start_time >= since - MATCH_TOLERANCE,
                                     Activity.source.in_(["garmin", "hevy"])).all()
    hevy_rows = [a for a in rows if a.source == "hevy"]
    source = source_of(db, profile)
    drafts, copies = [], []
    for garmin in (a for a in rows if a.source == "garmin" and "strength" in (a.activity_type or "").lower() and a.start_time >= since):
        existing = db.query(StrengthWorkout).filter(StrengthWorkout.garmin_activity_id == garmin.external_id).first()
        hevy = match_hevy(garmin, hevy_rows)
        if existing is None:  # telefonda canlı kaydedilen antrenman: saat kaydı ona bağlanır, taslak açılmaz
            live = (db.query(StrengthWorkout).filter(StrengthWorkout.profile_id == profile.id, StrengthWorkout.source == "kulvar",
                                                     StrengthWorkout.status.in_(["live", "confirmed"]),
                                                     StrengthWorkout.garmin_activity_id.is_(None),
                                                     StrengthWorkout.start_time.between(garmin.start_time - LIVE_TOLERANCE,
                                                                                        garmin.start_time + LIVE_TOLERANCE)).first())
            if live is not None:
                live.garmin_activity_id = garmin.external_id
                continue
        if existing is not None:
            if existing.status == "draft" and hevy is not None:  # taslak beklerken Hevy'de de kaydedildi: Hevy esas
                db.delete(existing)
                db.flush()
                copies.append(from_hevy(profile, hevy, garmin))
                db.add(copies[-1])
            continue
        if hevy is not None:
            copied = db.query(StrengthWorkout).filter(StrengthWorkout.hevy_id == hevy.external_id).first()
            if copied is not None:  # Hevy senkronu zaten kopyaladı: saat kaydını bağla
                copied.garmin_activity_id = copied.garmin_activity_id or garmin.external_id
            else:
                copies.append(from_hevy(profile, hevy, garmin))
                db.add(copies[-1])
            continue
        if source != "kulvar":
            continue  # Hevy kaydı henüz gelmedi; sonraki senkronda tekrar bakılır
        payload = client.get_activity_exercise_sets(int(garmin.external_id))
        workout_id = json.loads(garmin.raw_json or "{}").get("workoutId")
        plan, routine = _routine_for_activity(db, profile, workout_id)
        steps = []
        if workout_id and routine is not None:
            try:
                steps = fit_order(client.get_workout_by_id(workout_id))
            except Exception:  # noqa: BLE001
                logger.info("Garmin antrenmanı %s okunamadı; serbest kayıt olarak işleniyor", workout_id)
        draft = build_draft(db, profile, garmin, payload, steps, plan, routine)
        db.add(draft)
        drafts.append(draft)
    db.commit()
    for d in drafts:
        send_prompt(db, profile, d)
    _check_progression(db, copies)
    return drafts


def _nearby_garmin(db: Session, profile: Profile, start: dt.datetime) -> Activity | None:
    rows = (db.query(Activity).filter(Activity.profile_id == profile.id, Activity.source == "garmin",
                                      Activity.start_time.between(start - MATCH_TOLERANCE, start + MATCH_TOLERANCE)).all())
    rows = [a for a in rows if "strength" in (a.activity_type or "").lower()]
    return min(rows, key=lambda a: abs(a.start_time - start), default=None)


def mirror_hevy(db: Session, profile: Profile) -> int:
    """Hevy senkronunda: Kulvar'da karşılığı olmayan Hevy antrenmanlarını kopyalar; Hevy'de düzenlenen son
    antrenmanların kopyasını yeniler. Kulvar'da onaylanmış (kaynak kulvar) kayıtlara dokunmaz."""
    existing = {w.hevy_id: w for w in db.query(StrengthWorkout).filter(StrengthWorkout.profile_id == profile.id,
                                                                        StrengthWorkout.hevy_id.isnot(None))}
    added, new = 0, []
    for hevy in db.query(Activity).filter(Activity.profile_id == profile.id, Activity.source == "hevy").all():
        old = existing.get(hevy.external_id)
        if old is not None:
            if old.source == "hevy" and exercises_of(old) != exercises_of(from_hevy(profile, hevy, None)):
                old.sets = from_hevy(profile, hevy, None).sets  # Hevy'de düzenlendi
                old.title = json.loads(hevy.raw_json or "{}").get("title") or old.title
            continue
        new.append(from_hevy(profile, hevy, _nearby_garmin(db, profile, hevy.start_time)))
        db.add(new[-1])
        added += 1
    db.commit()
    _check_progression(db, new)
    return added


_history_checked: dict[str, dt.date] = {}


def history_due(profile: Profile) -> bool:
    """Hevy geçmişi aktarımı günde bir kez (zaten aktarılanlar atlanır, eksik kalan tamamlanır)."""
    return _history_checked.get(profile.id) != dt.date.today()


def import_hevy_history(db: Session, profile: Profile, api_key: str, max_pages: int = 50) -> int:
    """Hevy'deki tüm geçmişi Kulvar kaydına aktarır (zaten olanlar atlanır). Aktarılan sayısını döner."""
    from .integrations.hevy import _parse_dt

    known = {h for (h,) in db.query(StrengthWorkout.hevy_id).filter(StrengthWorkout.profile_id == profile.id,
                                                                     StrengthWorkout.hevy_id.isnot(None))}
    added, page = 0, 1
    while page <= max_pages:
        data = workout_library._hevy_get(api_key, "/workouts", page=page, pageSize=10)
        for w in data.get("workouts") or []:
            start, end = _parse_dt(w.get("start_time")), _parse_dt(w.get("end_time"))
            if not w.get("id") or start is None or str(w["id"]) in known:
                continue
            db.add(from_hevy_data(profile, w, str(w["id"]), start, (end - start).total_seconds() if end else None,
                                  garmin=_nearby_garmin(db, profile, start)))
            known.add(str(w["id"]))
            added += 1
        if page >= (data.get("page_count") or 1):
            break
        page += 1
    db.commit()
    _history_checked[profile.id] = dt.date.today()
    return added


# --------------------------------------------------------------------------- görünüm ve onay

def summary_lines(workout: StrengthWorkout, lang: str) -> list[str]:
    lines = []
    for e in exercises_of(workout):
        text = workout_library._sets_summary(e["sets"], lang)
        if text:
            lines.append(f"{e['title']}: {text}")
    return lines


def totals(workout: StrengthWorkout) -> tuple[int, float]:
    work = [s for s in workout.sets if s.set_type != "warmup" and not workout_library._is_cardio_or_warmup(s.title)]
    return len(work), sum((s.weight_kg or 0) * (s.reps or 0) for s in work)


def view(db: Session, workout: StrengthWorkout) -> list[dict]:
    """Onay ekranı: hareket başına görsel + setler."""
    groups: dict[int, dict] = {}
    for s in workout.sets:
        g = groups.get(s.position)
        if g is None:
            media = db.get(ExerciseMedia, s.template_id) if s.template_id else None
            frames = json.loads(media.frames) if media and media.frames else []
            g = groups[s.position] = {"position": s.position, "title": s.title, "frames": frames,
                                      "warmup": s.set_type == "warmup", "sets": [],
                                      "timed": False}
        g["sets"].append(s)
        g["timed"] = g["timed"] or (s.duration_s is not None and s.reps is None)
    return [groups[k] for k in sorted(groups)]


def apply_form(workout: StrengthWorkout, form: dict) -> None:
    """Onay ekranındaki değerleri yazar (reps_<id>, kg_<id>, sec_<id>)."""
    def num(key, cast):
        raw = (form.get(key) or "").strip().replace(",", ".")
        if raw == "":
            return None
        try:
            value = cast(float(raw))
        except ValueError:
            return None
        return value if value >= 0 else None

    for s in workout.sets:
        if f"reps_{s.id}" in form:
            s.reps = num(f"reps_{s.id}", int)
        if f"kg_{s.id}" in form:
            s.weight_kg = num(f"kg_{s.id}", float) or None
        if f"sec_{s.id}" in form:
            s.duration_s = num(f"sec_{s.id}", float)


def add_set(workout: StrengthWorkout, position: int) -> None:
    same = [s for s in workout.sets if s.position == position]
    if not same:
        return
    last = same[-1]
    workout.sets.append(StrengthSet(position=position, set_index=last.set_index + 1, title=last.title, template_id=last.template_id,
                                    set_type=last.set_type, reps=last.reps, weight_kg=last.weight_kg, duration_s=last.duration_s,
                                    planned_reps=last.planned_reps, planned_weight_kg=last.planned_weight_kg,
                                    planned_duration_s=last.planned_duration_s))


def confirm(db: Session, workout: StrengthWorkout) -> None:
    """Kaydı kesinleştirir, planı yapıldı işaretler ve Garmin/Strava güncellemesini başlatır."""
    from . import background

    workout.status, workout.confirmed_at = "confirmed", utcnow()
    if workout.planned_workout_id:
        plan = db.get(PlannedWorkout, workout.planned_workout_id)
        if plan is not None and plan.status is None:
            plan.status, plan.status_at = "done", utcnow()
    db.commit()
    background.start(f"strength-push-{workout.profile_id}", _push_job, workout.profile_id)
    _check_progression(db, [workout])


def _check_progression(db: Session, workouts: list[StrengthWorkout]) -> None:
    """Yeni onaylanan (ya da Hevy'den gelen son) antrenmanlardan sonra kademeli yüklenme önerisi."""
    from . import progression

    cutoff = dt.datetime.now() - LOOKBACK
    for w in workouts:
        if w.status != "confirmed" or w.start_time < cutoff:
            continue
        try:
            progression.check(db, db.get(Profile, w.profile_id), w)
        except Exception:  # noqa: BLE001 - öneri hatası kaydı bozmasın
            db.rollback()
            logger.exception("Kademeli yüklenme önerisi oluşturulamadı")


def _push_job(profile_id: str) -> None:
    """Onaydan sonra Garmin setlerini ve Strava açıklamasını hemen günceller (bir sonraki senkronu beklemeden)."""
    from .crypto import decrypt_payload
    from .db import SessionLocal
    from .integrations import garmin as garmin_integration, strava as strava_integration
    from . import garmin_sets, strava_merge

    db = SessionLocal()
    try:
        profile = db.get(Profile, profile_id)
        creds = {c.source: c for c in db.query(Credential).filter(Credential.profile_id == profile_id)}
        if "garmin" in creds:
            try:
                client = garmin_integration._login_from_payload(decrypt_payload(creds["garmin"].encrypted_payload))
                garmin_sets.fix_recent(db, profile, client)
            except Exception:  # noqa: BLE001
                db.rollback()
                logger.exception("Onay sonrası Garmin setleri güncellenemedi")
        if "strava" in creds:
            payload = decrypt_payload(creds["strava"].encrypted_payload)
            if strava_integration.can_write(payload):
                try:
                    client, _ = strava_integration._client_from_payload(payload)
                    strava_merge.enrich_recent(db, profile, client)
                except Exception:  # noqa: BLE001
                    db.rollback()
                    logger.exception("Onay sonrası Strava açıklaması güncellenemedi")
    finally:
        db.close()


# --------------------------------------------------------------------------- Telegram

def send_prompt(db: Session, profile: Profile, workout: StrengthWorkout) -> None:
    from . import telegram_bot as tb
    from .config import settings

    lang = i18n.normalize(profile.language)
    lines = [f"🏋️ {tb.T(profile, '**{title}** antrenmanın saatten geldi ({time}, {minutes} dk).', title=workout.title, time=f'{workout.start_time:%H:%M}', minutes=round((workout.duration_s or 0) / 60))}"]
    lines += [f"• {line}" for line in summary_lines(workout, lang)]
    lines += ["", tb.T(profile, "Planlandığı gibi yaptıysan onayla; farklı bir şey varsa düzenle.")]
    url = f"{settings.public_url}/strength/workouts/{workout.id}"
    buttons = [[{"text": tb.T(profile, "✅ Onayla"), "callback_data": f"sw:{workout.id}:ok"},
                {"text": tb.T(profile, "✏️ Düzenle"), "url": url}]]
    message_id = tb.send_to_profile(profile, "\n".join(lines), buttons)
    if message_id is None:  # bazı adresleri Telegram buton olarak kabul etmiyor: bağlantıyı metne yaz
        message_id = tb.send_to_profile(profile, "\n".join(lines + [url]), [buttons[0][:1]])
    workout.telegram_message_id = message_id
    db.commit()


def brief_lines(db: Session, profile: Profile, limit: int = 4) -> list[str]:
    """Koç verisi: Kulvar'da onaylanan son kuvvet antrenmanları (Hevy'den kopyalananlar Hevy bölümünde)."""
    rows = (db.query(StrengthWorkout).filter(StrengthWorkout.profile_id == profile.id, StrengthWorkout.status == "confirmed",
                                             StrengthWorkout.source == "kulvar")
            .order_by(StrengthWorkout.start_time.desc()).limit(limit).all())
    if not rows:
        return []
    lines = ["## Kuvvet Antrenmanları (Kulvar kaydı)"]
    for w in rows:
        n, volume = totals(w)
        lines.append(f"- {w.start_time:%d.%m.%Y} {w.title} · {n} set · {volume:.0f} kg: " + "; ".join(summary_lines(w, "tr")))
    lines.append("")
    return lines
