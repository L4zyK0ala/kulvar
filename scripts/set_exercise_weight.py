"""Hevy rutinlerinde bir hareketin çalışma setlerinin ağırlığını değiştirir (ör. row 5 → 10 kg).

Rutinin Hevy'deki güncel hali okunur, yalnızca adı eşleşen hareketin normal setlerinin ağırlığı
değişir; sonra rutin Hevy'ye geri yazılır ve Kulvar'daki kopyası yenilenir.

    venv/bin/python scripts/set_exercise_weight.py --routine "Ev D3" --exercise row --kg 10 [--profile Deniz] [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import routine_builder as rb, workout_library as wl  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Profile, StrengthRoutine  # noqa: E402

_SET_FIELDS = ("type", "weight_kg", "reps", "distance_meters", "duration_seconds", "custom_metric", "rep_range")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--routine", required=True, help="rutin adının başı, ör. \"Ev D3\"")
    ap.add_argument("--exercise", required=True, help="hareket adında geçen metin, ör. row")
    ap.add_argument("--kg", type=float, required=True)
    ap.add_argument("--profile", help="sadece bu profil (yoksa hepsi)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        profiles = db.query(Profile).all()
        for profile in profiles:
            if args.profile and profile.name.lower() != args.profile.lower():
                continue
            key = rb.hevy_key(db, profile.id)
            if not key:
                continue
            changed = False
            for routine in db.query(StrengthRoutine).filter(StrengthRoutine.profile_id == profile.id).all():
                if wl._routine_key(routine.title) != args.routine.lower().strip():
                    continue
                data = wl._hevy_get(key, f"/routines/{routine.hevy_id}")
                data = data.get("routine", data)
                data = data[0] if isinstance(data, list) else data
                hits = []
                for e in data["exercises"]:
                    if args.exercise.lower() in (e.get("title") or "").lower():
                        for s in e["sets"]:
                            if s.get("type") in (None, "normal") and s.get("weight_kg") != args.kg:
                                hits.append(f"{e['title']}: {s.get('weight_kg')} → {args.kg:g} kg")
                                s["weight_kg"] = args.kg
                print(f"{profile.name} · {data['title']}: " + ("; ".join(hits) if hits else "değişiklik yok"))
                if not hits or args.dry_run:
                    continue
                body = {"routine": {
                    "title": data["title"], "notes": data.get("notes") or "",
                    "exercises": [{
                        "exercise_template_id": e["exercise_template_id"], "superset_id": e.get("superset_id"),
                        "rest_seconds": e.get("rest_seconds"), "notes": e.get("notes"),
                        "sets": [{k: s.get(k) for k in _SET_FIELDS if k in s} for s in e["sets"]],
                    } for e in data["exercises"]],
                }}
                rb._hevy_send("PUT", key, f"/routines/{routine.hevy_id}", body)
                changed = True
            if changed:
                wl.refresh_routines(db, profile, key)
                print(f"{profile.name}: Hevy'ye yazıldı, Kulvar'daki rutinler yenilendi.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
