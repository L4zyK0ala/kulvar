"""A2: hareket kataloğu, rutin düzenleyici, Hevy ile senkron, Hevy geçmişi ve kademeli yüklenme."""
import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient

from app import exercise_catalog, progression, routine_builder, routines, strength_log, workout_library as wl
from app.main import app
from app.models import Activity, Credential, ExerciseMedia, ProgressionSuggestion, StrengthRoutine, StrengthSet, StrengthWorkout

FEDB = [
    {"id": "Goblet_Squat", "name": "Goblet Squat", "equipment": "kettlebells", "category": "strength", "primaryMuscles": ["quadriceps"],
     "images": [], "instructions": ["Hold the weight."]},
    {"id": "Plank", "name": "Plank", "equipment": "body only", "category": "strength", "primaryMuscles": ["abdominals"],
     "images": [], "instructions": []},
    {"id": "Seated_Cable_Rows", "name": "Seated Cable Rows", "equipment": "cable", "category": "strength", "primaryMuscles": ["middle back"],
     "images": [], "instructions": []},
]


@pytest.fixture(autouse=True)
def small_fedb(monkeypatch):
    monkeypatch.setattr(wl, "_fedb_cache", FEDB)
    monkeypatch.setattr(wl, "_download_frames", lambda item: [])


@pytest.fixture
def hevy_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(routine_builder, "hevy_key", lambda db, pid: "key")
    monkeypatch.setattr(routine_builder, "_hevy_send", lambda method, key, path, body: calls.append((method, path, body)) or {})
    return calls


def _hevy_routine(db, profile, title="Ev D1 (Alt)", exercises=None):
    r = StrengthRoutine(profile_id=profile.id, hevy_id="abc123", title=title, exercises_json=json.dumps(exercises or [
        {"template_id": "AAAA1111", "title": "Dumbbell Row", "rest_seconds": 60, "sets": [{"type": "normal", "reps": 12, "weight_kg": 10}] * 3}]))
    db.add(r)
    db.commit()
    return r


# --------------------------------------------------------------------------- katalog

def test_catalog_search_and_ensure(db):
    db.add(ExerciseMedia(template_id="AAAA1111", title="Dumbbell Row", mapped_by="auto"))
    db.commit()
    results = exercise_catalog.search(db, "row")
    assert results[0]["key"] == "AAAA1111" and results[0]["known"]  # daha önce kullanılan önce
    assert any(r["key"] == "fedb:Seated_Cable_Rows" for r in results)
    media = exercise_catalog.ensure(db, "fedb:Goblet_Squat")
    assert media.title == "Goblet Squat" and media.equipment == "kettlebell" and media.primary_muscle == "quadriceps"
    assert media.garmin_name == "GOBLET_SQUAT"  # saatte adım olarak gider
    assert exercise_catalog.ensure(db, "fedb:yok") is None
    assert exercise_catalog.is_timed("Plank") and not exercise_catalog.is_timed("Goblet Squat")


# --------------------------------------------------------------------------- düzenleyici

def test_editor_actions():
    items = routines.apply_action(None, [], "noop")
    base = [{"template_id": "A", "title": "A", "sets": [{"type": "normal", "reps": 10}]},
            {"template_id": "B", "title": "B", "sets": [{"type": "normal", "reps": 8}]}]
    assert [e["title"] for e in routines.apply_action(None, base, "down:0")] == ["B", "A"]
    assert len(routines.apply_action(None, base, "add_set:1")[1]["sets"]) == 2
    assert routines.apply_action(None, base, "del_set:0:0")[0]["sets"] == base[0]["sets"]  # son set silinmez
    assert routines.apply_action(None, base, "mode:0")[0]["sets"][0] == {"type": "normal", "duration_seconds": 30}
    assert [e["title"] for e in routines.apply_action(None, base, "del_ex:0")] == ["B"]
    form = {"set_0_0_reps": "12", "set_0_0_kg": "7,5", "ex_0_rest": "45", "ex_0_notes": " yavaş "}
    out = routines.parse_form(base, form)
    assert out[0]["sets"][0]["reps"] == 12 and out[0]["sets"][0]["weight_kg"] == 7.5 and out[0]["rest_seconds"] == 45
    assert out[0]["notes"] == "yavaş" and items == []


def test_save_pushes_to_hevy_or_detaches(db, profile, hevy_calls):
    r = _hevy_routine(db, profile)
    items = routines.exercises(r)
    items[0]["sets"][0]["weight_kg"] = 12.5
    assert routines.save(db, profile, r, r.title, items) == "hevy"
    assert hevy_calls[0][:2] == ("PUT", "/routines/abc123")
    items = routines.apply_action(db, items, "add_ex:fedb:Plank")  # Hevy'de karşılığı yok
    assert routines.save(db, profile, r, r.title, items) == "detached"
    assert r.hevy_id.startswith("kulvar-") and r.replaces_hevy_id == "abc123" and len(hevy_calls) == 1


def test_refresh_keeps_native_and_skips_replaced(db, profile, monkeypatch):
    native = routines.new_native(db, profile, "Ev D9")
    moved = routines.new_native(db, profile, "Ev D1 (Alt)")
    moved.replaces_hevy_id = "abc123"
    db.commit()
    hevy = {"routines": [{"id": "abc123", "title": "Ev D1 (Alt)", "exercises": []}, {"id": "zzz", "title": "Pull", "exercises": []}],
            "page_count": 1}
    monkeypatch.setattr(wl, "_hevy_get", lambda key, path, **kw: hevy)
    monkeypatch.setattr(wl, "map_pending", lambda db_: 0)
    wl.refresh_routines(db, profile, "key")
    titles = sorted((r.title, r.hevy_id.startswith("kulvar-")) for r in wl.routines(db, profile.id))
    assert titles == [("Ev D1 (Alt)", True), ("Ev D9", True), ("Pull", False)]  # taşınan rutinin Hevy kopyası geri gelmedi
    assert native.id in {r.id for r in wl.routines(db, profile.id)}


def test_editor_pages(db, profile, hevy_calls):
    client = TestClient(app)
    client.cookies.set("kulvar_profile", profile.id)
    r = client.post(f"/strength/{profile.id}/routines/new", follow_redirects=False)
    rid = r.headers["location"].rsplit("/", 1)[1]
    assert "Yeni rutin" in client.get(f"/strength/routines/{rid}").text
    page = client.post(f"/strength/routines/{rid}", data={"title": "Ev D4", "action": "search", "q": "squat"})
    assert "fedb:Goblet_Squat" in page.text
    client.post(f"/strength/routines/{rid}", data={"title": "Ev D4", "action": "add_ex:fedb:Goblet_Squat"})
    client.post(f"/strength/routines/{rid}", data={"title": "Ev D4", "set_0_0_reps": "12", "set_0_0_kg": "8", "action": "save"})
    routine = db.get(StrengthRoutine, rid)
    db.refresh(routine)
    assert routine.title == "Ev D4" and routines.exercises(routine)[0]["sets"][0] == {"type": "normal", "reps": 12, "weight_kg": 8.0}
    assert hevy_calls == []  # Kulvar rutini Hevy'ye gitmez
    assert "Ev D4" in client.get("/strength?tab=routines").text


def test_builder_without_hevy_uses_free_catalog(db, profile, monkeypatch):
    monkeypatch.setattr(routine_builder, "hevy_key", lambda db_, pid: None)
    catalog = routine_builder.builder_catalog(db, profile, {"none", "kettlebell"})
    kinds = {t["title"]: t["type"] for t in catalog}
    assert kinds == {"Goblet Squat": "weight_reps", "Plank": "duration"}  # kablo seçilmedi


# --------------------------------------------------------------------------- Hevy geçmişi

def _hevy_workout(wid, start, reps=12, weight=10):
    return {"id": wid, "title": "Ev D1 (Alt)", "start_time": start.isoformat() + "Z", "end_time": (start + dt.timedelta(minutes=30)).isoformat() + "Z",
            "exercises": [{"title": "Dumbbell Row", "exercise_template_id": "AAAA1111",
                           "sets": [{"type": "normal", "reps": reps, "weight_kg": weight}] * 3}]}


def test_history_import_and_mirror(db, profile, monkeypatch):
    pages = {1: {"workouts": [_hevy_workout("w1", dt.datetime(2026, 9, 1, 16))], "page_count": 2},
             2: {"workouts": [_hevy_workout("w2", dt.datetime(2026, 8, 1, 16))], "page_count": 2}}
    monkeypatch.setattr(wl, "_hevy_get", lambda key, path, page=1, **kw: pages[page])
    assert strength_log.import_hevy_history(db, profile, "key") == 2
    assert strength_log.import_hevy_history(db, profile, "key") == 0  # tekrar aktarılmaz
    w = _hevy_workout("w3", dt.datetime(2026, 10, 15, 16))
    db.add(Activity(profile_id=profile.id, source="hevy", external_id="w3", activity_type=w["title"],
                    start_time=dt.datetime(2026, 10, 15, 19), duration_s=1800, raw_json=json.dumps(w)))
    db.commit()
    assert strength_log.mirror_hevy(db, profile) == 1
    assert db.query(StrengthWorkout).filter_by(source="hevy").count() == 3
    client = TestClient(app)
    client.cookies.set("kulvar_profile", profile.id)
    assert client.get("/strength").text.count("Dumbbell Row") == 3  # Kuvvet sayfası Kulvar kaydından


# --------------------------------------------------------------------------- kademeli yüklenme

def test_next_step_rules():
    assert progression.next_step([{"reps": 12, "weight_kg": 10}]) == [{"reps": 14, "weight_kg": 10}]
    assert progression.next_step([{"reps": 15, "weight_kg": 10}]) == [{"reps": 11, "weight_kg": 12.5}]
    assert progression.next_step([{"reps": 20}]) == [{"reps": 22}]
    assert progression.next_step([{"reps": 25}]) is None
    assert progression.next_step([{"duration_seconds": 40}]) == [{"duration_seconds": 50}]
    assert progression.met([{"reps": 12, "weight_kg": 10}] * 2, [{"reps": 12, "weight_kg": 10}, {"reps": 13, "weight_kg": 10}])
    assert not progression.met([{"reps": 12, "weight_kg": 10}] * 2, [{"reps": 12, "weight_kg": 10}])  # eksik set
    assert not progression.met([{"reps": 12, "weight_kg": 10}], [{"reps": 12, "weight_kg": 7.5}])


def _done(db, profile, day, reps=12, weight=10):
    w = StrengthWorkout(profile_id=profile.id, start_time=day, title="Ev D1 (Alt)", status="confirmed", source="kulvar")
    for i in range(3):
        w.sets.append(StrengthSet(position=0, set_index=i, title="Dumbbell Row", template_id="AAAA1111", reps=reps, weight_kg=weight))
    db.add(w)
    db.commit()
    return w


def test_suggestion_after_two_successes_and_apply(db, profile, telegram, hevy_calls):
    profile.telegram_chat_id = "chat"
    _hevy_routine(db, profile)
    first = _done(db, profile, dt.datetime(2026, 10, 13, 19))
    assert progression.check(db, profile, first) == []  # tek başarı yetmez
    second = _done(db, profile, dt.datetime(2026, 10, 15, 19))
    created = progression.check(db, profile, second)
    assert len(created) == 1 and "3×12 · 10 kg → 3×14 · 10 kg" in progression.describe(created[0], "tr")
    assert "pg:" in telegram[-1][2][0][0]["callback_data"]
    assert progression.check(db, profile, second) == []  # bekleyen öneri varken yenisi yok
    assert progression.decide(db, profile, created[0], apply=True) == "applied"
    routine = db.query(StrengthRoutine).one()
    assert routines.exercises(routine)[0]["sets"][0]["reps"] == 14 and hevy_calls[-1][0] == "PUT"
    assert progression.decide(db, profile, created[0], apply=True) == "done"


def test_missed_target_no_suggestion_and_stale(db, profile, telegram, hevy_calls):
    _hevy_routine(db, profile)
    _done(db, profile, dt.datetime(2026, 10, 13, 19))
    w = _done(db, profile, dt.datetime(2026, 10, 15, 19), reps=10)  # hedefin altında
    assert progression.check(db, profile, w) == []
    s = ProgressionSuggestion(profile_id=profile.id, routine_id=db.query(StrengthRoutine).one().id, exercise_key="AAAA1111",
                              title="Dumbbell Row", old_json=json.dumps([{"type": "normal", "reps": 8}]), new_json="[]")
    db.add(s)
    db.commit()
    assert progression.decide(db, profile, s, apply=True) == "stale"  # rutin değişmiş


# --------------------------------------------------------------------------- evdeki ağırlıklar

def test_parse_weights_and_home_steps():
    assert progression.parse_weights("5, 10 kg; 7,5 · 0 · 500") == [5.0, 7.5, 10.0]
    home = [5.0, 10.0]
    assert progression.next_step([{"reps": 12, "weight_kg": 5}], home) == [{"reps": 14, "weight_kg": 5}]  # önce tekrar
    assert progression.next_step([{"reps": 15, "weight_kg": 5}], home) == [{"reps": 8, "weight_kg": 10.0}]  # sonraki mevcut ağırlık
    assert progression.next_step([{"reps": 15, "weight_kg": 10}], home) == [{"reps": 17, "weight_kg": 10}]  # en ağırı: tekrar artar
    assert progression.next_step([{"reps": 20, "weight_kg": 10}], home) is None
    assert progression.next_step([{"reps": 15, "weight_kg": 10}]) == [{"reps": 11, "weight_kg": 12.5}]  # liste yok: +2,5 kg


def test_equipment_form_and_progression_use_inventory(db, profile, telegram, hevy_calls):
    client = TestClient(app)
    client.cookies.set("kulvar_profile", profile.id)
    client.post(f"/strength/{profile.id}/equipment", data={"dumbbell": "10, 5", "kettlebell": ""})
    db.refresh(profile)
    assert progression.inventory(profile) == {"dumbbell": [5.0, 10.0]}
    assert 'value="5, 10"' in client.get("/strength?tab=routines").text
    params = routine_builder.last_params(db, profile)
    assert "5, 10 kg" in params["weights"] and "dumbbell" in params["equipment"]
    db.add(ExerciseMedia(template_id="AAAA1111", title="Dumbbell Row", equipment="dumbbell", mapped_by="auto"))
    _hevy_routine(db, profile, exercises=[{"template_id": "AAAA1111", "title": "Dumbbell Row", "rest_seconds": 60,
                                           "sets": [{"type": "normal", "reps": 15, "weight_kg": 5}] * 3}])
    for day in (13, 15):
        w = StrengthWorkout(profile_id=profile.id, start_time=dt.datetime(2026, 10, day, 19), title="Ev D1 (Alt)", status="confirmed", source="kulvar")
        w.sets = [StrengthSet(position=0, set_index=i, title="Dumbbell Row", template_id="AAAA1111", reps=15, weight_kg=5) for i in range(3)]
        db.add(w)
    db.commit()
    s = progression.check(db, profile, w)[0]
    assert progression.describe(s, "tr") == "Dumbbell Row: 3×15 · 5 kg → 3×8 · 10 kg"
