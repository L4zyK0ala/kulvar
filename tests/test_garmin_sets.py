"""Garmin kuvvet setlerinin Hevy'deki tekrar/ağırlıklarla düzeltilmesi."""
import datetime as dt
import json

from app import garmin_sets
from app.models import Activity, ExerciseMedia, GarminSetSync


def _set(category, name, reps, grams, kind="ACTIVE"):
    return {"setType": kind, "exercises": [{"category": category, "name": name}], "repetitionCount": reps, "weight": grams, "duration": 60}


def _rest():
    return {"setType": "REST", "exercises": [], "repetitionCount": None, "weight": None, "duration": 60}


GARMIN = {"activityId": 1, "exerciseSets": [
    _set("UNKNOWN", None, 23, None),  # ısınma: saat rastgele tekrar saymış
    _set("LUNGE", "DUMBBELL_REVERSE_LUNGE", 16, 0.0), _rest(),
    _set("LUNGE", "DUMBBELL_REVERSE_LUNGE", 16, 5000.0), _rest(),
    _set("ROW", "DUMBBELL_ROW", 12, 10000.0), _rest(),
    _set("UNKNOWN", None, 14, 0.0),
]}
ITEMS = [
    {"title": "Reverse Lunge", "category": "LUNGE", "sets": [{"reps": 8, "weight_kg": 5}, {"reps": 8, "weight_kg": 5}]},
    {"title": "Dumbbell Row", "category": "ROW", "sets": [{"reps": 12, "weight_kg": 10}]},
    {"title": "Lateral Leg Raises", "category": "UNKNOWN", "sets": [{"reps": 15, "weight_kg": None}]},
]


def test_apply_hevy_values():
    payload = json.loads(json.dumps(GARMIN))
    changes = garmin_sets.apply_hevy(payload, ITEMS)
    sets = payload["exerciseSets"]
    assert sets[0]["repetitionCount"] == 23  # ısınma grubuna dokunulmaz
    assert [sets[1]["repetitionCount"], sets[1]["weight"], sets[3]["repetitionCount"]] == [8, 5000.0, 8]
    assert sets[5]["repetitionCount"] == 12 and sets[5]["weight"] == 10000.0  # zaten doğru
    assert sets[7]["repetitionCount"] == 15 and sets[7]["weight"] == 0.0  # vücut ağırlığı: ağırlık değişmez
    assert sets[2]["setType"] == "REST" and sets[2]["repetitionCount"] is None
    assert len(changes) == 4


def test_unmatched_exercise_is_skipped():
    payload = json.loads(json.dumps(GARMIN))
    items = [{"title": "Goblet Squat", "category": "SQUAT", "sets": [{"reps": 10, "weight_kg": 5}]}] + ITEMS
    garmin_sets.apply_hevy(payload, items)
    assert payload["exerciseSets"][1]["repetitionCount"] == 8  # squat yok ama sonraki hareketler yine eşleşir


class FakeGarmin:
    def __init__(self):
        self.puts = []

    def get_activity_exercise_sets(self, activity_id):
        return json.loads(json.dumps(GARMIN))

    def set_activity_exercise_sets(self, activity_id, payload):
        self.puts.append((activity_id, payload))


def test_fix_recent_writes_once(db, profile):
    hevy = {"exercises": [
        {"title": "Warm Up", "exercise_template_id": None, "sets": [{"type": "normal", "duration_seconds": 300}]},
        {"title": "Reverse Lunge", "exercise_template_id": "RL", "sets": [{"type": "normal", "reps": 8, "weight_kg": 5}] * 2},
    ]}
    db.add(ExerciseMedia(template_id="RL", title="Reverse Lunge", garmin_category="LUNGE", garmin_name="DUMBBELL_REVERSE_LUNGE", mapped_by="auto"))
    db.add(Activity(profile_id=profile.id, source="hevy", external_id="h1", activity_type="Ev D2", start_time=dt.datetime(2026, 10, 15, 19, 17),
                    duration_s=34 * 60, raw_json=json.dumps(hevy)))
    db.add(Activity(profile_id=profile.id, source="garmin", external_id="77", activity_type="strength_training",
                    start_time=dt.datetime(2026, 10, 15, 19, 12), duration_s=37 * 60))
    db.commit()
    client, now = FakeGarmin(), dt.datetime(2026, 10, 15, 22, 0)
    assert garmin_sets.fix_recent(db, profile, client, now, dry_run=True) and client.puts == []  # deneme yazmaz
    changes = garmin_sets.fix_recent(db, profile, client, now)
    assert len(changes) == 3 and len(client.puts) == 1  # 16→8, 0→5 kg, 16→8
    assert db.get(GarminSetSync, "77").changes == 3
    assert garmin_sets.fix_recent(db, profile, client, now) == [] and len(client.puts) == 1  # aynı içerik tekrar yazılmaz


def _step(order, kind, title="", category=None, name=None):
    return {"type": "ExecutableStepDTO", "stepOrder": order, "stepType": {"stepTypeKey": kind}, "description": title,
            "category": category, "exerciseName": name}


WORKOUT = {"workoutSegments": [{"workoutSteps": [
    _step(1, "warmup", "Warm Up"),
    {"type": "RepeatGroupDTO", "stepOrder": 2, "numberOfIterations": 2,
     "workoutSteps": [_step(3, "interval", "Reverse Lunge (Dumbbell)", "LUNGE", "DUMBBELL_REVERSE_LUNGE"), _step(4, "rest")]},
    {"type": "RepeatGroupDTO", "stepOrder": 5, "numberOfIterations": 2,
     "workoutSteps": [_step(6, "interval", "Lateral Leg Raises"), _step(7, "rest")]},
]}]}


def _wset(idx, category, reps, grams=None, kind="ACTIVE", name=None):
    return {"setType": kind, "wktStepIndex": idx, "exercises": [{"category": category, "name": name}],
            "repetitionCount": reps, "weight": grams, "duration": 50}


def test_fit_order_puts_repeat_marker_after_children():
    steps = garmin_sets.fit_order(WORKOUT)
    assert [(s.get("stepType") or {}).get("stepTypeKey") or s["type"] for s in steps] == [
        "warmup", "interval", "rest", "RepeatGroupDTO", "interval", "rest", "RepeatGroupDTO"]


def test_apply_by_steps_fixes_misdetected_and_removes_tail():
    payload = {"exerciseSets": [
        _wset(0, "PUSH_UP", 5),  # ısınma "şınav" sanılmış
        _wset(1, "LUNGE", 16, 0.0, name="DUMBBELL_REVERSE_LUNGE"), _wset(2, None, None, kind="REST"),
        _wset(1, "LUNGE", 16, 5000.0, name="DUMBBELL_REVERSE_LUNGE"), _wset(2, None, None, kind="REST"),
        _wset(4, "LATERAL_RAISE", 8), _wset(5, None, None, kind="REST"),
        _wset(4, "DEADLIFT", 11, name="BARBELL_DEADLIFT"), _wset(5, None, None, kind="REST"),
        _wset(None, None, None, kind="REST"), _wset(None, "UNKNOWN", 999, 0.0),  # antrenman bitince kalan kayıt
    ]}
    titles = {"Reverse Lunge (Dumbbell)": "RL", "Lateral Leg Raises": "LL"}
    hevy = {"RL": [{"reps": 8, "weight_kg": 5}] * 2, "LL": [{"reps": 15, "weight_kg": None}] * 2}
    changes = garmin_sets.apply_by_steps(payload, garmin_sets.fit_order(WORKOUT), titles, hevy, {"RL", "LL"})
    sets = payload["exerciseSets"]
    assert len(sets) == 9 and all(s.get("wktStepIndex") is not None for s in sets)  # sondaki 2 kayıt silindi
    assert sets[0]["exercises"][0]["category"] == "WARM_UP" and sets[0]["repetitionCount"] is None
    assert [sets[1]["repetitionCount"], sets[1]["weight"], sets[3]["repetitionCount"]] == [8, 5000.0, 8]
    assert [sets[i]["exercises"][0]["category"] for i in (5, 7)] == ["UNKNOWN", "UNKNOWN"]  # planlanan hareket
    assert [sets[i]["repetitionCount"] for i in (5, 7)] == [15, 15]
    assert any("anlamsız set silindi" in c for c in changes)


def test_tail_kept_when_hevy_has_extra_exercise():
    payload = {"exerciseSets": [_wset(1, "LUNGE", 8, 5000.0, name="DUMBBELL_REVERSE_LUNGE"), _wset(None, "CURL", 10, 8000.0)]}
    garmin_sets.apply_by_steps(payload, garmin_sets.fit_order(WORKOUT), {"Reverse Lunge (Dumbbell)": "RL"},
                               {"RL": [{"reps": 8, "weight_kg": 5}]}, {"RL", "BICEPS_CURL"})
    assert len(payload["exerciseSets"]) == 2  # Hevy'de planda olmayan bir hareket var: sondaki set gerçek olabilir


def test_turkish_warmup_and_cardio_titles():
    from app import workout_library as wl
    assert wl._is_cardio_or_warmup("Isınma") and wl._is_cardio_or_warmup("Koşu Bandı")
    assert not wl._is_cardio_or_warmup("Bent Over Row (Dambıl)")
