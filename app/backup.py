"""Gecelik yedek: veritabanı + sağlık belgeleri + profil resimleri tek bir .tar.gz arşivde.

Veritabanı SQLite'ın çevrimiçi yedek API'siyle kopyalanır (servis çalışırken güvenli), kopyanın
bütünlüğü doğrulanır, sonra arşivlenir. Hareket animasyonları yedeğe girmez (yeniden indirilir).

Saklama: son 14 günün her yedeği + 8 haftaya kadar her haftanın ilk yedeği; en yeni 3 yedek her
koşulda kalır. KULVAR_BACKUP_COPY_DIR verilirse (ör. başka bir disk) arşiv oraya da kopyalanır.

Not: Şifreli hesap bilgileri .env'deki KULVAR_ENCRYPTION_KEY ile açılır; anahtar yedeğe konmaz,
ayrıca (ör. parola yöneticisinde) saklanmalı.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import logging
import shutil
import sqlite3
import tarfile
import tempfile
from pathlib import Path

from sqlalchemy.engine import make_url

from .config import env, settings

logger = logging.getLogger("kulvar.backup")

APP_DIR = Path(__file__).resolve().parent
HEALTH_DIR = APP_DIR / "data" / "health"
AVATAR_DIR = APP_DIR / "static" / "uploads" / "avatars"
PREFIX = "kulvar-"
SUFFIX = ".tar.gz"
KEEP_DAYS = int(env("BACKUP_KEEP_DAYS", "14"))
KEEP_WEEKS = int(env("BACKUP_KEEP_WEEKS", "8"))
KEEP_MIN = 3
BACKUP_HOUR = int(env("BACKUP_HOUR", "3"))


def db_path() -> Path | None:
    url = make_url(settings.database_url)
    return Path(url.database).resolve() if url.drivername.startswith("sqlite") and url.database else None


def backup_dir() -> Path:
    """Varsayılan: veritabanının yanındaki backups/ (Docker'da /data/backups, yani kalıcı birimde)."""
    configured = env("BACKUP_DIR")
    if configured:
        return Path(configured)
    db = db_path()
    return (db.parent if db else APP_DIR.parent) / "backups"


def copy_dir() -> Path | None:
    configured = env("BACKUP_COPY_DIR")
    return Path(configured) if configured else None


def _status_file() -> Path:
    return backup_dir() / "status.json"


def read_status() -> dict:
    try:
        return json.loads(_status_file().read_text())
    except (OSError, ValueError):
        return {}


def _write_status(**changes) -> dict:
    status = read_status()
    status.update(changes)
    try:
        _status_file().parent.mkdir(parents=True, exist_ok=True)
        _status_file().write_text(json.dumps(status, ensure_ascii=False, indent=1))
    except OSError:
        logger.exception("Yedek durum dosyası yazılamadı")
    return status


def archives(directory: Path | None = None) -> list[Path]:
    directory = directory or backup_dir()
    if not directory.is_dir():
        return []
    return sorted((p for p in directory.glob(f"{PREFIX}*{SUFFIX}") if p.is_file()), key=lambda p: p.name, reverse=True)


def _stamp(path: Path) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(path.name[len(PREFIX):-len(SUFFIX)], "%Y%m%d-%H%M%S")
    except ValueError:
        return None


def summary() -> dict:
    """Ayarlar sayfası için: son yedek, sayı, toplam boyut, konum ve son hata."""
    files = archives()
    status = read_status()
    latest = files[0] if files else None
    return {
        "dir": str(backup_dir()),
        "copy_dir": str(copy_dir()) if copy_dir() else None,
        "count": len(files),
        "total_mb": sum(p.stat().st_size for p in files) / 1_048_576,
        "latest_name": latest.name if latest else None,
        "latest_at": _stamp(latest) if latest else None,
        "latest_mb": latest.stat().st_size / 1_048_576 if latest else None,
        "error": status.get("error"),
        "error_at": status.get("error_at"),
        "copy_error": status.get("copy_error"),
        "hour": BACKUP_HOUR,
    }


def is_stale(hours: int = 26) -> bool:
    files = archives()
    stamp = _stamp(files[0]) if files else None
    return stamp is None or dt.datetime.now() - stamp > dt.timedelta(hours=hours)


def run_backup() -> Path:
    """Yedeği alır ve eskileri temizler. Hata olursa durum dosyasına yazar, Telegram'a bildirir ve yeniden fırlatır."""
    try:
        path = _create_archive()
    except Exception as exc:
        logger.exception("Yedek alınamadı")
        _write_status(error=str(exc)[:300], error_at=dt.datetime.now().isoformat(timespec="minutes"))
        _notify(failed=True, message=str(exc)[:300])
        raise
    copy_error = None
    if copy_dir() is not None:
        try:
            copy_dir().mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, copy_dir() / path.name)
            prune(copy_dir())
        except OSError as exc:
            logger.exception("Yedek ikinci konuma kopyalanamadı")
            copy_error = f"{copy_dir()}: {exc}"[:300]
    prune(backup_dir())
    before = read_status()
    _write_status(error=None, error_at=None, copy_error=copy_error, last_ok=dt.datetime.now().isoformat(timespec="minutes"))
    if copy_error and not before.get("copy_error"):  # aynı kopyalama hatası her gece tekrar bildirilmesin
        _notify(failed=True, message=copy_error)
    elif not copy_error and (before.get("error") or before.get("copy_error")):
        _notify(failed=False, message=None)
    logger.info("Yedek alındı: %s (%.1f MB)", path, path.stat().st_size / 1_048_576)
    return path


def _create_archive() -> Path:
    db = db_path()
    if db is None or not db.exists():
        raise RuntimeError("SQLite veritabanı bulunamadı; yedek sadece SQLite için çalışır.")
    target_dir = backup_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    name = f"{PREFIX}{dt.datetime.now():%Y%m%d-%H%M%S}{SUFFIX}"
    with tempfile.TemporaryDirectory(dir=target_dir, prefix=".tmp-") as tmp:
        snapshot = Path(tmp) / "kulvar.db"
        src = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
        dst = sqlite3.connect(snapshot)
        try:
            src.backup(dst)  # çevrimiçi yedek: yazmalar sürerken tutarlı bir kopya
        finally:
            src.close()
        try:
            check = dst.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            dst.close()
        if check != "ok":
            raise RuntimeError(f"Yedek kopyası bütünlük kontrolünden geçmedi: {check[:200]}")
        partial = Path(tmp) / name
        with tarfile.open(partial, "w:gz") as tar:
            tar.add(snapshot, arcname="kulvar.db")
            if HEALTH_DIR.is_dir():
                tar.add(HEALTH_DIR, arcname="data/health")
            if AVATAR_DIR.is_dir():
                tar.add(AVATAR_DIR, arcname="uploads/avatars")
            info = tarfile.TarInfo("BACKUP_INFO.json")
            payload = json.dumps({"created_at": dt.datetime.now().isoformat(timespec="seconds"), "source_db": str(db),
                                  "restore": "scripts/restore_backup.py"}, indent=1).encode()
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        final = target_dir / name
        partial.replace(final)  # yarım arşiv hiçbir zaman yedek gibi görünmesin
    final.chmod(0o600)  # sağlık verisi içerir
    return final


def prune(directory: Path, now: dt.datetime | None = None) -> list[Path]:
    """Saklama kuralı dışında kalan arşivleri siler; silinenleri döner."""
    now = now or dt.datetime.now()
    files = [(p, _stamp(p)) for p in archives(directory)]
    files = [(p, s) for p, s in files if s is not None]
    keep, seen_weeks = set(), set()
    for i, (p, s) in enumerate(files):  # en yeniden eskiye; her haftadan bir (en yeni) arşiv kalır
        age = now - s
        week = s.isocalendar()[:2]
        if i < KEEP_MIN or age <= dt.timedelta(days=KEEP_DAYS):
            keep.add(p)
        elif age <= dt.timedelta(weeks=KEEP_WEEKS) and week not in seen_weeks:
            keep.add(p)
        seen_weeks.add(week)
    removed = [p for p, _ in files if p not in keep]
    for p in removed:
        try:
            p.unlink()
        except OSError:
            logger.exception("Eski yedek silinemedi: %s", p)
    return removed


def _notify(failed: bool, message: str | None) -> None:
    from . import alerts

    try:
        alerts.backup_event(failed, message)
    except Exception:  # noqa: BLE001 - bildirim hatası yedeği bozmasın
        logger.exception("Yedek bildirimi gönderilemedi")
