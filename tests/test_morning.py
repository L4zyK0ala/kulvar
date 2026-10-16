"""Sabah hazırlık kontrolü: kötü gece + zor koşu → günde bir öneri; kolaya çevir / planı koru."""
import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app import morning
from app.main import app
from app.models import DailyMetric, MorningCheck, PlannedWorkout

TODAY = dt.date(2026, 10, 16)


def _metrics(db, profile, today_values, base=None):
    base = base or {"sleep_score": 78, "resting_hr": 46, "body_battery_max": 70}
    for i in range(1, 8):
        db.add(DailyMetric(profile_id=profile.id, date=TODAY - dt.timedelta(days=i), source="garmin", **base))
    db.add(DailyMetric(profile_id=profile.id, date=TODAY, source="garmin", **today_values))
    db.commit()


def _plan(db, profile, title="Thursday Tempo", **kw):
    p = PlannedWorkout(profile_id=profile.id, date=TODAY, title=title, workout_type="running",
                       target_duration_s=50 * 60, target_distance_m=10000, **kw)
    db.add(p)
    db.commit()
    return p


def test_assess_points():
    base = {"sleep_score": 78, "resting_hr": 46, "body_battery_max": 70}
    assert morning.assess({"sleep_score": 52}, base)[0] == 2
    assert morning.assess({"sleep_score": 64, "resting_hr": 51}, base)[0] == 2  # 1 + 1
    assert morning.assess({"sleep_score": 74, "resting_hr": 48, "body_battery_max": 66}, base)[0] == 0
    assert morning.assess({"body_battery_max": 20}, {"body_battery_max": 25})[0] == 0  # güvenilmez Body Battery


def test_hard_plan_detection(profile):
    profile.lthr = 170
    assert morning.is_hard(PlannedWorkout(title="Thursday Tempo", workout_type="running"), profile)
    assert morning.is_hard(PlannedWorkout(title="Saturday Long", workout_type="running"), profile)
    assert morning.is_hard(PlannedWorkout(title="Koşu", workout_type="running", target_hr_min=160), profile)  # Z3+
    assert not morning.is_hard(PlannedWorkout(title="Monday Easy", workout_type="running", target_hr_max=150), profile)
    assert not morning.is_hard(PlannedWorkout(title="Tempo", workout_type="strength"), profile)
    # nottaki kelimeler başka şeylerden bahsedebilir: "eşin hızlıysa yavaşla", "yarın uzun koşu"
    assert not morning.is_hard(PlannedWorkout(title="Friday Easy", workout_type="running", notes="eşin hızlıysa yavaşla; yarın uzun koşu"), profile)
    assert morning.is_hard(PlannedWorkout(title="Koşu", workout_type="running", notes="Isınma + 6x400 m"), profile)


def test_bad_night_hard_run_sends_once_and_eases(db, profile, telegram):
    profile.telegram_chat_id, profile.lthr = "chat", 170
    _metrics(db, profile, {"sleep_score": 52, "resting_hr": 54, "body_battery_max": 60})
    plan = _plan(db, profile, target_hr_min=158, target_hr_max=165)
    assert morning.check(db, profile, TODAY) == 1
    assert morning.check(db, profile, TODAY) == 0  # günde bir kez
    assert len(telegram) == 1 and "mc:" in telegram[0][2][0][0]["callback_data"] and "Uyku skoru 52" in telegram[0][1]
    check = db.query(MorningCheck).one()
    assert morning.decide(db, profile, check, "ease") == "eased"
    db.refresh(plan)
    assert plan.target_duration_s == 35 * 60 and plan.target_distance_m == 7000
    assert plan.target_hr_min is None and plan.target_hr_max == 152  # Z2 tavanı
    assert plan.title.endswith("· kolay") and "Önceki plan: Thursday Tempo" in plan.notes
    assert morning.decide(db, profile, check, "ease") == "done"


def test_no_message_when_fine_or_easy_day(db, profile, telegram):
    profile.telegram_chat_id = "chat"
    _metrics(db, profile, {"sleep_score": 52, "resting_hr": 54})
    _plan(db, profile, title="Monday Easy")
    assert morning.check(db, profile, TODAY) == 0 and telegram == []
    assert db.query(MorningCheck).one().status == "none"


def test_waits_for_night_data(db, profile, telegram):
    _plan(db, profile)
    assert morning.check(db, profile, TODAY) == 0 and db.query(MorningCheck).count() == 0  # veri yok: bekle
    assert morning.check(db, profile, TODAY, final=True) == 0 and db.query(MorningCheck).count() == 1  # pencere bitti


def test_panel_card_and_keep(db, profile, telegram):
    profile.telegram_chat_id = "chat"
    _metrics(db, profile, {"sleep_score": 50})
    _plan(db, profile)
    morning.check(db, profile, TODAY)
    check = db.query(MorningCheck).one()
    check.date = dt.date.today()  # Panel bugünün kontrolünü gösterir
    db.commit()
    client = TestClient(app)
    client.cookies.set("kulvar_profile", profile.id)
    assert "Toparlanman bugün düşük görünüyor" in client.get("/dashboard").text
    client.post(f"/morning/{check.id}/keep")
    db.refresh(check)
    assert check.status == "kept"
    assert "Toparlanman bugün düşük görünüyor" not in client.get("/dashboard").text


def test_run_respects_window(db, profile):
    assert morning.run(dt.datetime(2026, 10, 16, 5, 30)) == 0
    assert morning.run(dt.datetime(2026, 10, 16, 12, 0)) == 0
