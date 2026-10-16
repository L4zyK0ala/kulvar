"""Bir Kulvar yedek arşivini geri yükler (veritabanı + sağlık belgeleri + profil resimleri).

Önce servisi durdur (ör. sudo systemctl stop kulvar.service ya da docker compose stop), sonra:

    venv/bin/python scripts/restore_backup.py backups/kulvar-20261013-030000.tar.gz          # ne yapılacağını gösterir
    venv/bin/python scripts/restore_backup.py backups/kulvar-20261013-030000.tar.gz --yes    # geri yükler

Mevcut veritabanı silinmez, yanına kulvar.db.before-restore-<zaman> olarak taşınır; sağlık belgeleri
ve profil resimleri arşivdekilerle birleştirilir (aynı adlı dosyalar arşivdeki sürümle değişir).
Hesap bilgileri .env'deki KULVAR_ENCRYPTION_KEY ile açılır; yedeğin alındığı kurulumla aynı anahtar gerekir.
"""
from __future__ import annotations

import argparse
import datetime as dt
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import backup  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="Kulvar yedeğini geri yükler.")
    ap.add_argument("archive", type=Path)
    ap.add_argument("--yes", action="store_true", help="gerçekten geri yükle (yoksa sadece kontrol eder)")
    args = ap.parse_args()

    db = backup.db_path()
    if db is None:
        sys.exit("Geri yükleme sadece SQLite veritabanı için çalışır.")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        with tarfile.open(args.archive, "r:gz") as tar:
            tar.extractall(tmp, filter="data")  # arşiv dışına yazan / bağlantı içeren üyeler reddedilir
        snapshot = tmp / "kulvar.db"
        if not snapshot.exists():
            sys.exit("Arşivde kulvar.db yok; bu bir Kulvar yedeği değil.")
        con = sqlite3.connect(snapshot)
        try:
            check = con.execute("PRAGMA integrity_check").fetchone()[0]
            profiles = con.execute("SELECT count(*) FROM profiles").fetchone()[0]
        finally:
            con.close()
        if check != "ok":
            sys.exit(f"Arşivdeki veritabanı bozuk: {check}")
        health = sorted(p for p in (tmp / "data" / "health").rglob("*") if p.is_file()) if (tmp / "data" / "health").exists() else []
        avatars = sorted(p for p in (tmp / "uploads" / "avatars").rglob("*") if p.is_file()) if (tmp / "uploads" / "avatars").exists() else []
        print(f"Arşiv: {args.archive.name} · {profiles} profil · {len(health)} sağlık belgesi · {len(avatars)} profil resmi")
        print(f"Hedef veritabanı: {db}")
        if not args.yes:
            print("\nKontrol tamam. Geri yüklemek için servisi durdurup --yes ile tekrar çalıştır.")
            return

        if db.exists():
            kept = db.with_name(f"{db.name}.before-restore-{dt.datetime.now():%Y%m%d-%H%M%S}")
            db.replace(kept)
            print(f"Mevcut veritabanı taşındı: {kept.name}")
        for extra in (db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm"), db.with_name(db.name + "-journal")):
            if extra.exists():
                extra.unlink()
        shutil.copy2(snapshot, db)
        for src_root, dst_root, files in ((tmp / "data" / "health", backup.HEALTH_DIR, health),
                                          (tmp / "uploads" / "avatars", backup.AVATAR_DIR, avatars)):
            for f in files:
                dest = dst_root / f.relative_to(src_root)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dest)
        print("Geri yüklendi. Servisi başlatabilirsin.")


if __name__ == "__main__":
    main()
