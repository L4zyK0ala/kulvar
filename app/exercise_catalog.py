"""Kulvar'ın hareket kataloğu: bilinen hareketler (ExerciseMedia: Hevy şablonları ve daha önce eklenenler)
ile free-exercise-db'nin 876 hareketi (kamu malı, görselli). Hevy olmadan rutin kurabilmek için.

free-exercise-db hareketi rutine ilk eklendiğinde "fedb:<id>" kimliğiyle ExerciseMedia'ya yazılır:
görselleri indirilir ve Garmin kataloğundaki en yakın karşılığıyla eşlenir (saatte adım olarak gitsin).
"""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from . import workout_library
from .clock import utcnow
from .models import ExerciseMedia

FEDB_PREFIX = "fedb:"

_EQUIPMENT = {"body only": "none", "dumbbell": "dumbbell", "barbell": "barbell", "e-z curl bar": "barbell",
              "kettlebells": "kettlebell", "bands": "resistance_band", "cable": "machine", "machine": "machine",
              "medicine ball": "other", "exercise ball": "other", "foam roll": "other", "other": "other"}
_MUSCLE = {"middle back": "middle_back", "lower back": "lower_back"}
_TIMED_WORDS = ("plank", "hold", "wall sit", "isometric", "dead hang", "bridge hold", "hollow")


def is_timed(title: str) -> bool:
    """Süreyle yapılan hareket mi (plank, duruş, asılma …)."""
    t = (title or "").lower()
    return any(w in t for w in _TIMED_WORDS)


def search(db: Session, query: str, limit: int = 20) -> list[dict]:
    """[{key, title, muscle, equipment, frames, known}] — bilinen hareketler önce, sonra free-exercise-db."""
    q = (query or "").strip()
    if len(q) < 2:
        return []
    tokens = workout_library._tokens(q)
    low = q.lower()

    def score(title: str) -> float:
        t = (title or "").lower()
        overlap = len(tokens & workout_library._tokens(title)) / max(1, len(tokens))
        return overlap + (0.5 if low in t else 0) + (0.2 if t.startswith(low) else 0)

    out, seen_fedb = [], set()
    known = []
    for m in db.query(ExerciseMedia).all():
        if m.mapped_by == "none" or not m.title:
            continue
        s = score(m.title)
        if s > 0.3:
            known.append((s, m))
    for s, m in sorted(known, key=lambda x: -x[0])[:limit]:
        out.append({"key": m.template_id, "title": m.title, "muscle": m.primary_muscle, "equipment": m.equipment,
                    "frames": json.loads(m.frames) if m.frames else [], "known": True})
        if m.fedb_id:
            seen_fedb.add(m.fedb_id)
    fedb = [(score(e["name"]), e) for e in workout_library.load_fedb() if e["id"] not in seen_fedb]
    for s, e in sorted((x for x in fedb if x[0] > 0.3), key=lambda x: -x[0])[: max(0, limit - len(out))]:
        out.append({"key": FEDB_PREFIX + e["id"], "title": e["name"], "muscle": (e.get("primaryMuscles") or [None])[0],
                    "equipment": e.get("equipment"), "frames": [], "known": False})
    return out[:limit]


def ensure(db: Session, key: str) -> ExerciseMedia | None:
    """Kataloğun hareketi için ExerciseMedia (yoksa free-exercise-db'den oluşturur: görseller + Garmin eşlemesi)."""
    media = db.get(ExerciseMedia, key)
    if media is not None or not key.startswith(FEDB_PREFIX):
        return media
    item = next((e for e in workout_library.load_fedb() if e["id"] == key[len(FEDB_PREFIX):]), None)
    if item is None:
        return None
    equipment = _EQUIPMENT.get(item.get("equipment") or "", "other")
    muscle = (item.get("primaryMuscles") or [None])[0]
    media = ExerciseMedia(template_id=key, title=item["name"], primary_muscle=_MUSCLE.get(muscle, muscle), equipment=equipment)
    garmin = workout_library.garmin_candidates(item["name"], equipment)
    workout_library._apply(media, item, garmin[0][1] if garmin and garmin[0][0] >= workout_library._FALLBACK_OK else None, "auto")
    media.updated_at = utcnow()
    db.add(media)
    db.flush()
    return media
