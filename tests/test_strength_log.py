"""Kulvar'ın kendi kuvvet kaydı: saatten taslak, onay ekranı, Telegram onayı ve doğru değerlerin kaynağı."""
import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient

from app import background, garmin_sets, strava_merge, strength_log
from app.main import app
from app.models import Activity, Credential, PlannedWorkout, StrengthRoutine, StrengthWorkout

START = dt.datetime(2026, 10, 15, 19, 12)
NOW = dt.datetime(2026, 10, 15, 22, 0)
ROUTINE = [
    {"template_id": "WU", "title": "Warm Up", "sets": [{"type": "normal", "duration_seconds": 300}]},
    {"template_id": "RL", "title": "Reverse Lunge (Dumbbell)", "sets": [{"type": "normal", "reps": 8, "weight_kg": 5}] * 2},
    {"template_id": "PL", "title": "Plank", "sets": [{"type": "normal", "duration_seconds": 45}] * 2},
]


def _step(kind, title="", category=None, name=None):
    return {"type": "ExecutableStepDTO", "stepType": {"stepTypeKey": kind}, "description": title, "category": category, "exerciseName": name}


WORKOUT = {"workoutSegments": [{"workoutSteps": [
    _step("warmup", "Warm Up"),
    {"type": "RepeatGroupDTO", "workoutSteps": [_step("interval", "Reverse Lunge (Dumbbell)", "LUNGE", "DUMBBELL_REVERSE_LUNGE"), _step("rest")]},
    {"type": "RepeatGroupDTO", "workoutSteps": [_step("interval", "Plank", "PLANK", "PLANK"), _step("rest")]},
]}]}


def _gset(idx, category, reps, grams=None, secs=40, kind="ACTIVE"):
    return {"setType": kind, "wktStepIndex": idx, "exercises": [{"category": category, "name": None}],
            "repetitionCount": reps, "weight": grams, "duration": secs}


def _payload():
    return {"exerciseSets": [
        _gset(0, "PUSH_UP", 23, secs=300),
        _gset(1, "LUNGE", 16, 0.0), _gset(2, None, None, kind="REST"),
        _gset(1, "LUNGE", 16, 5000.0), _gset(2, None, None, kind="REST"),
        _gset(4, "PLANK", None, secs=41), _gset(5, None, None, kind="REST"),
        _gset(4, "PLANK", None, secs=38),
    ]}


class FakeGarmin:
    def __init__(self):
        self.puts = []

    def get_activity_exercise_sets(self, activity_id):
        return _payload()

    def get_workout_by_id(self, workout_id):
        return WORKOUT

    def set_activity_exercise_sets(self, activity_id, payload):
        self.puts.append(payload)


@pytest.fixture
def setup(db, profile, telegram, monkeypatch):
    started = []
    monkeypatch.setattr(background, "start", lambda key, fn, *a: started.append(key) or True)
    profile.strength_source, profile.telegram_chat_id = "kulvar", "chat"
    routine = StrengthRoutine(profile_id=profile.id, hevy_id="r1", title="Ev D2 (Alt vücut)", exercises_json=json.dumps(ROUTINE))
    db.add(routine)
    db.add(PlannedWorkout(profile_id=profile.id, date=START.date(), title="Kuvvet", workout_type="strength",
                          notes="Ev D2: lunge", garmin_workout_id="555"))
    db.add(Activity(profile_id=profile.id, source="garmin", external_id="900", activity_type="strength_training",
                    start_time=START, duration_s=33 * 60, raw_json=json.dumps({"workoutId": 555})))
    db.commit()
    return {"started": started, "sent": telegram}


def test_source_defaults_to_hevy_when_connected(db, profile):
    assert strength_log.source_of(db, profile) == "kulvar"
    db.add(Credential(profile_id=profile.id, source="hevy", encrypted_payload="x"))
    db.commit()
    assert strength_log.source_of(db, profile) == "hevy"
    profile.strength_source = "kulvar"
    assert strength_log.source_of(db, profile) == "kulvar"


def test_draft_from_watch_and_plan(db, profile, setup):
    drafts = strength_log.ingest_recent(db, profile, FakeGarmin(), NOW)
    assert len(drafts) == 1
    d = drafts[0]
    assert d.title == "Ev D2 (Alt vücut)" and d.status == "draft" and d.garmin_activity_id == "900"
    warm = [s for s in d.sets if s.set_type == "warmup"]
    lunge = [s for s in d.sets if s.title.startswith("Reverse")]
    plank = [s for s in d.sets if s.title == "Plank"]
    assert warm[0].duration_s == 300
    assert [(s.reps, s.weight_kg, s.watch_reps) for s in lunge] == [(8, 5, 16), (8, 5, 16)]  # plan esas, saat ipucu
    assert [s.duration_s for s in plank] == [41, 38]  # süre saatten
    assert len(setup["sent"]) == 1 and "sw:" in setup["sent"][0][2][0][0]["callback_data"]
    assert strength_log.ingest_recent(db, profile, FakeGarmin(), NOW) == []  # aynı aktivite için ikinci taslak yok


def test_hevy_mode_waits_and_copies(db, profile, setup):
    profile.strength_source = "hevy"
    db.commit()
    assert strength_log.ingest_recent(db, profile, FakeGarmin(), NOW) == []
    assert db.query(StrengthWorkout).count() == 0  # Hevy kaydı gelene kadar bekler
    hevy = {"title": "Ev D2 (Alt vücut)", "exercises": [{"title": "Reverse Lunge (Dumbbell)", "exercise_template_id": "RL",
                                                          "sets": [{"type": "normal", "reps": 8, "weight_kg": 5}]}]}
    db.add(Activity(profile_id=profile.id, source="hevy", external_id="h1", activity_type="Ev D2", start_time=START,
                    duration_s=30 * 60, raw_json=json.dumps(hevy)))
    db.commit()
    strength_log.ingest_recent(db, profile, FakeGarmin(), NOW)
    w = db.query(StrengthWorkout).one()
    assert w.status == "confirmed" and w.source == "hevy" and w.garmin_activity_id == "900" and setup["sent"] == []


def test_review_page_edit_and_confirm(db, profile, setup):
    d = strength_log.ingest_recent(db, profile, FakeGarmin(), NOW)[0]
    client = TestClient(app)
    client.cookies.set("kulvar_profile", profile.id)
    page = client.get(f"/strength/workouts/{d.id}").text
    assert page.count('class="set-row"') == 5 and "Onayla" in page
    assert "onay bekliyor" in client.get("/strength").text
    lunge = [s for s in d.sets if s.title.startswith("Reverse")]
    client.post(f"/strength/workouts/{d.id}", data={f"reps_{lunge[0].id}": "10", f"kg_{lunge[0].id}": "7,5", "action": f"add:{lunge[0].position}"})
    db.expire_all()
    assert len([s for s in db.get(StrengthWorkout, d.id).sets if s.title.startswith("Reverse")]) == 3
    client.post(f"/strength/workouts/{d.id}", data={"action": f"del:{lunge[1].id}"})
    r = client.post(f"/strength/workouts/{d.id}", data={"action": "confirm"}, follow_redirects=False)
    assert r.headers["location"].endswith("saved=confirmed")
    db.expire_all()
    w = db.get(StrengthWorkout, d.id)
    assert w.status == "confirmed" and [(s.reps, s.weight_kg) for s in w.sets if s.title.startswith("Reverse")] == [(10, 7.5), (8, 5.0)]  # yeni set son setin kopyası
    assert db.query(PlannedWorkout).one().status == "done"
    assert setup["started"]  # Garmin/Strava güncellemesi başlatıldı


def test_confirmed_record_drives_garmin_and_strava(db, profile, setup):
    d = strength_log.ingest_recent(db, profile, FakeGarmin(), NOW)[0]
    lunge = [s for s in d.sets if s.title.startswith("Reverse")]
    lunge[0].reps, lunge[0].weight_kg = 10, 7.5
    d.sets.remove(lunge[1])  # ikinci lunge setini yapmadı
    strength_log.confirm(db, d)
    fake = FakeGarmin()
    changes = garmin_sets.fix_recent(db, profile, fake, NOW)
    sets = fake.puts[0]["exerciseSets"]
    lunges = [s for s in sets if s["setType"] == "ACTIVE" and s["wktStepIndex"] == 1]
    assert [(s["repetitionCount"], s["weight"]) for s in lunges] == [(10, 7500.0)]  # silinen set Garmin'den de gitti
    assert sets[0]["exercises"][0]["category"] == "WARM_UP"
    assert any("silindi" in c for c in changes)
    name, text = strava_merge.describe_exercises(d.title, strength_log.exercises_of(d), "tr", "Garmin")
    assert name == "Ev D2 (Alt vücut)" and "Reverse Lunge (Dumbbell): 1×10 · 7.5 kg" in text and text.endswith("— Garmin · Kulvar")


def test_telegram_confirm_button(db, profile, setup, monkeypatch):
    from app import telegram_bot
    d = strength_log.ingest_recent(db, profile, FakeGarmin(), NOW)[0]
    calls = []
    monkeypatch.setattr(telegram_bot, "api", lambda method, token, **kw: calls.append((method, kw)) or {})
    monkeypatch.setattr(telegram_bot, "_profiles_for_token", lambda db_, token: [profile])
    cb = {"id": "1", "data": f"sw:{d.id}:ok", "message": {"chat": {"id": "chat"}, "message_id": 7}}
    telegram_bot._handle_callback(cb, "tok")
    db.expire_all()
    assert db.get(StrengthWorkout, d.id).status == "confirmed"
    assert any(m == "editMessageText" and "<b>Ev D2 (Alt vücut)</b>" in kw["text"] for m, kw in calls)
    telegram_bot._handle_callback(cb, "tok")  # ikinci basış: zaten onaylandı
    assert calls[-1][0] == "answerCallbackQuery"
