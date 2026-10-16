"""İlaçlar ve takviyeler: liste, Telegram hatırlatmaları, uyum takibi ve koç değerlendirmesi.

- Doktorun verdiği ilacın (prescription) dozu ve saatleri kullanıcının girdiğidir; koç bunları
  değiştirmez, yalnızca zamanlama çakışmalarını not edebilir.
- Takviyeler (supplement) için koç; tahliller, doktor notları ve diğer ilaçlara bakarak gerekli /
  gereksiz / doktora sor kararı ve alma zamanı önerir (tek Claude çağrısıyla tüm liste).
- Hatırlatmalar dakikada bir kontrol edilir; servis o an kapalıysa 2 saate kadar geç gönderilir.
  Cevapsız kalanlar ertesi gün "missed" olur.
"""
from __future__ import annotations

import datetime as dt
import logging
import re

import anthropic
from sqlalchemy.orm import Session

from .clock import utcnow
from . import i18n
from .config import settings
from .db import SessionLocal
from .models import CoachMessage, Medication, MedicationLog, Profile

logger = logging.getLogger("kulvar.meds")

KINDS = {"supplement": "Takviye", "prescription": "Doktorun verdiği ilaç"}
STATUSES = {"active": "Kullanıyorum", "considering": "Kullanmayı düşünüyorum", "stopped": "Bıraktım"}
VERDICTS = {"needed": "Gerekli görünüyor", "not_needed": "Gerekli görünmüyor", "ask_doctor": "Doktora sor"}
LATE_SEND_WINDOW = dt.timedelta(hours=2)
SNOOZE = dt.timedelta(minutes=30)

_TIME_RE = re.compile(r"^([01]?\d|2[0-3])[:.]([0-5]\d)$")


def parse_times(raw: str | None) -> list[str]:
    """ "8:00, 20.00 08:00" → ["08:00", "20:00"] (geçersizleri atar, sıralar, tekrarları birleştirir)."""
    out = set()
    for part in re.split(r"[,\s;]+", raw or ""):
        m = _TIME_RE.match(part.strip())
        if m:
            out.add(f"{int(m.group(1)):02d}:{m.group(2)}")
    return sorted(out)


def times_of(med: Medication) -> list[str]:
    return parse_times(med.times)


def days_of(med: Medication) -> list[int]:
    """Seçili haftanın günleri (0=Pzt … 6=Paz); boş liste her gün demek."""
    return sorted({int(d) for d in (med.days or "").split(",") if d.strip().isdigit() and 0 <= int(d) <= 6})


# Dozun başındaki adet: "2 tablet" → 2, "yarım tablet" → 0,5. "1000 IU" gibi birimler adet değildir → 1.
_UNIT_RE = re.compile(
    r"^\s*(\d+(?:[.,]\d+)?|yarım|half)\s*(tablet|tab|tb|kapsül|kapsul|kaps|caps|capsule|softgel|draje|pastil|"
    r"damla|drop|ölçek|olcek|scoop|saşe|şase|sase|sachet|adet|pill|puf|puff|ampul|kaşık|kasik|piece)", re.I)


def units_per_intake(dose: str | None) -> float:
    m = _UNIT_RE.match(dose or "")
    if not m:
        return 1.0
    raw = m.group(1).lower()
    value = 0.5 if raw in ("yarım", "half") else float(raw.replace(",", "."))
    return value if value > 0 else 1.0


def end_date_for(quantity: int | None, dose: str | None, times: list[str], days: list[int],
                 start: dt.date) -> dt.date | None:
    """Kutudaki adet bittiğinde son alımın günü: her alımda dozdaki adet, günde saat sayısı kadar alım,
    sadece seçili günler (boşsa her gün). Formdaki canlı hesap (coach_brief.html) aynı kuralı izler."""
    if not quantity or quantity <= 0:
        return None
    intakes = int(quantity / units_per_intake(dose) + 1e-9)
    if intakes < 1:
        return None
    per_day, day = max(1, len(times)), start
    for _ in range(3660):
        if not days or day.weekday() in days:
            intakes -= per_day
            if intakes <= 0:
                return day
        day += dt.timedelta(days=1)
    return None


def parse_days(values: list[str]) -> str | None:
    days = sorted({int(v) for v in values if str(v).isdigit() and 0 <= int(v) <= 6})
    return None if not days or len(days) == 7 else ",".join(map(str, days))


def days_text(med: Medication, lang: str = "tr") -> str:
    days = days_of(med)
    return ", ".join(i18n.weekday_name(lang, d, short=True) for d in days) if days else i18n.t(lang, "her gün")


def is_due_day(med: Medication, day: dt.date) -> bool:
    days = days_of(med)
    return ((med.start_date is None or med.start_date <= day) and (med.end_date is None or day <= med.end_date)
            and (not days or day.weekday() in days))


def label(med: Medication) -> str:
    return med.name + (f" ({med.dose})" if med.dose else "")


# --------------------------------------------------------------------------- hatırlatmalar

def _send_reminder(profile: Profile, med: Medication, log: MedicationLog) -> str | None:
    from . import telegram_bot as tb

    text = "💊 " + tb.T(profile, "{name} zamanı", name=label(med))
    if med.instructions:
        text += f"\n{med.instructions}"
    return tb.send_to_profile(profile, text, [[
        {"text": tb.T(profile, "✅ Aldım"), "callback_data": f"md:{log.id}:take"},
        {"text": tb.T(profile, "⏰ 30 dk sonra"), "callback_data": f"md:{log.id}:snooze"},
        {"text": tb.T(profile, "⏭️ Atla"), "callback_data": f"md:{log.id}:skip"},
    ]])


def run_reminders(now: dt.datetime | None = None) -> int:
    """Zamanı gelen hatırlatmaları gönderir; dünden kalan cevapsızları "missed" yapar. Gönderilen sayısını döner."""
    from . import telegram_bot as tb

    now = now or dt.datetime.now()
    sent = 0
    db = SessionLocal()
    try:
        # Önceki günlerden cevapsız kalanlar kaçırılmış sayılır.
        midnight = dt.datetime.combine(now.date(), dt.time.min)
        for log in db.query(MedicationLog).filter(MedicationLog.status == "pending", MedicationLog.scheduled_for < midnight).all():
            log.status = "missed"
        db.commit()

        meds = db.query(Medication).filter(Medication.status == "active", Medication.reminders == 1).all()
        for med in meds:
            profile = db.get(Profile, med.profile_id)
            if profile is None or not profile.telegram_chat_id or not tb.token_for(profile) or not is_due_day(med, now.date()):
                continue
            for hhmm in times_of(med):
                slot = dt.datetime.combine(now.date(), dt.time.fromisoformat(hhmm))
                if not (slot <= now <= slot + LATE_SEND_WINDOW):
                    continue
                exists = db.query(MedicationLog).filter(MedicationLog.medication_id == med.id, MedicationLog.scheduled_for == slot).first()
                if exists:
                    continue
                log = MedicationLog(medication_id=med.id, profile_id=med.profile_id, scheduled_for=slot)
                db.add(log)
                db.commit()
                log.telegram_message_id = _send_reminder(profile, med, log)
                db.commit()
                sent += 1

        # Ertelenenler: süresi dolunca tekrar gönder.
        for log in db.query(MedicationLog).filter(MedicationLog.status == "pending", MedicationLog.snooze_until.isnot(None),
                                                  MedicationLog.snooze_until <= now).all():
            med = db.get(Medication, log.medication_id)
            profile = db.get(Profile, log.profile_id)
            log.snooze_until = None
            db.commit()
            if med is not None and profile is not None and med.status == "active":
                log.telegram_message_id = _send_reminder(profile, med, log)
                db.commit()
                sent += 1
    finally:
        db.close()
    return sent


def respond(db: Session, log: MedicationLog, action: str) -> str:
    """Hatırlatma butonu: take | skip | snooze. Sonuç: "taken" | "skipped" | "snoozed" | "done"."""
    if log.status != "pending":
        return "done"
    now = dt.datetime.now()
    if action == "take":
        log.status, log.responded_at = "taken", now
    elif action == "skip":
        log.status, log.responded_at = "skipped", now
    elif action == "snooze":
        log.snooze_until = now + SNOOZE
    else:
        return "done"
    db.commit()
    return {"take": "taken", "skip": "skipped", "snooze": "snoozed"}[action]


def adherence(db: Session, med: Medication, days: int = 7) -> tuple[int, int]:
    """(alınan, toplam) — son `days` gündeki, cevabı beklenmeyen hatırlatmalar üzerinden."""
    since = dt.datetime.combine(dt.date.today() - dt.timedelta(days=days - 1), dt.time.min)
    logs = db.query(MedicationLog).filter(MedicationLog.medication_id == med.id, MedicationLog.scheduled_for >= since).all()
    done = [l for l in logs if l.status != "pending"]
    return sum(l.status == "taken" for l in done), len(done)


def today_lines(db: Session, profile: Profile) -> list[str]:
    """Telegram /ilac: bugünün saatleri ve durumları."""
    from . import telegram_bot as tb

    today = dt.date.today()
    icons = {"taken": "✅", "skipped": "⏭️", "missed": "❌", "pending": "⏳"}
    lines = []
    for med in db.query(Medication).filter(Medication.profile_id == profile.id, Medication.status == "active").order_by(Medication.name):
        if not is_due_day(med, today):
            continue
        slots = []
        for hhmm in times_of(med):
            slot = dt.datetime.combine(today, dt.time.fromisoformat(hhmm))
            log = db.query(MedicationLog).filter(MedicationLog.medication_id == med.id, MedicationLog.scheduled_for == slot).first()
            slots.append(f"{hhmm} {icons.get(log.status, '⏳') if log else '·'}")
        lines.append(f"• {label(med)}" + (f": {', '.join(slots)}" if slots else f" ({tb.T(profile, 'hatırlatma saati yok')})"))
    return lines


# --------------------------------------------------------------------------- koç verisi ve değerlendirme

def brief_lines(db: Session, profile: Profile) -> list[str]:
    meds = db.query(Medication).filter(Medication.profile_id == profile.id, Medication.status != "stopped").order_by(Medication.kind, Medication.name).all()
    if not meds:
        return []
    lines = ["## İlaçlar ve Takviyeler (doktorun verdiği ilaçların dozu ve zamanı kullanıcının girdiğidir; değiştirme)"]
    for med in meds:
        bits = [f"{med.name} — {KINDS[med.kind].lower()}", STATUSES[med.status].lower()]
        if med.dose:
            bits.append(med.dose)
        if med.times:
            bits.append("saat " + ", ".join(times_of(med)))
        if med.days:
            bits.append("günler " + days_text(med))
        if med.instructions:
            bits.append(med.instructions)
        taken, total = adherence(db, med)
        if total:
            bits.append(f"son 7 gün uyum {taken}/{total}")
        if med.coach_verdict:
            bits.append(f"önceki değerlendirmen: {VERDICTS[med.coach_verdict].lower()}"
                        + (f", zaman: {med.coach_timing}" if med.coach_timing else ""))
        lines.append("- " + " · ".join(bits))
    lines.append("")
    return lines


_EVAL_PROMPT = """[İlaç ve takviye değerlendirmesi]
Aşağıdaki listedeki her TAKVİYE için (doktorun verdiği ilaçlar için değil) verideki kan tahlili yorumlarına, doktor notlarına, sağlık kayıtlarına ve listedeki diğer ilaçlara bakarak karar ver. Doktor değilsin ve teşhis koymazsın: tahlilde açık bir dayanak yoksa "doktora sor" de; reçeteli ilaçların doz ve zamanını asla değiştirme, sadece takviyeyle zamanlama çakışması varsa (ör. demir–kalsiyum, tiroid ilacı–takviye arası) ZAMAN'da belirt.

Liste:
{items}

Her takviye için cevabında tam olarak şu bloğu yaz (AD listedekiyle birebir aynı):
<<<TAKVİYE
AD: ...
KARAR: gerekli | gereksiz | doktora sor
ZAMAN: ne zaman ve nasıl alınmalı (ör. sabah aç karnına, kalsiyumdan 2 saat ayrı); gereksizse boş
NEDEN: tek cümle, mümkünse dayandığın tahlil değeriyle (ör. ferritin 26 ng/mL, 03.10.2026)
>>>
Blokların dışında en fazla 4 satırlık kısa bir özet yaz."""

_BLOCK_RE = re.compile(r"<<<\s*TAKV[İI]YE\s*\n(.*?)\n?\s*>>>", re.S | re.I)
_FIELD_RE = re.compile(r"^(AD|KARAR|ZAMAN|NEDEN)[ \t]*:[ \t]*(.*)$", re.M | re.I)
_VERDICT_MAP = {"gerekli": "needed", "gereksiz": "not_needed", "doktora sor": "ask_doctor",
                "needed": "needed", "not needed": "not_needed", "ask your doctor": "ask_doctor", "ask doctor": "ask_doctor"}


def parse_evaluation(text: str) -> tuple[str, list[dict]]:
    """(blokların dışındaki özet, [{name, verdict, timing, reason}])"""
    results = []
    for m in _BLOCK_RE.finditer(text or ""):
        f = {k.upper().replace("İ", "I"): v.strip() for k, v in _FIELD_RE.findall(m.group(1))}
        verdict = _VERDICT_MAP.get((f.get("KARAR") or "").lower().strip(" ."))
        if f.get("AD") and verdict:
            results.append({"name": f["AD"], "verdict": verdict, "timing": f.get("ZAMAN") or None, "reason": f.get("NEDEN") or None})
    return _BLOCK_RE.sub("", text or "").strip(), results


def evaluate_in_background(db: Session, profile: Profile) -> None:
    from . import background

    profile.meds_eval_status, profile.meds_eval_summary = "running", None
    db.commit()
    background.start(f"meds-{profile.id}", _evaluate_job, profile.id)


def _evaluate_job(profile_id: str) -> None:
    db = SessionLocal()
    try:
        profile = db.get(Profile, profile_id)
        if profile is None:
            return
        try:
            result = evaluate(db, profile)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Takviye değerlendirmesi başarısız")
            result = {"ok": False, "message": str(exc)[:300]}
        db.refresh(profile)
        profile.meds_eval_status = "done" if result["ok"] else "failed"
        profile.meds_eval_summary = result.get("summary") if result["ok"] else result.get("message")
        profile.meds_eval_at = utcnow()
        db.commit()
    finally:
        db.close()


def evaluate(db: Session, profile: Profile) -> dict:
    """Listedeki takviyeleri tek Claude çağrısıyla değerlendirir. {"ok", "summary", "count", "message"}"""
    from . import coach_chat
    from .routers.coach import build_profile_brief

    lang = i18n.normalize(profile.language)
    meds = db.query(Medication).filter(Medication.profile_id == profile.id, Medication.status != "stopped").all()
    if not any(m.kind == "supplement" for m in meds):
        return {"ok": False, "message": i18n.t(lang, "Değerlendirilecek takviye yok.")}
    if not coach_chat.api_key_configured():
        return {"ok": False, "message": i18n.t(lang, "Claude API anahtarı tanımlı değil.")}
    if coach_chat.month_cost(db) >= settings.coach_monthly_budget_usd:
        return {"ok": False, "message": i18n.t(lang, "Bu ayki Koç bütçesi doldu (${spent} / ${budget}). Bütçeyi .env'de KULVAR_COACH_MONTHLY_BUDGET_USD ile artırabilirsin.",
                                               spent=f"{coach_chat.month_cost(db):.2f}", budget=f"{settings.coach_monthly_budget_usd:.2f}")}
    items = "\n".join(
        f"- {m.name} [{'TAKVİYE' if m.kind == 'supplement' else 'DOKTORUN VERDİĞİ İLAÇ'}; {STATUSES[m.status].lower()}"
        + (f"; doz {m.dose}" if m.dose else "") + (f"; saat {', '.join(times_of(m))}" if m.times else "")
        + (f"; günler {days_text(m)}" if m.days else "")
        + (f"; not: {m.instructions}" if m.instructions else "") + "]"
        for m in meds)
    brief = build_profile_brief(db, profile, include_prompt=False)
    client = anthropic.Anthropic()
    try:
        with client.beta.messages.stream(
            model=settings.coach_model,
            max_tokens=16000,
            system=coach_chat._system_blocks(profile),  # koç talimatları önbellekten okunur
            messages=[{"role": "user", "content": f"Kulvar verisi:\n\n{brief}\n\n{_EVAL_PROMPT.format(items=items)}"}],
            output_config={"effort": settings.coach_effort},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            final = stream.get_final_message()
    except anthropic.APIStatusError as exc:
        return {"ok": False, "message": i18n.t(lang, "Claude API hatası ({code}). Biraz sonra tekrar dene.", code=exc.status_code)}
    except anthropic.APIConnectionError:
        return {"ok": False, "message": i18n.t(lang, "Claude API'ye bağlanılamadı (internet bağlantısını kontrol et).")}

    cost = coach_chat._cost(final.usage, final.model)
    thread = coach_chat.current_thread(db, profile)
    db.add(CoachMessage(  # bütçe ve kullanım tablosuna yansısın; sohbet geçmişine girmez
        thread_id=thread.id, profile_id=profile.id, seq=-1, role="health", content_json="[]",
        model=final.model, stop_reason=final.stop_reason, cost_usd=cost,
        input_tokens=final.usage.input_tokens, output_tokens=final.usage.output_tokens,
        cache_read_tokens=final.usage.cache_read_input_tokens, cache_write_tokens=final.usage.cache_creation_input_tokens,
    ))
    text = "".join(b.text for b in final.content if b.type == "text")
    summary, results = parse_evaluation(text)
    by_name = {m.name.strip().lower(): m for m in meds if m.kind == "supplement"}
    count = 0
    for r in results:
        med = by_name.get(r["name"].strip().lower())
        if med is None:
            continue
        med.coach_verdict, med.coach_timing, med.coach_reason = r["verdict"], r["timing"], r["reason"]
        med.coach_evaluated_at = utcnow()
        count += 1
    db.commit()
    return {"ok": True, "summary": summary, "count": count, "cost_usd": cost}
