"""Hevy + Garmin → Strava'da tek kuvvet kaydı.

Hevy'nin Strava yüklemesi kapalıyken kuvvet antrenmanı Strava'ya yalnız Garmin'den gelir (nabız,
kalori, süre). Burada aynı antrenmanın Hevy kaydı bulunur ve Strava'daki o kaydın başlığı rutin
adıyla, açıklaması hareket/set listesiyle güncellenir. Strava senkronunun sonunda çalışır;
"activity:write" izni gerekir.

Dokunulmayanlar: Hevy'nin kendi yüklediği Strava kaydı ve kullanıcının elle yazdığı açıklama
(Kulvar'ın imzasını taşımayan dolu açıklama). Aynı içerik bir kez yazılır; Hevy'de antrenman
düzenlenirse yeni içerik tekrar yazılır.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging

from sqlalchemy.orm import Session

from . import i18n, strength_log, workout_library
from .clock import utcnow
from .models import Activity, Profile, StravaEnrichment

logger = logging.getLogger("kulvar.strava_merge")

STRENGTH_TYPES = {"WeightTraining", "Workout", "Crossfit"}
MATCH_TOLERANCE = dt.timedelta(minutes=20)
LOOKBACK = dt.timedelta(days=3)
SIGNATURE = "Kulvar"  # açıklamanın son satırında; elle yazılmış açıklamayla ayırt etmek için


def _end(a: Activity) -> dt.datetime:
    return a.start_time + dt.timedelta(seconds=a.duration_s or 0)


def match_hevy(strava: Activity, hevy_rows: list[Activity]) -> Activity | None:
    """Aynı antrenmanın Hevy kaydı: zaman aralıkları çakışan ya da başlangıcı 20 dk içinde olan, en yakını."""
    candidates = [h for h in hevy_rows
                  if (h.start_time < _end(strava) and strava.start_time < _end(h))
                  or abs(h.start_time - strava.start_time) <= MATCH_TOLERANCE]
    return min(candidates, key=lambda h: abs(h.start_time - strava.start_time), default=None)


def describe(hevy: Activity, lang: str) -> tuple[str, str]:
    """(başlık, açıklama) — Hevy antrenmanının hareketleri, setleri ve toplam hacmi."""
    data = json.loads(hevy.raw_json or "{}")
    return describe_exercises(data.get("title") or hevy.activity_type or "Hevy", data.get("exercises") or [], lang, "Hevy + Garmin")


def describe_exercises(title: str, exercises: list[dict], lang: str, origin: str) -> tuple[str, str]:
    title = title[:100]
    lines, sets_total, volume = [], 0, 0.0
    for e in exercises:
        sets = e.get("sets") or []
        summary = workout_library._sets_summary(sets, lang)
        if not summary:
            continue
        lines.append(f"• {e.get('title') or '?'}: {summary}")
        if workout_library._is_cardio_or_warmup(e.get("title") or ""):
            continue  # ısınma/kardiyo set sayısına ve hacme girmez
        for s in sets:
            if s.get("type") == "warmup":
                continue
            sets_total += 1
            volume += (s.get("weight_kg") or 0) * (s.get("reps") or 0)
    total = i18n.t(lang, "{n} set", n=sets_total)
    if volume:
        total += " · " + i18n.t(lang, "{kg} kg toplam hacim", kg=i18n.number(lang, volume))
    lines += ["", total, f"— {origin} · {SIGNATURE}"]
    return title, "\n".join(lines)


def _digest(name: str, description: str) -> str:
    return hashlib.sha1(f"{name}\n{description}".encode()).hexdigest()


def enrich_recent(db: Session, profile: Profile, client, now: dt.datetime | None = None) -> int:
    """Son 3 günün Strava kuvvet kayıtlarını eşleşen Hevy antrenmanıyla günceller; güncellenen sayısını döner."""
    now = now or dt.datetime.now()
    since = now - LOOKBACK
    lang = i18n.normalize(profile.language)
    rows = (db.query(Activity).filter(Activity.profile_id == profile.id, Activity.start_time >= since - MATCH_TOLERANCE,
                                      Activity.source.in_(["strava", "hevy"])).all())
    hevy_rows = [a for a in rows if a.source == "hevy"]
    updated = 0
    for strava in (a for a in rows if a.source == "strava" and a.activity_type in STRENGTH_TYPES and a.start_time >= since):
        # Doğru değerler: Kulvar'da onaylanan kayıt; yoksa Hevy'deki antrenman.
        native = strength_log.confirmed_near(db, profile.id, strava.start_time)
        if native is not None and native.source == "kulvar":
            ref = f"kulvar:{native.id}"
            name, description = describe_exercises(native.title, strength_log.exercises_of(native), lang, "Garmin")
        else:
            hevy = match_hevy(strava, hevy_rows)
            if hevy is None:
                continue
            ref = hevy.external_id
            name, description = describe(hevy, lang)
        digest = _digest(name, description)
        record = db.get(StravaEnrichment, strava.external_id)
        if record is not None and (record.status != "written" or record.digest == digest):
            continue  # zaten yazıldı ya da dokunulmayacak bir kayıt
        detail = client.get_activity(int(strava.external_id))
        if record is None:
            record = StravaEnrichment(strava_id=strava.external_id, profile_id=profile.id)
            db.add(record)
        record.hevy_id, record.updated_at = ref, utcnow()
        if (getattr(detail, "external_id", None) or "").lower().startswith("hevy"):
            record.status = "hevy_copy"  # Hevy'nin kendi yüklemesi: zaten hareketleri içeriyor
            continue
        current = (getattr(detail, "description", None) or "").strip()
        if current and SIGNATURE not in current:
            record.status = "user_text"  # kullanıcının yazdığı açıklama ezilmez
            continue
        client.update_activity(int(strava.external_id), name=name, description=description)
        record.status, record.digest = "written", digest
        updated += 1
        logger.info("Strava %s, %s ile güncellendi (%s)", strava.external_id, ref, profile.name)
    db.commit()
    return updated
