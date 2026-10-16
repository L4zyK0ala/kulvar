"""Kuvvet: hareket eşleme, rutin ↔ plan bağlama, Garmin adımları ve koçun rutin taslakları."""
import datetime as dt
import json

import pytest

from app import routine_builder as rb
from app import workout_library as wl
from app.models import ExerciseMedia, PlannedWorkout, RoutineDraft, StrengthRoutine

FEDB = [  # free-exercise-db'den küçük bir örnek (gerçek dizin indirilmez)
    {"id": "Dumbbell_Flyes", "name": "Dumbbell Flyes", "images": [], "instructions": []},
    {"id": "Leverage_Chest_Press", "name": "Leverage Chest Press", "images": [], "instructions": []},
    {"id": "Pushups", "name": "Pushups", "images": [], "instructions": []},
    {"id": "Plyo_Push-up", "name": "Plyo Push-up", "images": [], "instructions": []},
    {"id": "Thigh_Abductor", "name": "Thigh Abductor", "images": [], "instructions": []},
    {"id": "Dumbbell_Shoulder_Press", "name": "Dumbbell Shoulder Press", "images": [], "instructions": []},
    {"id": "Arnold_Dumbbell_Press", "name": "Arnold Dumbbell Press", "images": [], "instructions": []},
]


@pytest.fixture(autouse=True)
def small_fedb(monkeypatch):
    monkeypatch.setattr(wl, "_fedb_cache", FEDB)
    monkeypatch.setattr(wl, "_download_frames", lambda item: [f"uploads/exercise/{item['id']}/0.jpg", f"uploads/exercise/{item['id']}/1.jpg"])


def test_token_aliases():
    assert wl._tokens("Chest Fly") >= {"fly"} and "fly" in wl._tokens("Dumbbell Flyes")
    assert "abduction" in wl._tokens("Thigh Abductor")
    assert {"push", "up"} <= wl._tokens("Pushups")
    assert "pushdown" in wl._tokens("Rope Press-down")
    assert "shoulder" in wl._tokens("Overhead Press")


@pytest.mark.parametrize("title, equipment, expected", [
    ("Chest Fly (Dumbbell)", "dumbbell", "Dumbbell_Flyes"),
    ("Push Up", "none", "Pushups"),
    ("Overhead Press (Dumbbell)", "dumbbell", "Dumbbell_Shoulder_Press"),
    ("Hip Abduction (Machine)", "machine", "Thigh_Abductor"),
])
def test_fedb_best_candidate(title, equipment, expected):
    assert wl.fedb_candidates(title, equipment)[0][1]["id"] == expected


def test_garmin_rope_pushdown():
    best = wl.garmin_candidates("Triceps Rope Pushdown", "cable")[0][1]
    assert best["exercise"] == "ROPE_PRESSDOWN"


def test_map_pending_without_claude_skips_cardio_and_weak(db, monkeypatch):
    from app import coach_chat
    monkeypatch.setattr(coach_chat, "api_key_configured", lambda: False)
    db.add_all([ExerciseMedia(template_id="A", title="Push Up", equipment="none"),
                ExerciseMedia(template_id="B", title="Warm Up", equipment="none"),
                ExerciseMedia(template_id="C", title="Zzz Unknown Move", equipment="none")])
    db.commit()
    wl.map_pending(db)
    rows = {m.template_id: m for m in db.query(ExerciseMedia)}
    assert rows["A"].fedb_id == "Pushups" and rows["A"].garmin_name == "PUSH_UP" and rows["A"].mapped_by == "auto"
    assert rows["B"].mapped_by == "none"
    assert rows["C"].fedb_id is None  # yanlış eşleme yerine eşlemesiz


def _routine(db, profile, title, exercises=None):
    r = StrengthRoutine(profile_id=profile.id, hevy_id=title, title=title, exercises_json=json.dumps(exercises or []))
    db.add(r)
    db.commit()
    return r


def _plan(db, profile, notes, **kw):
    w = PlannedWorkout(profile_id=profile.id, date=dt.date(2026, 10, 13), title="Kuvvet", workout_type="strength", notes=notes, **kw)
    db.add(w)
    db.commit()
    return w


def test_routine_for_plan_is_strict(db, profile):
    gym = _routine(db, profile, "D1 (Salı - Alt Vücut)")
    home = _routine(db, profile, "Ev D1 (Squat + itiş)")
    assert wl.routine_for_plan(db, _plan(db, profile, "D1 — alt vücut ağırlıklı")).id == gym.id
    assert wl.routine_for_plan(db, _plan(db, profile, "Ev D1 (2x5 kg): squat")).id == home.id  # en uzun anahtar
    assert wl.routine_for_plan(db, _plan(db, profile, "Hafif; kalça/core")) is None  # tahmin yok
    assert wl.routine_for_plan(db, _plan(db, profile, "D10 farklı rutin")) is None  # "d1" + rakam eşleşmez
    chosen = _plan(db, profile, "Hafif", routine_id=gym.id)
    assert wl.routine_for_plan(db, chosen).id == gym.id


def test_sets_summary():
    assert wl._sets_summary([{"reps": 10, "weight_kg": 5}] * 3) == "3×10 · 5 kg"
    assert wl._sets_summary([{"duration_seconds": 40}] * 2) == "2×40 sn"
    assert wl._sets_summary([{"type": "warmup", "reps": 15}, {"reps": 8}, {"reps": 6}]) == "8 / 6"


def test_garmin_steps_group_and_compress(db, profile):
    one = [{"template_id": None, "title": "Squat", "rest_seconds": 60, "sets": [{"reps": 10}] * 3}]
    steps, _ = wl.garmin_steps(db, _routine(db, profile, "Kısa", one))
    assert len(steps) == 1 and steps[0]["type"] == "RepeatGroupDTO" and steps[0]["numberOfIterations"] == 3

    warm = [{"template_id": None, "title": "Warm Up", "sets": [{"duration_seconds": 300}]}]
    steps, seconds = wl.garmin_steps(db, _routine(db, profile, "Isınma", warm))
    assert steps[0]["stepType"]["stepTypeKey"] == "warmup" and seconds == 300

    pyramid = [{"template_id": None, "title": f"Hareket {i}", "rest_seconds": 60,
                "sets": [{"reps": 12}, {"reps": 10}, {"reps": 8}, {"reps": 6}]} for i in range(10)]
    steps, _ = wl.garmin_steps(db, _routine(db, profile, "Uzun", pyramid))
    assert wl._step_count(steps) <= wl.GARMIN_MAX_STEPS  # 80 adım → tek gruplara sıkıştırılır


# --------------------------------------------------------------------------- rutin oluşturucu

CATALOG = {
    "GS": {"id": "GS", "title": "Goblet Squat", "type": "weight_reps", "equipment": "dumbbell"},
    "PU": {"id": "PU", "title": "Push Up", "type": "reps_only", "equipment": "none"},
    "PL": {"id": "PL", "title": "Plank", "type": "duration", "equipment": "none"},
}


def test_normalize_draft():
    raw = {"routines": [
        {"title": "Ev D1 (Alt vücut + itiş)", "exercises": [
            {"id": "GS", "sets": 9, "reps": 12, "weight_kg": 5, "rest_seconds": 999},
            {"id": "PU", "sets": 3, "reps": 10, "weight_kg": 20},
            {"id": "PL", "sets": 3, "duration_seconds": 40},
            {"id": "YOK", "sets": 3}]},
        {"title": "Üst vücut", "exercises": [{"id": "PU"}]},
        {"title": "Ev D3", "exercises": [{"id": "YOK"}]}]}
    out = rb.normalize(raw, CATALOG, "Ev D")
    assert [r["title"] for r in out] == ["Ev D1 (Alt vücut + itiş)", "Ev D2 (Üst vücut)"]  # geçersiz rutin atlanır
    gs, pu, pl = out[0]["exercises"]
    assert len(gs["sets"]) == 6 and gs["rest_seconds"] == 300 and gs["sets"][0]["weight_kg"] == 5.0  # sınırlar
    assert "weight_kg" not in pu["sets"][0]  # tekrar-only harekete ağırlık yazılmaz
    assert pl["sets"][0] == {"type": "normal", "duration_seconds": 40}
    assert out[1]["exercises"][0]["sets"][0]["reps"] == 10  # varsayılanlar


def test_normalize_replaces_copied_placeholder():
    raw = {"routines": [{"title": "Ev D1 (kısa odak)", "notes": "Kalça + core. Yavaş in.", "exercises": [{"id": "PU"}]}]}
    assert rb.normalize(raw, CATALOG, "Ev D")[0]["title"] == "Ev D1 (Kalça + core)"


@pytest.mark.parametrize("title, notes, expected", [
    ("Ev D1 (kısa odak)", "Squat paterni + itiş + yan core. Nefesini tutma.", "Ev D1 (Squat paterni + itiş + yan core)"),
    ("Home D2 (short focus)", "Hips and core. Go slow", "Home D2 (Hips and core)"),
    ("Ev D2 (…)", "", "Ev D2"),
    ("Ev D3 (Alt vücut)", "farklı not", "Ev D3 (Alt vücut)"),  # gerçek odak korunur
])
def test_fix_placeholder_title(title, notes, expected):
    assert rb.fix_title(title, notes) == expected


def test_save_to_hevy_creates_then_updates(db, profile, monkeypatch):
    profile.strength_source = "hevy"  # Hevy modu: rutinler Hevy'ye yazılır (Kulvar modu test_routines'ta)
    calls = []
    monkeypatch.setattr(rb, "hevy_key", lambda db, pid: "key")
    monkeypatch.setattr(rb, "_ensure_folder", lambda key: 42)
    monkeypatch.setattr(rb, "_hevy_send", lambda method, key, path, body: calls.append((method, path, body["routine"])) or {})

    def fake_refresh(db_, profile_, key):  # Hevy'den geri çekilmiş gibi
        for r in json.loads(draft.routines_json):
            if not any(x.title == r["title"] for x in wl.routines(db_, profile_.id)):
                _routine(db_, profile_, r["title"], r["exercises"])
    monkeypatch.setattr(wl, "refresh_routines", fake_refresh)

    routines = rb.normalize({"routines": [{"title": "Ev D1 (Alt)", "exercises": [{"id": "GS", "reps": 10}]}]}, CATALOG, "Ev D")
    draft = RoutineDraft(profile_id=profile.id, status="saving", routines_json=json.dumps(routines))
    db.add(draft)
    db.commit()
    rb.save_to_hevy(db, draft)
    assert [(m, p) for m, p, _ in calls] == [("POST", "/routines")]
    assert calls[0][2]["folder_id"] == 42 and calls[0][2]["exercises"][0]["exercise_template_id"] == "GS"
    assert draft.status == "saved"

    calls.clear()
    draft.status = "saving"
    rb.save_to_hevy(db, draft)
    assert [(m, p) for m, p, _ in calls] == [("PUT", "/routines/Ev D1 (Alt)")]  # aynı ad: kopya yok, güncelleme
    assert "folder_id" not in calls[0][2]


def test_reset_interrupted(db, profile):
    db.add_all([RoutineDraft(profile_id=profile.id, status="running"), RoutineDraft(profile_id=profile.id, status="saving")])
    db.commit()
    rb.reset_interrupted(db)
    assert sorted(d.status for d in db.query(RoutineDraft)) == ["failed", "save_failed"]
