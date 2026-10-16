"""B: telefonda canlı kuvvet antrenmanı — başlat, set işaretle, dinlenme bildirimi, bitir, saat kaydını bağla."""
import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient

from app import background, live_workout, strength_log
from app.main import app
from app.models import Activity, PlannedWorkout, StrengthRoutine, StrengthSet, StrengthWorkout

ROUTINE = [
    {"template_id": "WU", "title": "Warm Up", "rest_seconds": 0, "sets": [{"type": "normal", "duration_seconds": 300}]},
    {"template_id": "RW", "title": "Dumbbell Row", "rest_seconds": 45, "notes": "Dirsek kalçaya",
     "sets": [{"type": "normal", "reps": 12, "weight_kg": 10}] * 2},
    {"template_id": "PL", "title": "Plank", "rest_seconds": 30, "sets": [{"type": "normal", "duration_seconds": 40}]},
]


@pytest.fixture
def env(db, profile, telegram, monkeypatch):
    monkeypatch.setattr(background, "start", lambda *a: True)
    profile.telegram_chat_id = "chat"
    routine = StrengthRoutine(profile_id=profile.id, hevy_id="kulvar-1", title="Ev D2 (Sırt)", exercises_json=json.dumps(ROUTINE))
    db.add(routine)
    db.add(PlannedWorkout(profile_id=profile.id, date=dt.date.today(), title="Kuvvet", workout_type="strength", notes="Ev D2: row"))
    # önceki antrenman: "önceki" değerleri için
    prev = StrengthWorkout(profile_id=profile.id, start_time=dt.datetime.now() - dt.timedelta(days=3), title="Ev D2 (Sırt)",
                           status="confirmed", source="kulvar")
    prev.sets = [StrengthSet(position=0, set_index=i, title="Dumbbell Row", template_id="RW", reps=12, weight_kg=7.5) for i in range(2)]
    db.add(prev)
    db.commit()
    client = TestClient(app)
    client.cookies.set("kulvar_profile", profile.id)
    return {"routine": routine, "client": client, "sent": telegram}


def test_start_prefills_from_routine_and_previous(db, profile, env):
    assert live_workout.todays_routine(db, profile).id == env["routine"].id
    r = env["client"].post(f"/strength/{profile.id}/live/start", data={"routine_id": env["routine"].id}, follow_redirects=False)
    w = db.query(StrengthWorkout).filter_by(status="live").one()
    assert r.headers["location"] == f"/strength/live/{w.id}" and w.planned_workout_id is not None
    state = live_workout.state(db, w, "tr")
    warm, row, plank = state["exercises"]
    assert warm["warmup"] and warm["sets"][0]["sec"] == 300
    assert [(s["reps"], s["kg"], s["prev"]) for s in row["sets"]] == [(12, 10, "12 × 7.5 kg"), (12, 10, "12 × 7.5 kg")]
    assert row["rest"] == 45 and row["notes"] == "Dirsek kalçaya"
    assert plank["timed"] and plank["sets"][0]["sec"] == 40
    assert live_workout.start(db, profile, env["routine"]).id == w.id  # aynı anda ikinci canlı antrenman açılmaz
    page = env["client"].get(f"/strength/live/{w.id}").text
    assert "Dumbbell Row" in page and "Antrenmanı bitir" in page
    assert "Devam eden antrenman" in env["client"].get("/strength").text


def test_api_set_and_finish(db, profile, env):
    w = live_workout.start(db, profile, env["routine"])
    api = f"/strength/live/{w.id}/api"
    row_sets = [s for s in w.sets if s.title == "Dumbbell Row"]
    c = env["client"]
    assert c.post(api, json={"op": "set", "set_id": row_sets[0].id, "reps": 10, "kg": 12.5, "done": True}).json()["ok"]
    added = c.post(api, json={"op": "add_set", "position": row_sets[0].position}).json()["set"]
    assert added["reps"] == 12 and added["kg"] == 10
    assert c.post(api, json={"op": "rest", "seconds": 45}).status_code == 400  # dinlenme bildirimi sunucuda yok
    assert env["sent"] == []  # Telegram'a hiçbir şey gitmedi
    r = c.post(f"/strength/live/{w.id}/finish", follow_redirects=False)
    assert r.headers["location"].endswith("saved=confirmed")
    db.expire_all()
    w = db.get(StrengthWorkout, w.id)
    assert w.status == "confirmed" and [(s.reps, s.weight_kg) for s in w.sets] == [(10, 12.5)]  # yapılmayanlar atıldı
    assert db.get(PlannedWorkout, w.planned_workout_id).status == "done"
    assert c.post(api, json={"op": "set", "set_id": "x"}).status_code == 409  # bitmiş antrenman değişmez


def test_finish_without_sets_keeps_workout(db, profile, env):
    wid = live_workout.start(db, profile, env["routine"]).id
    r = env["client"].post(f"/strength/live/{wid}/finish", follow_redirects=False)
    assert "empty=1" in r.headers["location"] and db.get(StrengthWorkout, wid).status == "live"
    env["client"].post(f"/strength/live/{wid}/discard")
    db.expire_all()
    assert db.get(StrengthWorkout, wid) is None


def test_watch_recording_links_to_live_workout(db, profile, env):
    w = live_workout.start(db, profile, env["routine"])
    db.add(Activity(profile_id=profile.id, source="garmin", external_id="901", activity_type="strength_training",
                    start_time=w.start_time + dt.timedelta(minutes=10), duration_s=1800))
    db.commit()

    class NoCalls:
        def __getattr__(self, name):
            raise AssertionError(f"Garmin çağrılmamalı: {name}")

    profile.strength_source = "kulvar"
    assert strength_log.ingest_recent(db, profile, NoCalls()) == []  # taslak açılmadı
    assert db.get(StrengthWorkout, w.id).garmin_activity_id == "901"
