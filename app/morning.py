"""Sabah hazırlık kontrolü: gece verisi (uyku, dinlenik nabız, Body Battery) kötüyse ve bugün zor bir koşu
planlıysa "kolaya çevireyim mi?" önerisi. Profil başına günde en fazla bir kez; Telegram + Panel.

Puanlama (7 günlük kişisel ortalamaya göre; 2 puan ve üstü = öneri):
  - Uyku skoru ≤ 55 → 2 · ortalamanın 12 altında → 1
  - Dinlenik nabız ortalamanın 8 üstünde → 2 · 5 üstünde → 1
  - Body Battery zirvesi < 40 ya da ortalamanın 20 altında → 1 (ortalaması güvenilir değilse — saat gece
    takılmıyorsa — hesaba katılmaz)
"Kolaya çevir": süre/mesafe %30 azalır, nabız tavanı LTHR'a göre Z2 olur, eski plan nota yazılır ve Garmin'deki
antrenman yenisiyle değiştirilir.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re

from sqlalchemy.orm import Session

from . import heart, i18n
from .clock import utcnow
from .db import SessionLocal
from .models import Credential, DailyMetric, MorningCheck, PlannedWorkout, Profile

logger = logging.getLogger("kulvar.morning")

WINDOW = (6, 11)  # yerel saat: kontrol bu aralıkta, gece verisi gelince bir kez
EASE_FACTOR = 0.7
_HARD_WORDS = re.compile(r"tempo|interval|aral[ıi]k|e[şs]ik|threshold|fartlek|repeat|hill|yoku[şs]|uzun|long|yar[ıi][şs]|race|progres|vo2", re.I)
_EASY_WORDS = re.compile(r"easy|kolay|recovery|toparlanma|hafif|shake", re.I)
_INTERVAL_SHAPE = re.compile(r"\d+\s*[x×]\s*\d+")  # notta "6x400", "5 × 1 km" gibi açık interval yapısı


def _metrics(db: Session, profile_id: str, today: dt.date) -> tuple[dict, dict]:
    """(bugün, önceki 7 günün ortalaması) — kaynaklar birleştirilir."""
    rows = db.query(DailyMetric).filter(DailyMetric.profile_id == profile_id, DailyMetric.date >= today - dt.timedelta(days=7),
                                        DailyMetric.date <= today).all()
    days: dict[dt.date, dict] = {}
    for r in rows:
        bucket = days.setdefault(r.date, {})
        for field in ("sleep_score", "resting_hr", "body_battery_max"):
            if getattr(r, field) is not None:
                bucket.setdefault(field, getattr(r, field))
    base = {}
    for field in ("sleep_score", "resting_hr", "body_battery_max"):
        prev = [v[field] for d, v in days.items() if d < today and v.get(field) is not None]
        if prev:
            base[field] = sum(prev) / len(prev)
    return days.get(today, {}), base


def assess(today_values: dict, base: dict) -> tuple[int, list[dict]]:
    """(puan, gerekçeler). Gerekçe: {key, value, base} — metin dile göre üretilir."""
    points, reasons = 0, []
    sleep, b_sleep = today_values.get("sleep_score"), base.get("sleep_score")
    if sleep is not None:
        if sleep <= 55:
            points += 2
            reasons.append({"key": "sleep", "value": sleep, "base": b_sleep})
        elif b_sleep and sleep <= b_sleep - 12:
            points += 1
            reasons.append({"key": "sleep", "value": sleep, "base": b_sleep})
    rhr, b_rhr = today_values.get("resting_hr"), base.get("resting_hr")
    if rhr is not None and b_rhr:
        if rhr >= b_rhr + 8:
            points += 2
            reasons.append({"key": "rhr", "value": rhr, "base": b_rhr})
        elif rhr >= b_rhr + 5:
            points += 1
            reasons.append({"key": "rhr", "value": rhr, "base": b_rhr})
    bb, b_bb = today_values.get("body_battery_max"), base.get("body_battery_max")
    if bb is not None and b_bb and b_bb >= 45 and (bb < 40 or bb <= b_bb - 20):
        points += 1
        reasons.append({"key": "bb", "value": bb, "base": b_bb})
    return points, reasons


def reason_text(r: dict, lang: str) -> str:
    base = f" ({i18n.t(lang, 'ort.')} {r['base']:.0f})" if r.get("base") else ""
    label = {"sleep": i18n.t(lang, "Uyku skoru"), "rhr": i18n.t(lang, "Dinlenik nabız"), "bb": "Body Battery"}[r["key"]]
    return f"{label} {r['value']}{base}"


def is_hard(plan: PlannedWorkout, profile: Profile) -> bool:
    """Zor koşu: başlıkta tempo/interval/uzun gibi bir kelime, notta açık interval yapısı, Z3 ve üstü nabız
    hedefi ya da 12 km üstü. Başlıkta "Easy/Kolay" varsa asla zor sayılmaz (notlar başka günlerden de bahseder)."""
    if plan.workout_type != "running" or _EASY_WORDS.search(plan.title or ""):
        return False
    if _HARD_WORDS.search(plan.title or "") or _INTERVAL_SHAPE.search(plan.notes or ""):
        return True
    zones = heart.lthr_zones(profile.lthr)
    if plan.target_hr_min and zones and plan.target_hr_min >= zones[2]["lo"]:
        return True
    return bool(plan.target_distance_m and plan.target_distance_m >= 12000)


def todays_hard_plan(db: Session, profile: Profile, today: dt.date) -> PlannedWorkout | None:
    from .routers.coach import active_plans

    plans = active_plans(db, profile.id).filter(PlannedWorkout.date == today, PlannedWorkout.status.is_(None)).all()
    return next((p for p in plans if is_hard(p, profile)), None)


def run(now: dt.datetime | None = None) -> int:
    """Zamanlayıcıdan: penceredeyse her profil için bugünün kontrolünü bir kez yapar. Gönderilen öneri sayısı."""
    now = now or dt.datetime.now()
    if not WINDOW[0] <= now.hour < WINDOW[1]:
        return 0
    db = SessionLocal()
    sent = 0
    try:
        for profile in db.query(Profile).all():
            try:
                sent += check(db, profile, now.date(), final=now.hour >= WINDOW[1] - 1)
            except Exception:  # noqa: BLE001 - bir profilin hatası diğerini durdurmasın
                db.rollback()
                logger.exception("Sabah kontrolü başarısız (%s)", profile.name)
    finally:
        db.close()
    return sent


def check(db: Session, profile: Profile, today: dt.date, final: bool = False) -> int:
    """Bugünün kontrolü (zaten yapıldıysa hiçbir şey yapmaz). Gece verisi henüz yoksa bekler; final=True ise
    pencerenin son saatinde veri gelmemişse kontrolü kapatır."""
    if db.query(MorningCheck).filter_by(profile_id=profile.id, date=today).first() is not None:
        return 0
    values, base = _metrics(db, profile.id, today)
    if values.get("sleep_score") is None and values.get("resting_hr") is None:
        if final:
            db.add(MorningCheck(profile_id=profile.id, date=today, status="none"))
            db.commit()
        return 0
    plan = todays_hard_plan(db, profile, today)
    points, reasons = assess(values, base)
    check = MorningCheck(profile_id=profile.id, date=today, status="none", reasons_json=json.dumps(reasons))
    db.add(check)
    if plan is None or points < 2:
        db.commit()
        return 0
    check.status, check.planned_workout_id = "sent", plan.id
    db.commit()
    _send(db, profile, check, plan)
    return 1


def _send(db: Session, profile: Profile, check: MorningCheck, plan: PlannedWorkout) -> None:
    from . import telegram_bot as tb

    lang = i18n.normalize(profile.language)
    reasons = " · ".join(reason_text(r, lang) for r in json.loads(check.reasons_json or "[]"))
    text = ("🌅 " + tb.T(profile, "Toparlanman bugün düşük görünüyor: {reasons}.", reasons=reasons) + "\n"
            + tb.T(profile, "Bugünkü **{title}** antrenmanını kolay koşuya çevireyim mi? (süre %30 kısalır, nabız Z2)", title=plan.title))
    check.telegram_message_id = tb.send_to_profile(profile, text, [[
        {"text": tb.T(profile, "✅ Kolaya çevir"), "callback_data": f"mc:{check.id}:ease"},
        {"text": tb.T(profile, "❌ Planı koru"), "callback_data": f"mc:{check.id}:keep"},
    ]])
    db.commit()


def ease(db: Session, profile: Profile, plan: PlannedWorkout, reasons: str) -> str | None:
    """Planı kolay koşuya çevirir; Garmin'deyse antrenmanı yenisiyle değiştirir. Garmin hata metni ya da None."""
    from .crypto import decrypt_payload
    from .integrations import garmin as garmin_integration

    old = f"{plan.title}" + (f" — {plan.notes}" if plan.notes else "")
    if plan.target_duration_s:
        plan.target_duration_s = round(plan.target_duration_s * EASE_FACTOR / 60) * 60
    if plan.target_distance_m:
        plan.target_distance_m = round(plan.target_distance_m * EASE_FACTOR / 500) * 500
    zones = heart.lthr_zones(profile.lthr)
    plan.target_hr_min, plan.target_hr_max = None, (zones[1]["hi"] if zones else plan.target_hr_max)
    plan.title = f"{plan.title} · {i18n.t(i18n.normalize(profile.language), 'kolay')}"[:120]
    plan.notes = (f"{i18n.t(i18n.normalize(profile.language), 'Toparlanma düşük ({reasons}); kolaya çevrildi. Önceki plan: {old}', reasons=reasons, old=old)}")[:1000]
    db.commit()
    if not plan.garmin_workout_id:
        return None
    credential = db.query(Credential).filter(Credential.profile_id == profile.id, Credential.source == "garmin").first()
    if credential is None:
        return None
    payload = decrypt_payload(credential.encrypted_payload)
    garmin_integration.unschedule_planned_workout(payload, plan)
    plan.garmin_workout_id = None
    workout_id, error = garmin_integration.push_planned_workout(payload, plan)
    plan.garmin_workout_id, plan.garmin_error = workout_id, error
    db.commit()
    return error


def decide(db: Session, profile: Profile, check: MorningCheck, action: str) -> str:
    """ease | keep → "eased" | "kept" | "done" (zaten karar verilmiş ya da plan değişmiş)."""
    if check.profile_id != profile.id or check.status != "sent":
        return "done"
    plan = db.get(PlannedWorkout, check.planned_workout_id) if check.planned_workout_id else None
    check.decided_at = utcnow()
    if action != "ease" or plan is None or plan.status is not None:
        check.status = "kept"
        db.commit()
        return "kept"
    lang = i18n.normalize(profile.language)
    ease(db, profile, plan, ", ".join(reason_text(r, lang) for r in json.loads(check.reasons_json or "[]")))
    check.status = "eased"
    db.commit()
    return "eased"


def today_check(db: Session, profile: Profile) -> MorningCheck | None:
    return db.query(MorningCheck).filter_by(profile_id=profile.id, date=dt.date.today()).first()


def brief_lines(db: Session, profile: Profile) -> list[str]:
    c = today_check(db, profile)
    if c is None or not c.reasons_json or c.reasons_json == "[]":
        return []
    reasons = ", ".join(reason_text(r, "tr") for r in json.loads(c.reasons_json))
    decision = {"sent": "öneri gönderildi, cevap bekleniyor", "eased": "bugünkü zor koşu kolaya çevrildi",
                "kept": "kullanıcı planı korudu", "none": "bugün zor antrenman yok ya da sinyaller sınırda"}[c.status]
    return ["## Sabah Hazırlık", f"- {reasons} → {decision}", ""]
