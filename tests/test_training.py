"""Haftalık hedef takibi ve yarış dönemi fazları."""
import datetime as dt

import pytest

from app import training
from app.models import Activity, PlannedWorkout, Race

MONDAY = dt.date(2026, 10, 12)


def test_default_targets():
    t = training.targets(training.DEFAULT_WEEK)
    assert t == {"runs": 3, "strength": 2, "long_day": 5}  # Cumartesi uzun koşu


def _run(db, profile, day, km, source="garmin"):
    db.add(Activity(profile_id=profile.id, source=source, external_id=f"{source}-{day}", activity_type="running",
                    start_time=dt.datetime.combine(day, dt.time(7)), distance_m=km * 1000, duration_s=km * 360))
    db.commit()


def test_week_status_counts_runs_plans_and_done_marks(db, profile):
    _run(db, profile, MONDAY, 6)
    db.add_all([
        PlannedWorkout(profile_id=profile.id, date=MONDAY + dt.timedelta(days=1), title="Kuvvet", workout_type="strength", status="done"),
        PlannedWorkout(profile_id=profile.id, date=MONDAY + dt.timedelta(days=3), title="Kuvvet", workout_type="strength"),
        PlannedWorkout(profile_id=profile.id, date=MONDAY + dt.timedelta(days=5), title="Uzun koşu", workout_type="running", target_distance_m=14000),
    ])
    db.commit()
    st = training.week_status(db, profile, MONDAY, today=MONDAY + dt.timedelta(days=2))
    assert len(st["runs"]) == 1
    assert len(st["strength"]) == 1  # ✅ ile işaretlenen kayıtsız kuvvet sayılır
    assert st["missing_strength"] == 0  # 1 yapıldı + 1 planlı
    assert st["missing_runs"] == 1  # 1 yapıldı + 1 planlı uzun, hedef 3
    assert not st["missing_long"] and st["long_planned"] is not None


def test_garmin_and_strava_copy_counted_once(db, profile):
    _run(db, profile, MONDAY, 6, source="garmin")
    db.add(Activity(profile_id=profile.id, source="strava", external_id="s1", activity_type="Run",
                    start_time=dt.datetime.combine(MONDAY, dt.time(7, 0, 30)), distance_m=6010, duration_s=2160))
    db.commit()
    st = training.week_status(db, profile, MONDAY, today=MONDAY)
    assert len(st["runs"]) == 1


@pytest.mark.parametrize("days_to, phase", [(80, "build"), (9, "taper"), (3, "race_week"), (0, "race_week"), (-2, "recovery"), (120, None)])
def test_race_phase_half_marathon(db, profile, days_to, phase):
    db.add(Race(profile_id=profile.id, name="Yarı Maraton", date=MONDAY + dt.timedelta(days=days_to), distance_km=21.1, priority="A"))
    db.commit()
    result = training.race_phase(db, profile.id, MONDAY)
    assert (result or {}).get("phase") == phase


def test_b_race_does_not_start_race_period(db, profile):
    db.add(Race(profile_id=profile.id, name="10K", date=MONDAY + dt.timedelta(days=5), distance_km=10, priority="B"))
    db.commit()
    assert training.race_phase(db, profile.id, MONDAY) is None
