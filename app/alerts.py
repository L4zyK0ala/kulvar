"""Telegram uyarıları: uzun süredir başarısız ya da hiç tamamlanmayan senkronlar ve yedek hataları.

Her kaynak için tek uyarı gider (KULVAR_SYNC_ALERT_HOURS, varsayılan 6 saat sonra); kaynak tekrar
çalışınca "düzeldi" mesajı gelir. Uyarı o profilin Telegram'ına gider; profilin Telegram'ı yoksa
evin ilk profiline (yönetici). Yedek uyarıları da yöneticiye gider.
"""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy.orm import Session

from .clock import utcnow
from . import i18n
from .config import env, settings
from .db import SessionLocal
from .integrations import SOURCE_LABELS
from .models import Credential, Profile

logger = logging.getLogger("kulvar.alerts")

ALERT_HOURS = int(env("SYNC_ALERT_HOURS", "6"))


def T(profile: Profile, text: str, **kw) -> str:
    return i18n.t(i18n.normalize(profile.language), text, **kw)


def _code(text: str | None) -> str:
    """Hata metnini Telegram'da tek satırlık kod olarak gösterir."""
    text = " ".join((text or "?").replace("`", "'").split())
    return f"`{text[:240]}`"


def admin_profile(db: Session) -> Profile | None:
    """Telegram'ı bağlı ilk profil (hanenin yöneticisi sayılır)."""
    from . import telegram_bot

    for p in db.query(Profile).order_by(Profile.created_at).all():
        if p.telegram_chat_id and telegram_bot.token_for(p):
            return p
    return None


def _send(db: Session, profile: Profile, text: str) -> bool:
    """Profilin Telegram'ına; yoksa yöneticiye (profil adıyla) gönderir. Gönderildiyse True."""
    from . import telegram_bot

    target = profile
    if not (profile.telegram_chat_id and telegram_bot.token_for(profile)):
        target = admin_profile(db)
        if target is None:
            return False
        text = f"**{profile.name}** · {text}"
    return telegram_bot.send_to_profile(target, text) is not None


def _hours(since: dt.datetime, now: dt.datetime) -> int:
    return max(1, int((now - since).total_seconds() // 3600))


def sync_result(db: Session, credential: Credential, now: dt.datetime | None = None) -> None:
    """Her senkrondan sonra çağrılır (commit'ten önce): başarısızlık süresini izler, gerekirse uyarır."""
    now = now or utcnow()
    if credential.last_sync_status == "error":
        credential.failing_since = credential.failing_since or now
        _maybe_alert(db, credential, now, credential.last_sync_message)
        return
    if credential.alert_sent_at is not None:
        profile = db.get(Profile, credential.profile_id)
        if profile is not None:
            _send(db, profile, T(profile, "✅ **{source}** senkronu yeniden çalışıyor.",
                                  source=SOURCE_LABELS.get(credential.source, credential.source)))
    credential.failing_since = None
    credential.alert_sent_at = None


def _maybe_alert(db: Session, credential: Credential, now: dt.datetime, error: str | None, stale: bool = False) -> None:
    if credential.alert_sent_at is not None or credential.failing_since is None:
        return
    if now - credential.failing_since < dt.timedelta(hours=ALERT_HOURS):
        return
    profile = db.get(Profile, credential.profile_id)
    if profile is None:
        return
    source = SOURCE_LABELS.get(credential.source, credential.source)
    hours = _hours(credential.failing_since, now)
    if stale:
        head = T(profile, "⚠️ **{source}** senkronu {hours} saattir tamamlanmadı (askıda kalıyor olabilir).", source=source, hours=hours)
    else:
        head = T(profile, "⚠️ **{source}** senkronu {hours} saattir başarısız.", source=source, hours=hours)
    text = (head + ("\n" + T(profile, "Son hata") + ": " + _code(error) if error else "") + "\n\n"
            + T(profile, "Ayarlar'dan bağlantıyı kontrol et; düzelince haber vereceğim."))
    if _send(db, profile, text):
        credential.alert_sent_at = now


def check_stale(now: dt.datetime | None = None) -> None:
    """Saatlik: zaman aşımına uğrayıp durumu hiç güncellenmeyen senkronları da yakalar."""
    now = now or utcnow()
    limit = dt.timedelta(hours=ALERT_HOURS, minutes=2 * settings.sync_interval_minutes)
    db = SessionLocal()
    try:
        for c in db.query(Credential).filter(Credential.last_synced_at.isnot(None)).all():
            if now - c.last_synced_at <= limit:
                continue
            c.failing_since = c.failing_since or c.last_synced_at
            _maybe_alert(db, c, now, c.last_sync_message if c.last_sync_status == "error" else None, stale=True)
        db.commit()
    finally:
        db.close()


def backup_event(failed: bool, message: str | None) -> None:
    """Yedek alınamadıysa (ya da ikinci konuma kopyalanamadıysa) uyarır; ilk başarılı yedekte düzeldi der."""
    db = SessionLocal()
    try:
        profile = admin_profile(db)
        if profile is None:
            return
        if failed:
            text = T(profile, "⚠️ Kulvar yedeği alınamadı.") + "\n" + _code(message) + "\n\n" + T(profile, "Ayarlar → Yedekleme'den tekrar deneyebilirsin.")
        else:
            text = T(profile, "✅ Kulvar yedeği yeniden alınıyor.")
        _send(db, profile, text)
    finally:
        db.close()
