"""Gecelik yedek, saklama kuralı, geri yükleme ve Telegram uyarıları."""
import datetime as dt
import importlib.util
import sqlite3
import sys
import tarfile
from pathlib import Path

import pytest

from app import alerts, backup
from app.clock import utcnow
from app.models import Credential, Profile

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    """Sağlık belgeleri / profil resimleri ve yedek klasörü geçici dizinlerde."""
    health, avatars, out = tmp_path / "health", tmp_path / "avatars", tmp_path / "backups"
    (health / "p1").mkdir(parents=True)
    (health / "p1" / "tahlil.pdf").write_bytes(b"%PDF-1.4 test")
    avatars.mkdir()
    (avatars / "p1.jpg").write_bytes(b"jpg")
    monkeypatch.setattr(backup, "HEALTH_DIR", health)
    monkeypatch.setattr(backup, "AVATAR_DIR", avatars)
    monkeypatch.setenv("KULVAR_BACKUP_DIR", str(out))
    return tmp_path


def test_backup_archive_contents(db, profile, dirs, telegram):
    path = backup.run_backup()
    assert path.parent == dirs / "backups" and oct(path.stat().st_mode)[-3:] == "600"
    names = tarfile.open(path).getnames()
    assert {"kulvar.db", "data/health/p1/tahlil.pdf", "uploads/avatars/p1.jpg", "BACKUP_INFO.json"} <= set(names)
    assert not backup.is_stale()
    assert backup.summary()["count"] == 1 and backup.read_status().get("error") is None
    assert telegram == []  # başarılı yedek sessizdir


def test_backup_failure_notifies_once_then_recovers(db, profile, dirs, telegram, monkeypatch):
    profile.telegram_chat_id = "chat"
    db.commit()
    real = backup._create_archive
    monkeypatch.setattr(backup, "_create_archive", lambda: (_ for _ in ()).throw(RuntimeError("disk dolu")))
    with pytest.raises(RuntimeError):
        backup.run_backup()
    assert backup.summary()["error"] == "disk dolu"
    assert "disk dolu" in telegram[-1][1]
    monkeypatch.setattr(backup, "_create_archive", real)
    backup.run_backup()
    assert backup.summary()["error"] is None
    assert telegram[-1][1].startswith("✅")


def test_prune_keeps_two_weeks_and_weeklies(tmp_path):
    now = dt.datetime(2026, 10, 13, 3, 0)
    for d in range(70):
        (tmp_path / f"kulvar-{now - dt.timedelta(days=d):%Y%m%d-%H%M%S}.tar.gz").write_text("x")
    backup.prune(tmp_path, now)
    left = sorted(p.name[7:15] for p in backup.archives(tmp_path))
    daily = [x for x in left if x >= "20260929"]
    weekly = [x for x in left if x < "20260929"]
    assert len(daily) == 15  # bugün dahil son 14 gün
    assert len(weekly) == len({dt.datetime.strptime(x, "%Y%m%d").isocalendar()[:2] for x in weekly})  # haftada bir
    assert min(left) >= f"{now - dt.timedelta(weeks=backup.KEEP_WEEKS):%Y%m%d}"


def test_prune_always_keeps_newest_three(tmp_path):
    now = dt.datetime(2026, 10, 13)
    for d in (100, 200, 300, 400):
        (tmp_path / f"kulvar-{now - dt.timedelta(days=d):%Y%m%d-%H%M%S}.tar.gz").write_text("x")
    backup.prune(tmp_path, now)
    assert len(backup.archives(tmp_path)) == 3


def test_restore_roundtrip(db, profile, dirs, monkeypatch, tmp_path):
    archive = backup.run_backup()
    profile.name = "Değişti"
    db.commit()
    spec = importlib.util.spec_from_file_location("restore_backup", ROOT / "scripts" / "restore_backup.py")
    restore = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(restore)
    restored_health = tmp_path / "restored-health"
    monkeypatch.setattr(restore.backup, "HEALTH_DIR", restored_health)
    from app.db import engine
    engine.dispose()
    monkeypatch.setattr(sys, "argv", ["restore", str(archive), "--yes"])
    restore.main()
    con = sqlite3.connect(backup.db_path())
    assert con.execute("SELECT name FROM profiles").fetchone()[0] == "Deniz"
    assert (restored_health / "p1" / "tahlil.pdf").read_bytes() == b"%PDF-1.4 test"
    assert list(backup.db_path().parent.glob("*.before-restore-*"))  # eski veritabanı silinmez


# --------------------------------------------------------------------------- senkron uyarıları

def _credential(db, profile, source="garmin"):
    c = Credential(profile_id=profile.id, source=source, encrypted_payload="x")
    db.add(c)
    db.commit()
    return c


def test_sync_alert_once_after_threshold_then_recovery(db, profile, telegram):
    profile.telegram_chat_id = "chat"
    c = _credential(db, profile)
    t0 = dt.datetime(2026, 10, 13, 8)
    for hours in (0, 3, alerts.ALERT_HOURS - 0.1, alerts.ALERT_HOURS + 0.1, alerts.ALERT_HOURS + 3):
        c.last_sync_status, c.last_sync_message = "error", "401 Unauthorized"
        alerts.sync_result(db, c, t0 + dt.timedelta(hours=hours))
    assert len(telegram) == 1 and "401 Unauthorized" in telegram[0][1]
    c.last_sync_status = "ok"
    alerts.sync_result(db, c, t0 + dt.timedelta(hours=10))
    assert len(telegram) == 2 and telegram[1][1].startswith("✅")
    assert c.failing_since is None and c.alert_sent_at is None


def test_short_outage_is_silent(db, profile, telegram):
    profile.telegram_chat_id = "chat"
    c = _credential(db, profile)
    t0 = dt.datetime(2026, 10, 13, 8)
    c.last_sync_status = "error"
    alerts.sync_result(db, c, t0)
    c.last_sync_status = "ok"
    alerts.sync_result(db, c, t0 + dt.timedelta(hours=1))
    assert telegram == []


def test_alert_falls_back_to_admin(db, profile, telegram):
    profile.telegram_chat_id = "chat"  # ilk profil = yönetici
    other = Profile(name="Ece", language="tr")
    db.add(other)
    db.commit()
    c = _credential(db, other, "hevy")
    t0 = dt.datetime(2026, 10, 13, 8)
    c.last_sync_status = "error"
    alerts.sync_result(db, c, t0)
    alerts.sync_result(db, c, t0 + dt.timedelta(hours=alerts.ALERT_HOURS + 1))
    assert telegram[0][0] == "Deniz" and telegram[0][1].startswith("**Ece**")


def test_stale_sync_detected(db, profile, telegram):
    profile.telegram_chat_id = "chat"
    c = _credential(db, profile, "mfp")
    c.last_sync_status, c.last_synced_at = "ok", utcnow() - dt.timedelta(hours=alerts.ALERT_HOURS + 6)
    db.commit()
    alerts.check_stale()
    alerts.check_stale()
    assert len(telegram) == 1 and "askıda" in telegram[0][1] and "Son hata" not in telegram[0][1]


def test_alert_in_english(db, profile, telegram):
    profile.telegram_chat_id, profile.language = "chat", "en"
    c = _credential(db, profile)
    t0 = dt.datetime(2026, 10, 13, 8)
    c.last_sync_status = "error"
    alerts.sync_result(db, c, t0)
    alerts.sync_result(db, c, t0 + dt.timedelta(hours=alerts.ALERT_HOURS + 1))
    assert "sync has been failing" in telegram[0][1]
