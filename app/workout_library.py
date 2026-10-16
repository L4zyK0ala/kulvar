"""Kuvvet günleri: Hevy rutinleri, hareket görselleri/animasyonları ve Garmin hareket adımları.

- Rutinler (D1, D2 …) Hevy API'den günde bir çekilir ve StrengthRoutine olarak saklanır.
- Her hareket bir kez eşlenir (ExerciseMedia): free-exercise-db (Unlicense) başlangıç/bitiş kareleri
  ve talimatlar → Kulvar'da iki kareli animasyon; garminconnect.exercises → Garmin kategori/ad, böylece
  saat antrenmanda her hareketin kendi animasyonunu gösterir ve tekrar sayar. Otomatik eşleşmeyen
  hareketler toplu olarak bir kez Claude'a sorulur.
- Görseller yerel olarak static/uploads/exercise/ altına indirilir; sayfa görüntülenirken dış istek yok.
"""
from __future__ import annotations

import datetime as dt
import difflib
import json
import logging
import re
import threading
from pathlib import Path

import requests
from sqlalchemy.orm import Session

from . import i18n
from .clock import utcnow
from .models import ExerciseMedia, PlannedWorkout, Profile, StrengthRoutine

logger = logging.getLogger("kulvar.workout_library")

HEVY_URL = "https://api.hevyapp.com/v1"
FEDB_INDEX_URL = "https://raw.githubusercontent.com/yuhonas/free-exercise-db/main/dist/exercises.json"
FEDB_IMAGE_URL = "https://raw.githubusercontent.com/yuhonas/free-exercise-db/main/exercises/{path}"
APP_DIR = Path(__file__).resolve().parent
FEDB_CACHE = APP_DIR / "data" / "free-exercise-db.json"
FRAMES_DIR = APP_DIR / "static" / "uploads" / "exercise"

_last_refresh: dict[str, dt.date] = {}
NATIVE_PREFIX = "kulvar-"  # Kulvar'da oluşturulan rutinlerin kimlik öneki (Hevy yenilemesi bunlara dokunmaz)
_map_lock = threading.Lock()  # iki profilin senkronu aynı hareketleri iki kez eşlemesin
_fedb_cache: list[dict] | None = None

_EQUIPMENT_WORDS = {
    "dumbbell": "dumbbell", "barbell": "barbell", "cable": "cable", "machine": "machine", "kettlebell": "kettlebell",
    "smith machine": "smith", "band": "band", "bodyweight": "body", "ez bar": "ez",
}


# --------------------------------------------------------------------------- Hevy rutinleri

def _hevy_get(api_key: str, path: str, **params) -> dict:
    resp = requests.get(f"{HEVY_URL}{path}", headers={"api-key": api_key, "Accept": "application/json"}, params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


def needs_refresh(profile: Profile) -> bool:
    return _last_refresh.get(profile.id) != dt.date.today()


def refresh_routines(db: Session, profile: Profile, api_key: str) -> int:
    """Hevy rutinlerini çeker, yeni hareketlerin şablonlarını ekler ve eşler. Rutin sayısını döner."""
    routines, page = [], 1
    while page <= 10:
        data = _hevy_get(api_key, "/routines", page=page, pageSize=10)
        routines += data.get("routines") or []
        if page >= (data.get("page_count") or 1):
            break
        page += 1
    parsed = [(r, [{
        "template_id": e.get("exercise_template_id"), "title": e.get("title"), "rest_seconds": e.get("rest_seconds"),
        "notes": e.get("notes") or None,
        "sets": [{"type": s.get("type"), "reps": s.get("reps"), "weight_kg": s.get("weight_kg"),
                  "duration_seconds": s.get("duration_seconds")} for s in e.get("sets") or []],
    } for e in r.get("exercises") or []]) for r in routines]
    # Yeni hareket şablonları önce indirilir; veritabanına yazma tek ve kısa bir işlemde yapılır
    # (HTTP beklerken yazma kilidi tutulursa diğer profilin senkronu "database is locked" alır).
    known = {tid for (tid,) in db.query(ExerciseMedia.template_id)}
    templates: dict[str, dict] = {}
    for _, exercises in parsed:
        for e in exercises:
            tid = e["template_id"]
            if tid and tid not in known and tid not in templates:
                try:
                    templates[tid] = _hevy_get(api_key, f"/exercise_templates/{tid}")
                except requests.RequestException:
                    templates[tid] = {}
                templates[tid]["title"] = templates[tid].get("title") or e["title"]
    db.commit()  # açık okuma işlemini kapat; yazma aşağıda kısa bir işlemde yapılır
    for tid, t in templates.items():
        if db.get(ExerciseMedia, tid) is None:  # başka profil bu arada eklemiş olabilir
            db.add(ExerciseMedia(template_id=tid, title=t.get("title"), primary_muscle=t.get("primary_muscle_group"),
                                 equipment=t.get("equipment"), thumbnail_url=t.get("thumbnail_url")))
    replaced = {hid for (hid,) in db.query(StrengthRoutine.replaces_hevy_id).filter(
        StrengthRoutine.profile_id == profile.id, StrengthRoutine.replaces_hevy_id.isnot(None))}
    seen = set()
    for r, exercises in parsed:
        if str(r["id"]) in replaced:  # Kulvar'a taşınmış rutin: Hevy'deki eski kopyası geri gelmesin
            continue
        row = (db.query(StrengthRoutine).filter_by(profile_id=profile.id, hevy_id=str(r["id"])).first()
               or StrengthRoutine(profile_id=profile.id, hevy_id=str(r["id"])))
        row.title, row.exercises_json, row.updated_at = (r.get("title") or "Rutin")[:120], json.dumps(exercises, ensure_ascii=False), utcnow()
        row.notes = r.get("notes") or None
        db.add(row)
        seen.add(str(r["id"]))
    for old in db.query(StrengthRoutine).filter(StrengthRoutine.profile_id == profile.id).all():
        if old.hevy_id not in seen and not old.hevy_id.startswith(NATIVE_PREFIX):  # Kulvar rutinleri silinmez
            db.delete(old)
    db.commit()
    _last_refresh[profile.id] = dt.date.today()
    map_pending(db)
    return len(seen)


# --------------------------------------------------------------------------- eşleme

def _norm(text: str) -> str:
    text = (text or "").lower().replace("-", " ")
    text = re.sub(r"[()/,]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _base_and_equipment(title: str, equipment: str | None) -> tuple[str, str | None]:
    """ "Romanian Deadlift (Dumbbell)" → ("romanian deadlift", "dumbbell")"""
    m = re.match(r"^(.*?)\s*\(([^)]*)\)\s*$", title or "")
    base, paren = (m.group(1), m.group(2)) if m else (title, "")
    eq = None
    for word, key in _EQUIPMENT_WORDS.items():
        if word in paren.lower() or word in (equipment or "").lower():
            eq = key
            break
    return _norm(base), eq


def load_fedb() -> list[dict]:
    """free-exercise-db dizini (bir kez indirilir, app/data altında saklanır)."""
    global _fedb_cache
    if _fedb_cache is not None:
        return _fedb_cache
    if not FEDB_CACHE.exists():
        FEDB_CACHE.parent.mkdir(parents=True, exist_ok=True)
        resp = requests.get(FEDB_INDEX_URL, timeout=60)
        resp.raise_for_status()
        FEDB_CACHE.write_bytes(resp.content)
    _fedb_cache = json.loads(FEDB_CACHE.read_text(encoding="utf-8"))
    return _fedb_cache


_STOP = {"the", "with", "on", "a", "of", "and", "s"}


# Kataloglar aynı hareketi farklı yazıyor: "Fly"/"Flyes", "Abduction"/"Abductor", "Pushups"/"Push Up" …
_TOKEN_ALIASES = {"flye": "fly", "abductor": "abduction", "adductor": "adduction", "pressdown": "pushdown",
                  "pushup": "push up", "pullup": "pull up", "chinup": "chin up", "situp": "sit up"}
_PHRASE_ALIASES = {"overhead press": "shoulder press", "military press": "shoulder press",
                   "press down": "pushdown", "push down": "pushdown"}


def _tokens(text: str) -> set[str]:
    text = _norm(text)
    for phrase, alias in _PHRASE_ALIASES.items():
        text = text.replace(phrase, alias)
    out = set()
    for t in text.split():
        if t in _STOP:
            continue
        t = t.rstrip("s")
        out.update(_TOKEN_ALIASES.get(t, t).split())
    return out


def _score(base: str, eq: str | None, candidate: str) -> float:
    """Kelime örtüşmesi (aday fazla kelime içerdikçe ceza) + yazım benzerliği + ekipman uyumu."""
    b, c = _tokens(base), _tokens(candidate)
    eq_tokens = _tokens(eq or "")
    c_core = c - eq_tokens
    if not b or not c_core:
        return 0.0
    overlap = len(b & c_core) / len(b)
    extra = len(c_core - b)
    s = overlap - 0.08 * extra + 0.3 * difflib.SequenceMatcher(None, base, " ".join(sorted(c_core))).ratio()
    if eq and eq_tokens & c:
        s += 0.1
    elif eq and (c & {"dumbbell", "barbell", "cable", "machine", "kettlebell", "smith", "band"}):
        s -= 0.1  # başka ekipmanla yapılan varyant
    return s


def fedb_candidates(title: str, equipment: str | None, n: int = 8) -> list[tuple[float, dict]]:
    base, eq = _base_and_equipment(title, equipment)
    scored = [(_score(base, eq, x["name"]), x) for x in load_fedb()]
    return sorted(scored, key=lambda t: -t[0])[:n]


def garmin_candidates(title: str, equipment: str | None, n: int = 8) -> list[tuple[float, dict]]:
    from garminconnect import exercises as gx

    base, eq = _base_and_equipment(title, equipment)
    pool = [e if isinstance(e, dict) else {"name": e[0], "category": e[1], "exercise": e[2]} for e in gx.EXERCISES]
    scored = [(_score(base, eq, x["name"]), x) for x in pool]
    return sorted(scored, key=lambda t: -t[0])[:n]


def _download_frames(fedb_item: dict) -> list[str]:
    """Başlangıç/bitiş karelerini yerel klasöre indirir; static'e göre yolları döner."""
    out = []
    for i, rel in enumerate((fedb_item.get("images") or [])[:2]):
        dest = FRAMES_DIR / fedb_item["id"] / f"{i}.jpg"
        if not dest.exists():
            try:
                resp = requests.get(FEDB_IMAGE_URL.format(path=rel), timeout=30)
                resp.raise_for_status()
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(resp.content)
            except requests.RequestException:
                continue
        out.append(f"uploads/exercise/{fedb_item['id']}/{i}.jpg")
    return out


def _apply(media: ExerciseMedia, fedb_item: dict | None, garmin_item: dict | None, by: str) -> None:
    if fedb_item:
        media.fedb_id = fedb_item["id"]
        media.frames = json.dumps(_download_frames(fedb_item))
        media.instructions = json.dumps((fedb_item.get("instructions") or [])[:6], ensure_ascii=False)
    if garmin_item:
        media.garmin_category, media.garmin_name = garmin_item["category"], garmin_item["exercise"]
    media.mapped_by, media.updated_at = by, utcnow()


_AUTO_OK = 1.15  # tam örtüşme + ekipman uyumu; altı Claude'a sorulur
_FALLBACK_OK = 1.0  # Claude yokken kabul edilen en düşük skor (yanlış eşleme yerine eşlemesiz bırak)


def map_pending(db: Session) -> int:
    """Eşlenmemiş hareketleri eşler: önce otomatik, kalanları tek Claude çağrısıyla. Eşlenen sayısını döner."""
    with _map_lock:
        db.expire_all()  # kilidi bekleyen thread diğerinin eşlediklerini görsün
        return _map_pending(db)


def _map_pending(db: Session) -> int:
    pending = db.query(ExerciseMedia).filter(ExerciseMedia.mapped_by.is_(None)).all()
    if not pending:
        return 0
    unsure = []
    for media in pending:
        if _is_cardio_or_warmup(media.title):
            media.mapped_by = "none"
            continue
        fc = fedb_candidates(media.title, media.equipment)
        gc = garmin_candidates(media.title, media.equipment)
        if fc and gc and fc[0][0] >= _AUTO_OK and gc[0][0] >= _AUTO_OK:
            _apply(media, fc[0][1], gc[0][1], "auto")
        else:
            unsure.append((media, fc, gc))
    db.commit()
    if unsure:
        _map_with_claude(db, unsure)
    return len(pending)


def _is_cardio_or_warmup(title: str) -> bool:
    t = (title or "").lower()  # Türkçe Hevy: "Isınma".lower() → "isınma"
    return any(k in t for k in ("warm up", "warm-up", "elliptical", "rowing machine", "treadmill", "cycling", "stair",
                                "ısınma", "isınma", "isinma", "eliptik", "kürek makinesi", "koşu bandı", "bisiklet", "merdiven"))


def _map_with_claude(db: Session, unsure: list) -> None:
    """Otomatik eşleşmeyenleri toplu sorar: her hareket için aday listelerinden numara seçilir."""
    import anthropic

    from . import coach_chat
    from .config import settings
    from .models import CoachMessage, CoachThread

    if not coach_chat.api_key_configured():
        for media, fc, gc in unsure:  # Claude yoksa en iyi adayla yetin
            _apply(media, fc[0][1] if fc and fc[0][0] >= _FALLBACK_OK else None, gc[0][1] if gc and gc[0][0] >= _FALLBACK_OK else None, "auto")
        db.commit()
        return
    lines = []
    for i, (media, fc, gc) in enumerate(unsure):
        lines.append(f"{i}. {media.title} (ekipman: {media.equipment or '?'}, kas: {media.primary_muscle or '?'})")
        lines.append("   A: " + " | ".join(f"{j}={c[1]['name']}" for j, c in enumerate(fc)))
        lines.append("   G: " + " | ".join(f"{j}={c[1]['name']}" for j, c in enumerate(gc)))
    prompt = ("Aşağıdaki Hevy hareketlerinin her biri için A listesinden (görsel veritabanı) ve G listesinden (Garmin hareket "
              "kataloğu) aynı hareketi seç. Aynı hareket yoksa en yakın ve aynı hareket desenini (ör. aynı kas, aynı ekipman) "
              "seç; hiçbiri uymuyorsa -1 yaz. Sadece JSON dizisi döndür: [{\"i\":0,\"a\":2,\"g\":0}, ...]\n\n" + "\n".join(lines))
    try:
        client = anthropic.Anthropic()
        msg = client.messages.create(model=settings.coach_model, max_tokens=4000, messages=[{"role": "user", "content": prompt}])
        text = "".join(b.text for b in msg.content if b.type == "text")
        choices = json.loads(text[text.find("["): text.rfind("]") + 1])
        cost = coach_chat._cost(msg.usage, msg.model)
        thread = db.query(CoachThread).first()
        if thread is not None:
            db.add(CoachMessage(thread_id=thread.id, profile_id=thread.profile_id, seq=-1, role="health", content_json="[]",
                                model=msg.model, cost_usd=cost, input_tokens=msg.usage.input_tokens, output_tokens=msg.usage.output_tokens))
    except Exception:  # noqa: BLE001 - eşleme başarısızsa en iyi otomatik adayla devam
        logger.exception("Hareket eşlemesi Claude'a sorulamadı")
        choices = []
    picked = {c.get("i"): c for c in choices if isinstance(c, dict)}
    for i, (media, fc, gc) in enumerate(unsure):
        c = picked.get(i)
        if c is None:
            _apply(media, fc[0][1] if fc and fc[0][0] >= _FALLBACK_OK else None, gc[0][1] if gc and gc[0][0] >= _FALLBACK_OK else None, "auto")
            continue
        a, g = c.get("a", -1), c.get("g", -1)
        _apply(media, fc[a][1] if isinstance(a, int) and 0 <= a < len(fc) else None,
               gc[g][1] if isinstance(g, int) and 0 <= g < len(gc) else None, "claude")
    db.commit()


# --------------------------------------------------------------------------- plan ↔ rutin

def routines(db: Session, profile_id: str) -> list[StrengthRoutine]:
    return db.query(StrengthRoutine).filter(StrengthRoutine.profile_id == profile_id).order_by(StrengthRoutine.title).all()


def _routine_key(title: str) -> str:
    """ "D1 (Salı - Alt Vücut …)" → "d1" — parantez öncesi kısa ad."""
    return re.sub(r"\s*\(.*$", "", title or "").strip().lower()


def routine_for_plan(db: Session, workout: PlannedWorkout) -> StrengthRoutine | None:
    """Seçilmiş rutin; yoksa NOT bir rutin adıyla başlıyorsa o rutin (koç böyle yazar, ör. "D1 — …").
    Tahmin yapılmaz: "Ev D1" gibi farklı bir varyant yanlışlıkla salon D1'ine bağlanmasın."""
    if workout.workout_type != "strength":
        return None
    if workout.routine_id:
        chosen = db.get(StrengthRoutine, workout.routine_id)
        if chosen is not None:
            return chosen
    notes = (workout.notes or "").strip().lower()
    best = None
    for r in routines(db, workout.profile_id):
        key = _routine_key(r.title)
        if key and (notes == key or (notes.startswith(key) and not notes[len(key):len(key) + 1].isalnum())):
            if best is None or len(key) > len(_routine_key(best.title)):
                best = r
    return best


def _sets_summary(sets: list[dict], lang: str = "tr") -> str:
    work = [s for s in sets if s.get("type") != "warmup"] or sets
    if not work:
        return ""
    first = work[0]
    same = all((s.get("reps"), s.get("weight_kg"), s.get("duration_seconds")) == (first.get("reps"), first.get("weight_kg"), first.get("duration_seconds")) for s in work)
    def one(s):
        if s.get("duration_seconds"):
            return f"{int(s['duration_seconds'])} {i18n.t(lang, 'sn')}"
        txt = f"{s.get('reps') or '?'}"
        return txt + (f" · {s['weight_kg']:g} kg" if s.get("weight_kg") else "")
    return f"{len(work)}×{one(first)}" if same else " / ".join(one(s) for s in work)


def routine_view(db: Session, routine: StrengthRoutine, lang: str = "tr") -> list[dict]:
    """Şablon için: her hareketin görselleri, setleri ve talimatları."""
    return exercises_view(db, json.loads(routine.exercises_json or "[]"), lang)


def exercises_view(db: Session, exercises: list[dict], lang: str = "tr") -> list[dict]:
    out = []
    for e in exercises:
        media = db.get(ExerciseMedia, e.get("template_id")) if e.get("template_id") else None
        frames = json.loads(media.frames) if media and media.frames else []
        out.append({
            "title": e.get("title"),
            "sets": _sets_summary(e.get("sets") or [], lang),
            "rest": e.get("rest_seconds"),
            "notes": e.get("notes"),
            "frames": frames,
            "thumbnail": media.thumbnail_url if media and not frames else None,
            "muscle": media.primary_muscle if media else None,
            "instructions": json.loads(media.instructions) if media and media.instructions else [],
            "garmin": bool(media and media.garmin_category),
        })
    return out


def brief_lines(db: Session, profile: Profile) -> list[str]:
    """Koç verisi: kullanılabilir Hevy rutinleri (kuvvet satırında NOT'a rutin adını yazsın diye)."""
    rows = routines(db, profile.id)
    if not rows:
        return []
    lines = ["## Kuvvet Rutinleri (Hevy)"]
    for r in rows:
        names = [e.get("title") for e in json.loads(r.exercises_json or "[]")]
        lines.append(f"- {r.title}: " + ", ".join(n for n in names if n))
    lines.append("")
    return lines


# --------------------------------------------------------------------------- Garmin adımları

def _exercise_step(media: ExerciseMedia | None, title: str, s: dict, order: int) -> dict:
    step = {
        "type": "ExecutableStepDTO", "stepOrder": order,
        "stepType": {"stepTypeId": 3, "stepTypeKey": "interval", "displayOrder": 3},
        "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1},
        "description": title[:100],
    }
    if s.get("duration_seconds"):
        step["endCondition"] = {"conditionTypeId": 2, "conditionTypeKey": "time", "displayOrder": 2, "displayable": True}
        step["endConditionValue"] = float(s["duration_seconds"])
    else:
        step["endCondition"] = {"conditionTypeId": 10, "conditionTypeKey": "reps", "displayOrder": 10, "displayable": True}
        step["endConditionValue"] = float(s.get("reps") or 10)
    if media and media.garmin_category:
        step["category"], step["exerciseName"] = media.garmin_category, media.garmin_name
    if s.get("weight_kg"):
        step["weightValue"] = float(s["weight_kg"]) * 1000.0  # garminconnect ile aynı kural: gram, kilogram birimiyle
        step["weightUnit"] = {"unitId": 8, "unitKey": "kilogram", "factor": 1000.0}
    return step


def _rest_step(seconds: int, order: int) -> dict:
    return {"type": "ExecutableStepDTO", "stepOrder": order,
            "stepType": {"stepTypeId": 5, "stepTypeKey": "rest", "displayOrder": 5},
            "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time", "displayOrder": 2, "displayable": True},
            "endConditionValue": float(seconds),
            "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1}}


GARMIN_MAX_STEPS = 50


def _step_count(steps: list[dict]) -> int:
    return sum(1 + len(s.get("workoutSteps") or []) for s in steps)


def garmin_steps(db: Session, routine: StrengthRoutine) -> tuple[list[dict], int]:
    """Rutinden Garmin antrenman adımları: aynı setler tek tekrar grubunda. Toplam adım Garmin'in sınırını
    aşarsa farklı setli hareketler de ilk çalışma setiyle tek gruba indirilir. (adımlar, tahmini süre sn)"""
    steps, seconds = _build_steps(db, routine, uniform=False)
    if _step_count(steps) > GARMIN_MAX_STEPS:
        steps, seconds = _build_steps(db, routine, uniform=True)
    return steps, seconds


def _build_steps(db: Session, routine: StrengthRoutine, uniform: bool) -> tuple[list[dict], int]:
    steps, order, seconds = [], 1, 0
    for e in json.loads(routine.exercises_json or "[]"):
        title = e.get("title") or "Exercise"
        media = db.get(ExerciseMedia, e.get("template_id")) if e.get("template_id") else None
        sets = [s for s in e.get("sets") or []]
        rest = int(e.get("rest_seconds") or 60)
        if _is_cardio_or_warmup(title):
            dur = int(sum(s.get("duration_seconds") or 0 for s in sets) or 300)
            steps.append({"type": "ExecutableStepDTO", "stepOrder": order, "description": title[:100],
                          "stepType": {"stepTypeId": 1, "stepTypeKey": "warmup", "displayOrder": 1},
                          "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time", "displayOrder": 2, "displayable": True},
                          "endConditionValue": float(dur),
                          "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1}})
            order += 1
            seconds += dur
            continue
        if not sets:
            continue
        key = lambda s: (s.get("reps"), s.get("weight_kg"), s.get("duration_seconds"))
        if uniform:
            work = [s for s in sets if s.get("type") != "warmup"] or sets
            sets = [work[0]] * len(work)
        if len(sets) > 1 and all(key(s) == key(sets[0]) for s in sets):
            group_order = order
            inner = [_exercise_step(media, title, sets[0], order + 1), _rest_step(rest, order + 2)]
            steps.append({"type": "RepeatGroupDTO", "stepOrder": group_order, "numberOfIterations": len(sets), "smartRepeat": False,
                          "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
                          "endCondition": {"conditionTypeId": 7, "conditionTypeKey": "iterations", "displayOrder": 7, "displayable": False},
                          "endConditionValue": float(len(sets)), "workoutSteps": inner})
            order += 3
        else:
            for s in sets:
                steps.append(_exercise_step(media, title, s, order))
                steps.append(_rest_step(rest, order + 1))
                order += 2
        seconds += sum(int(s.get("duration_seconds") or 40) for s in sets) + rest * len(sets)
    return steps, seconds
