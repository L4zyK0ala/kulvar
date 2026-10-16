"""Hevy hareketlerinin animasyon ve Garmin eşlemesini yeniden yapar.

Varsayılan: kelime eşleşmesiyle kesin olmayan otomatik eşlemeleri sıfırlar ve Claude'a bir kez
toplu sorar. --all ile tüm eşlemeler (kardiyo/ısınma hariç), --only ile adında verilen
metin geçen hareketler sıfırlanır.

    venv/bin/python scripts/remap_exercises.py [--all | --only "Chest Fly" "Push Up"] [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import coach_chat, workout_library as wl  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import ExerciseMedia  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--all", action="store_true", help="kesin eşlemeler dahil hepsini sıfırla")
    ap.add_argument("--only", nargs="+", metavar="AD", help="sadece adında bu metinlerden biri geçen hareketler")
    ap.add_argument("--dry-run", action="store_true", help="sadece neyin sıfırlanacağını göster")
    args = ap.parse_args()
    if not coach_chat.api_key_configured() and not args.dry_run:
        sys.exit("ANTHROPIC_API_KEY tanımlı değil (.env) — Claude olmadan yeniden eşleme aynı sonucu verir.")

    db = SessionLocal()
    try:
        reset = []
        for m in db.query(ExerciseMedia).filter(ExerciseMedia.mapped_by != "none").order_by(ExerciseMedia.title):
            fc = wl.fedb_candidates(m.title, m.equipment)
            gc = wl.garmin_candidates(m.title, m.equipment)
            sure = fc and gc and fc[0][0] >= wl._AUTO_OK and gc[0][0] >= wl._AUTO_OK
            if args.only:
                hit = any(o.lower() in m.title.lower() for o in args.only)
            else:
                hit = args.all or (m.mapped_by == "auto" and not sure)
            if hit:
                reset.append(m)
        print(f"Sıfırlanacak {len(reset)} hareket:")
        for m in reset:
            print(f"  - {m.title}  (şu an: {m.fedb_id or '—'} | {m.garmin_name or '—'})")
        if args.dry_run or not reset:
            return
        for m in reset:
            m.mapped_by = m.fedb_id = m.frames = m.instructions = m.garmin_category = m.garmin_name = None
        db.commit()
        wl.map_pending(db)
        print("\nYeni eşlemeler:")
        for m in reset:
            db.refresh(m)
            print(f"  {'✓' if m.frames and m.frames != '[]' else '·'} {m.title} → {m.fedb_id or '—'} | {m.garmin_name or '—'} ({m.mapped_by})")
    finally:
        db.close()


if __name__ == "__main__":
    main()
