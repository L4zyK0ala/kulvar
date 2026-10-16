"""Antrenman sonrası his (RPE) ve haftalık algılanan yük."""
import datetime as dt

from fastapi.testclient import TestClient

from app import feedback
from app.main import app
from app.models import Activity, StrengthSet, StrengthWorkout, WorkoutFeedback


def _run(db, profile, start, source="garmin", ext="g1", minutes=40):
    a = Activity(profile_id=profile.id, source=source, external_id=ext, activity_type="running", start_time=start,
                 duration_s=minutes * 60, distance_m=6000)
    db.add(a)
    db.commit()
    return a


def test_one_feedback_for_all_copies(db, profile):
    start = dt.datetime.now() - dt.timedelta(hours=3)
    garmin, strava = _run(db, profile, start), _run(db, profile, start + dt.timedelta(seconds=20), "strava", "s1")
    client = TestClient(app)
    client.cookies.set("kulvar_profile", profile.id)
    page = client.get("/dashboard").text
    assert "Nasıl hissettirdi?" in page  # son 48 saatin antrenmanı soruluyor
    client.post(f"/feedback/{profile.id}", data={"kind": "act", "obj_id": strava.id, "rpe": "7", "back": "/dashboard"})
    client.post(f"/feedback/{profile.id}", data={"kind": "act", "obj_id": garmin.id, "rpe": "", "note": " sol diz hafif ", "back": "/dashboard"})
    rows = db.query(WorkoutFeedback).all()
    assert len(rows) == 1 and rows[0].rpe == 7 and rows[0].note == "sol diz hafif"  # not puanı silmedi
    assert 'class="rpe-btn on"' in client.get(f"/activities/{garmin.id}").text
    r = client.post(f"/feedback/{profile.id}", data={"kind": "act", "obj_id": garmin.id, "rpe": "5", "back": "https://evil.example"}, follow_redirects=False)
    assert r.headers["location"].startswith("/dashboard")  # dış adrese yönlendirme yok


def test_strength_workout_shares_feedback_with_watch_activity(db, profile):
    g = Activity(profile_id=profile.id, source="garmin", external_id="900", activity_type="strength_training",
                 start_time=dt.datetime.now() - dt.timedelta(hours=1), duration_s=1800)
    db.add(g)
    w = StrengthWorkout(profile_id=profile.id, start_time=g.start_time, duration_s=1800, title="Ev D2", status="confirmed",
                        source="kulvar", garmin_activity_id="900")
    w.sets = [StrengthSet(position=0, set_index=0, title="Row", reps=12, weight_kg=10)]
    db.add(w)
    db.commit()
    client = TestClient(app)
    client.cookies.set("kulvar_profile", profile.id)
    assert "Nasıl hissettirdi?" in client.get(f"/strength/workouts/{w.id}").text
    client.post(f"/feedback/{profile.id}", data={"kind": "sw", "obj_id": w.id, "rpe": "8"})
    assert feedback.find(db, profile.id, feedback.refs_for_activity(db, g)).rpe == 8  # saatin kaydında da aynı puan


def test_brief_and_load_jump(db, profile):
    today = dt.date(2026, 10, 16)  # Cuma
    monday = today - dt.timedelta(days=today.weekday())
    for day, rpe, minutes in ((monday - dt.timedelta(days=5), 5, 40), (monday + dt.timedelta(days=1), 8, 45), (monday + dt.timedelta(days=3), 7, 40)):
        db.add(WorkoutFeedback(profile_id=profile.id, ref=f"act:{day}", date=day, title="Koşu", minutes=minutes, rpe=rpe))
    db.commit()
    lines = feedback.brief_lines(db, profile, today)
    assert any("RPE 8" in line for line in lines)
    assert "bu hafta 640, geçen hafta 200" in lines[-2] and "artış" in lines[-2]
