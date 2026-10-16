"""Kulvar'ı hesap bağlamadan denemek için kurgusal demo verisi üretir.

İki hayali profil (Deniz, Ece) için son ~4 ayın koşu/kuvvet antrenmanlarını, günlük metrikleri,
beslenme kayıtlarını, vücut ölçümlerini, planlı antrenmanları, bir hedef yarışı ve örnek bir koç
sohbetini oluşturur. Ayrıca: ev rutinleri ve Kulvar kuvvet kaydı (hareket görselleri free-exercise-db'den
indirilir), kademeli yüklenme önerisi, antrenman sonrası his (RPE), nabız bölgeleri, takviyeler, kan
değerlerinin seyri ve sabah hazırlık önerisi. Hiçbir gerçek kişiye ait veri içermez; GPS rotası sentetik bir döngüdür.

Kullanım (boş bir veritabanıyla; --lang en profilleri ve örnek metinleri İngilizce yapar):
    KULVAR_DATABASE_URL=sqlite:///demo.db python scripts/seed_demo.py [--lang en]
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if not os.environ.get("KULVAR_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet

    os.environ["KULVAR_ENCRYPTION_KEY"] = Fernet.generate_key().decode()  # demo'da kimlik bilgisi saklanmaz

from app import exercise_catalog, heart, progression  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import (  # noqa: E402
    Activity, ActivityStream, BodyMeasurement, CoachMessage, CoachThread, DailyMetric, ExerciseMedia, HealthRecord,
    HealthSchedule, LabResult, Medication, MorningCheck, NutritionLog, PlannedWorkout, Profile, Race, StrengthRoutine,
    StrengthSet, StrengthWorkout, WorkoutFeedback,
)

rng = random.Random(42)
LANG = "en" if "--lang" in sys.argv and sys.argv[sys.argv.index("--lang") + 1:][:1] == ["en"] else "tr"
TX = {
    "tr": {
        "lower_push": "Alt Vücut + İtiş", "lower_pull": "Alt Vücut + Çekiş",
        "instructions": "{name} için örnek koç talimatı: kolay koşularda nabız 150 altında; haftada 3 koşu (Cumartesi uzun) + 2 kuvvet; ağrı olursa yükü azalt.",
        "easy_note": "Nabız 150 altı, rahat tempo", "long_note": "Son 2 km biraz hızlan", "strength_note": "2-3 tekrar rezerv",
        "home_d1": "Ev D1 (Alt vücut + itiş)", "home_d2": "Ev D2 (Kalça + çekiş)", "tempo": "Friday Tempo",
        "tempo_note": "15 dk ısınma, 3×8 dk eşik temposu",
        "coach_notes": "- Sağ baldır uzun koşudan sonra hafif gergin; esnemeyi atlamasın.\n- Evde 5 ve 10 kg dambıl, 12 kg kettlebell var.\n- Yarı maraton hedefi 1:59 altı; uzun koşular kolay tempoda.",
        "rpe_note": "Son km zorladı, sıcak vardı",
        "eval_summary": "D vitamini tahlilde düşük çıktığı için takviye mantıklı; omega 3 için önce doktoruna danış.",
        "blood_summary": "## Özet\n- D vitamini düşükten normale çıktı.\n- LDL hâlâ referansın biraz üstünde.",
        "blood_coach": "Antrenmana etkisi yok, mevcut kurallar geçerli.",
        "vit_d": "D vitamini", "magnesium": "Magnezyum", "capsule": "1 kapsül",
        "doctor_note": "Örnek doktor notu: genel kontrol normal, antrenmana kısıtlama yok.",
        "doctor_coach": "Doktor kontrolü normal; antrenman kısıtı yok.",
        "race_a": "Demo Yarı Maratonu", "race_a_goal": "1:59 altı", "race_a_notes": "Düz parkur, sabah 08:00 start", "race_b": "Bahar 10K",
        "question": "Cumartesi uzun koşusunu nasıl geçirdim? Bu hafta neye dikkat edeyim?",
        "answer": "Güzel bir uzun koşuydu 👏\n\n- **Tempo ve nabız:** ilk yarıda nabız 150 civarında, ikinci yarıda kayma %4 — "
                  "aerobik taban iyi gidiyor.\n- **Kadans:** ortalama 171 spm, kolay koşu için yerinde.\n- **Hafta durumu:** 3 koşu "
                  "hedefinin 2'si yapıldı, kuvvet 1/2.\n\nBu hafta Salı kuvveti kaçırma; Cumartesi uzun koşuyu 1 km uzatabiliriz:\n\n"
                  "{sat} | running | Saturday Long | 13 | | ilk 10 km rahat, son 3 km yarış temposuna yakın",
        "done": "Demo verisi yüklendi: {n} aktivite, profiller: Deniz, Ece",
        "not_empty": "Veritabanı boş değil; demo verisi yalnızca boş bir veritabanına yüklenir.",
    },
    "en": {
        "lower_push": "Lower Body + Push", "lower_pull": "Lower Body + Pull",
        "instructions": "Sample coach instructions for {name}: keep easy runs under 150 bpm; 3 runs a week (long run on Saturday) + 2 strength sessions; reduce the load if anything hurts.",
        "easy_note": "Under 150 bpm, easy pace", "long_note": "Pick it up a little in the last 2 km", "strength_note": "Leave 2-3 reps in reserve",
        "home_d1": "Home D1 (Lower body + push)", "home_d2": "Home D2 (Hips + pull)", "tempo": "Friday Tempo",
        "tempo_note": "15 min warm-up, 3×8 min at threshold",
        "coach_notes": "- Right calf a bit tight after long runs; don't skip the stretching.\n- At home: 5 and 10 kg dumbbells, a 12 kg kettlebell.\n- Half marathon goal sub 1:59; long runs at an easy pace.",
        "rpe_note": "Last km felt hard, it was hot",
        "eval_summary": "Vitamin D was low in the blood test, so a supplement makes sense; ask your doctor before starting omega-3.",
        "blood_summary": "## Summary\n- Vitamin D went from low to normal.\n- LDL is still slightly above the reference.",
        "blood_coach": "No effect on training, current rules apply.",
        "vit_d": "Vitamin D", "magnesium": "Magnesium", "capsule": "1 capsule",
        "doctor_note": "Sample doctor's note: general check-up normal, no training restrictions.",
        "doctor_coach": "Check-up normal; no training restrictions.",
        "race_a": "Demo Half Marathon", "race_a_goal": "sub 1:59", "race_a_notes": "Flat course, 8:00 am start", "race_b": "Spring 10K",
        "question": "How did my Saturday long run go? What should I focus on this week?",
        "answer": "Nice long run 👏\n\n- **Pace and heart rate:** around 150 bpm in the first half, 4% drift in the second half — "
                  "your aerobic base is coming along.\n- **Cadence:** 171 spm on average, right where it should be for an easy run.\n"
                  "- **This week:** 2 of 3 runs done, strength 1/2.\n\nDon't skip Tuesday's strength session; we can add 1 km to Saturday's long run:\n\n"
                  "{sat} | running | Saturday Long | 13 | | first 10 km easy, last 3 km close to race pace",
        "done": "Demo data loaded: {n} activities, profiles: Deniz, Ece",
        "not_empty": "The database isn't empty; demo data only loads into an empty database.",
    },
}[LANG]
TODAY = dt.date.today()
DAYS = 120
WEEKDAYS_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _pace_run(profile_id: str, day: dt.date, km: float, pace_s: float, hr: float, hour: int, idx: int) -> Activity:
    duration = km * pace_s
    return Activity(
        profile_id=profile_id, source="garmin", external_id=f"demo-run-{profile_id[:6]}-{idx}",
        activity_type="running", start_time=dt.datetime.combine(day, dt.time(hour, rng.randint(0, 40))),
        duration_s=duration, distance_m=km * 1000, calories=km * 68, avg_hr=hr, max_hr=hr + rng.randint(14, 22),
        elevation_gain_m=round(km * rng.uniform(3, 7)),
    )


def _stream(km: float, pace_s: float, hr: float) -> dict:
    """Sentetik bir park döngüsü: eşit aralıklı zaman serisi, km başına hafif tempo/nabız değişimi."""
    center_lat, center_lon = 40.7812, -73.9665
    points = int(km * pace_s / 3)
    data = {k: [] for k in ("time", "distance", "latlng", "altitude", "heartrate", "cadence")}
    dist = 0.0
    wobble = 0.0
    for i in range(points):
        t = i * 3
        speed = 1000 / (pace_s * (1 + 0.03 * math.sin(i / 90)))
        dist += speed * 3
        angle = dist / 3800 * 2 * math.pi  # ~3,8 km'lik oval tur
        data["time"].append(t)
        data["distance"].append(round(dist, 1))
        data["latlng"].append([round(center_lat + 0.0105 * math.sin(angle), 6), round(center_lon + 0.0042 * math.cos(angle), 6)])
        # Turda bir yokuş + düzlük; bir sonraki noktaya yumuşak geçiş.
        data["altitude"].append(round(30 + 4 * max(0.0, math.sin(angle)) ** 2 + 0.8 * math.sin(angle * 5), 1))
        wobble = 0.92 * wobble + rng.uniform(-0.6, 0.6)  # yumuşatılmış rastgele dalgalanma
        climb = 3 * max(0.0, math.sin(angle - 0.3)) ** 2  # yokuşta nabız biraz yükselir
        drift = 5 * i / points  # ikinci yarıda hafif nabız kayması
        data["heartrate"].append(round(hr - 10 + 10 * min(1, i / 150) + drift + climb + wobble))
        data["cadence"].append(rng.choice([84, 85, 85, 86, 86, 87]))  # Strava gibi tek bacak
    return data


STRENGTH_DAYS = {
    "lower_push": [("Squat (Barbell)", 40, 8), ("Romanian Deadlift (Barbell)", 35, 10),
                          ("Bulgarian Split Squat", 10, 10), ("Bench Press (Dumbbell)", 14, 10), ("Calf Raise", 20, 15)],
    "lower_pull": [("Hip Thrust (Barbell)", 50, 10), ("Step Up (Dumbbell)", 10, 10),
                           ("Lat Pulldown (Cable)", 40, 10), ("Seated Row (Cable)", 35, 12), ("Plank", None, 1)],
}


def _strength(profile_id: str, day: dt.date, title: str, week_no: int, idx: int, scale: float) -> Activity:
    exercises = []
    for e_idx, (name, weight, reps) in enumerate(STRENGTH_DAYS[title]):
        w = None if weight is None else round(weight * scale + 2.5 * (week_no // 3), 1)
        exercises.append({
            "index": e_idx, "title": name,
            "sets": [{"index": s, "type": "normal", "weight_kg": w, "reps": reps if w else None,
                      "duration_seconds": 45 if w is None else None} for s in range(3)],
        })
    start = dt.datetime.combine(day, dt.time(19, rng.randint(0, 30)))
    raw = {"title": TX[title], "start_time": start.isoformat(), "exercises": exercises}
    return Activity(
        profile_id=profile_id, source="hevy", external_id=f"demo-hevy-{profile_id[:6]}-{idx}", activity_type=TX[title],
        start_time=start, duration_s=rng.randint(48, 62) * 60, calories=rng.randint(220, 320), raw_json=json.dumps(raw),
    )


def _z2(lthr: int) -> int:
    return heart.lthr_zones(lthr)[1]["hi"]


def seed_profile(db, name: str, color: str, gender: str, height: float, start_weight: float, goal: float,
                 easy_pace: float, base_hr: float, scale: float, adherence: float, lthr: int = 172) -> Profile:
    p = Profile(name=name, color=color, gender=gender, height_cm=height, weight_goal_kg=goal, language=LANG,
                coach_instructions=TX["instructions"].format(name=name), coach_notes=TX["coach_notes"],
                lthr=lthr, lthr_date=TODAY - dt.timedelta(days=12), hr_max=lthr + 20, hr_rest=round(base_hr / 2.9),
                hr_profile_source="garmin", hr_profile_updated_at=dt.datetime.now(), strength_source="kulvar",
                garmin_hr_zones=json.dumps({"floors": [round((lthr + 20) * f) for f in (0.5, 0.6, 0.7, 0.8, 0.9)], "method": "HR_MAX"}),
                home_equipment=json.dumps({"dumbbell": [5, 10], "kettlebell": [12]}))
    db.add(p)
    db.flush()

    idx = 0
    long_km = 8.0
    strength_days: list[dt.date] = []
    for offset in range(DAYS, -1, -1):
        day = TODAY - dt.timedelta(days=offset)
        wd = day.weekday()
        week_no = (DAYS - offset) // 7
        planned_today = day == TODAY
        if wd in (0, 2) and not planned_today and rng.random() < adherence:
            km = round(rng.uniform(5.5, 7.5), 2)
            run = _pace_run(p.id, day, km, easy_pace + rng.uniform(-10, 15), base_hr + rng.uniform(-3, 4), 7, idx)
            db.add(run); idx += 1
        if wd == 5 and not planned_today and rng.random() < adherence + 0.05:
            long_km = min(16.0, long_km + rng.choice([0.5, 1.0, 1.0, 0]))
            km = round(long_km + rng.uniform(-0.3, 0.3), 2)
            pace = easy_pace + 12
            run = _pace_run(p.id, day, km, pace, base_hr + 4, 8, idx)
            if offset < 14:
                stream = _stream(km, pace, base_hr + 4)
                run.stream = ActivityStream(point_count=len(stream["time"]), data_json=json.dumps(stream))
            db.add(run); idx += 1
        if wd in (1, 3) and not planned_today and rng.random() < adherence - 0.1:
            if offset <= 28:
                strength_days.append(day)  # son 4 hafta: Kulvar kuvvet kaydı (seed_strength)
            else:
                title = "lower_push" if wd == 1 else "lower_pull"
                db.add(_strength(p.id, day, title, week_no, idx, scale)); idx += 1

        weight = start_weight - (start_weight - goal) * 0.55 * (DAYS - offset) / DAYS + rng.uniform(-0.3, 0.3)
        sleep_h = rng.uniform(6.3, 8.1)
        db.add(DailyMetric(
            profile_id=p.id, date=day, source="garmin",
            steps=rng.randint(6500, 13500) if not planned_today else rng.randint(3500, 6000), steps_goal=8000,
            resting_hr=round(base_hr / 2.75 + rng.uniform(-2, 2)), sleep_score=round(55 + (sleep_h - 6) * 15 + rng.uniform(-5, 5)),
            sleep_duration_s=sleep_h * 3600, body_battery_min=rng.randint(10, 30), body_battery_max=rng.randint(65, 95),
            stress_avg=rng.randint(22, 40), weight_kg=round(weight, 1) if offset % 3 == 0 else None,
        ))
        if offset <= 60:
            cal_goal = 2100 if gender == "male" else 1750
            cal = cal_goal + rng.uniform(-300, 250)
            db.add(NutritionLog(
                profile_id=p.id, date=day, source="mfp", calories=round(cal), calories_goal=cal_goal,
                protein_g=round(cal * 0.27 / 4), protein_goal_g=round(cal_goal * 0.3 / 4),
                carbs_g=round(cal * 0.45 / 4), carbs_goal_g=round(cal_goal * 0.45 / 4),
                fat_g=round(cal * 0.28 / 9), fat_goal_g=round(cal_goal * 0.25 / 9),
                fiber_g=round(rng.uniform(18, 34)), fiber_goal_g=30, sugar_g=round(rng.uniform(25, 60)), sugar_goal_g=50,
                sodium_mg=round(rng.uniform(1600, 2600)), sodium_goal_mg=2300,
            ))
        if offset % 14 == 0:
            db.add(BodyMeasurement(
                profile_id=p.id, source="hevy", external_id=f"demo-m-{p.id[:6]}-{offset}", date=day,
                weight_kg=round(weight, 1), fat_percent=round(22 - 3 * (DAYS - offset) / DAYS + rng.uniform(-0.4, 0.4), 1),
                waist_cm=round((88 if gender == "male" else 74) - 3 * (DAYS - offset) / DAYS, 1),
                hips_cm=round((100 if gender == "male" else 98) - 1.5 * (DAYS - offset) / DAYS, 1),
                left_thigh_cm=round(57 + rng.uniform(-0.3, 0.3), 1), right_thigh_cm=round(57.4 + rng.uniform(-0.3, 0.3), 1),
            ))

    # Bu haftanın ve gelecek haftanın planı (normal haftalık düzen: Pzt/Çar koşu, Cmt uzun, Sal/Per kuvvet).
    week_start = TODAY - dt.timedelta(days=TODAY.weekday())
    layout = {0: ("running", "Easy", 6, None), 1: ("strength", "Strength", None, 50), 2: ("running", "Easy", 7, None),
              3: ("strength", "Strength", None, 50), 5: ("running", "Long", round(long_km + 1), None)}
    for w in range(2):
        for wd, (wtype, kind, km, mins) in layout.items():
            day = week_start + dt.timedelta(days=7 * w + wd)
            if day < TODAY:
                continue
            db.add(PlannedWorkout(
                profile_id=p.id, date=day, title=f"{WEEKDAYS_EN[wd]} {kind}", workout_type=wtype,
                target_distance_m=km * 1000 if km else None, target_duration_s=mins * 60 if mins else None,
                notes=TX["easy_note"] if kind == "Easy" else (TX["long_note"] if kind == "Long" else
                      f"{TX['home_d1' if wd == 1 else 'home_d2'].split(' (')[0]}: {TX['strength_note']}"),
                target_hr_max=_z2(lthr) if kind in ("Easy", "Long") else None,
                garmin_workout_id=f"demo-{rng.randint(1000, 9999)}" if wtype == "running" else None,
            ))

    for kind, months, ago in (("blood", 6, 130), ("cardio", 24, 300)):
        db.add(HealthSchedule(profile_id=p.id, kind=kind, interval_months=months, last_date=TODAY - dt.timedelta(days=ago)))
    db.add(HealthRecord(profile_id=p.id, kind="doctor_note", record_date=TODAY - dt.timedelta(days=40), source="web",
                        text_note=TX["doctor_note"], coach_note=TX["doctor_coach"]))
    p._strength_days = strength_days
    return p


# --------------------------------------------------------------------------- kuvvet, his, sağlık

# (free-exercise-db kimliği, set, tekrar ya da süre, ağırlık) — süreli hareket (TIMED): tekrar yerine saniye
HOME_ROUTINES = {
    "home_d1": [("Goblet_Squat", 3, 12, 12), ("Split_Squat_with_Dumbbells", 3, 10, 10), ("Pushups", 3, 12, None),
                ("Standing_Dumbbell_Calf_Raise", 3, 15, 10), ("Plank", 3, 40, None)],
    "home_d2": [("Stiff-Legged_Dumbbell_Deadlift", 3, 12, 10), ("One-Arm_Dumbbell_Row", 3, 12, 10),
                ("Single_Leg_Glute_Bridge", 3, 12, None), ("Dumbbell_Step_Ups", 3, 10, 5), ("Dead_Bug", 3, 10, None)],
}
TIMED = {"Plank"}
ON_TARGET = {"Goblet_Squat", "One-Arm_Dumbbell_Row"}  # son iki seansta hedefte: kademeli yüklenme önerisi gelir


def _media(db, fedb_id: str) -> ExerciseMedia:
    """Katalogdan hareket (görseller indirilir, saatteki karşılığı bulunur); internet yoksa yalnız adıyla."""
    try:
        media = exercise_catalog.ensure(db, "fedb:" + fedb_id)
    except Exception:  # noqa: BLE001 - çevrimdışı kurulum: demo görselsiz de çalışır
        db.rollback()
        media = None
    if media is None:
        media = db.get(ExerciseMedia, "fedb:" + fedb_id)
    if media is None:
        media = ExerciseMedia(template_id="fedb:" + fedb_id, title=fedb_id.replace("_", " "), mapped_by="auto")
        db.add(media)
        db.flush()
    return media


def seed_strength(db, profile: Profile) -> None:
    """Ev rutinleri (Kulvar), son 4 haftanın Kulvar kuvvet kayıtları (saat kaydıyla) ve kademeli yüklenme önerisi."""
    routines, fedb_of = {}, {}
    for key, items in HOME_ROUTINES.items():
        exercises = []
        for fedb_id, sets, reps, kg in items:
            media = _media(db, fedb_id)
            fedb_of[media.template_id] = fedb_id
            one = {"type": "normal", "duration_seconds": reps} if fedb_id in TIMED else {"type": "normal", "reps": reps, "weight_kg": kg}
            exercises.append({"template_id": media.template_id, "title": media.title, "rest_seconds": 45 if fedb_id in TIMED else 60,
                              "notes": None, "sets": [dict(one) for _ in range(sets)]})
        r = StrengthRoutine(profile_id=profile.id, hevy_id=f"kulvar-demo-{key}-{profile.id[:6]}", title=TX[key],
                            exercises_json=json.dumps(exercises), updated_at=dt.datetime.now())
        db.add(r)
        routines[key] = r
    db.flush()
    last = None
    for n, day in enumerate(sorted(profile._strength_days)):
        routine = routines["home_d1" if day.weekday() == 1 else "home_d2"]
        start = dt.datetime.combine(day, dt.time(19, rng.randint(0, 20)))
        garmin = Activity(profile_id=profile.id, source="garmin", external_id=f"demo-str-{profile.id[:6]}-{n}",
                          activity_type="strength_training", start_time=start, duration_s=rng.randint(30, 38) * 60,
                          calories=rng.randint(150, 220), avg_hr=rng.randint(98, 112))
        db.add(garmin)
        w = StrengthWorkout(profile_id=profile.id, start_time=start, duration_s=garmin.duration_s, title=routine.title,
                            routine_id=routine.id, garmin_activity_id=garmin.external_id, status="confirmed", source="kulvar",
                            confirmed_at=start)
        for pos, e in enumerate(json.loads(routine.exercises_json)):
            for i, s in enumerate(e["sets"]):
                hit = fedb_of[e["template_id"]] in ON_TARGET or i < 2  # diğerlerinde son set hedefin biraz altında
                common = {"position": pos, "set_index": i, "title": e["title"], "template_id": e["template_id"], "rest_s": e["rest_seconds"],
                          "done_at": start}
                if s.get("duration_seconds"):
                    w.sets.append(StrengthSet(duration_s=s["duration_seconds"] - (0 if hit else 5), **common))
                else:
                    w.sets.append(StrengthSet(reps=s["reps"] - (0 if hit else 2), weight_kg=s.get("weight_kg"), **common))
        db.add(w)
        last = w
    db.commit()
    if last is not None:
        progression.check(db, profile, last)


def seed_feedback(db, profile: Profile) -> None:
    """Son 10 günün antrenmanlarına algılanan zorluk (RPE) ve bir not."""
    since = dt.datetime.combine(TODAY - dt.timedelta(days=10), dt.time.min)
    acts = db.query(Activity).filter(Activity.profile_id == profile.id, Activity.start_time >= since).order_by(Activity.start_time).all()
    for n, a in enumerate(acts):
        long_run = (a.distance_m or 0) > 10000
        db.add(WorkoutFeedback(profile_id=profile.id, ref=f"act:{a.id}", date=a.start_time.date(), title=a.activity_type,
                               minutes=(a.duration_s or 0) / 60, rpe=7 if long_run else rng.choice([4, 5, 5, 6]),
                               note=TX["rpe_note"] if long_run else None))
    db.commit()


def seed_health(db, profile: Profile, male: bool) -> None:
    """Takviyeler (koç değerlendirmesiyle) ve iki tahlil dönemine ait kan değerleri."""
    rows = [(TX["vit_d"], "active", "1000 IU", "09:00", None, "needed"),
            ("Omega 3", "considering", TX["capsule"], None, None, "ask_doctor"),
            (TX["magnesium"], "active", "1 tablet", "22:00", "0,3", "needed")]
    for name, status, dose, times, days, verdict in rows:
        db.add(Medication(profile_id=profile.id, name=name, kind="supplement", status=status, dose=dose, times=times, days=days,
                          reminders=1 if times else 0, start_date=TODAY - dt.timedelta(days=20), quantity=60 if times else None,
                          coach_verdict=verdict, coach_evaluated_at=dt.datetime.now()))
    profile.meds_eval_status, profile.meds_eval_summary, profile.meds_eval_at = "done", TX["eval_summary"], dt.datetime.now()
    old, new = TODAY - dt.timedelta(days=310), TODAY - dt.timedelta(days=130)
    record = HealthRecord(profile_id=profile.id, kind="blood", record_date=new, source="web", ai_summary=TX["blood_summary"],
                          coach_note=TX["blood_coach"], labs_extracted_at=dt.datetime.now())
    db.add(record)
    db.flush()
    values = [("ferritin", "Ferritin", "ng/mL", 30, 400, 38, 62), ("vitamin_d", "25-OH D", "ng/mL", 30, 100, 18, 34),
              ("b12", "B12", "pg/mL", 200, 900, 320, 410), ("alt", "ALT", "U/L", None, 41, 28, 31),
              ("hba1c", "HbA1c", "%", 4, 6, 5.4, 5.3), ("ldl", "LDL", "mg/dL", None, 100, 128, 112),
              ("hemoglobin", "Hemoglobin", "g/dL", 13.5 if male else 12, 17.5 if male else 15.5, 14.6 if male else 12.9, 14.9 if male else 13.2)]
    for key, name, unit, lo, hi, v_old, v_new in values:
        for day, v in ((old, v_old), (new, v_new)):
            db.add(LabResult(profile_id=profile.id, record_id=record.id, key=key, name=name, value=v, unit=unit,
                             ref_low=lo, ref_high=hi, date=day))
    db.commit()


def seed_morning(db, profile: Profile) -> None:
    """Bugün eşik koşusu + kötü bir gece: Panel'in en üstünde "kolaya çevir" önerisi."""
    plan = db.query(PlannedWorkout).filter_by(profile_id=profile.id, date=TODAY).first()
    if plan is None:
        plan = PlannedWorkout(profile_id=profile.id, date=TODAY, workout_type="running", title=TX["tempo"])
        db.add(plan)
    zones = heart.lthr_zones(profile.lthr)
    plan.workout_type, plan.title, plan.notes = "running", TX["tempo"], TX["tempo_note"]
    plan.target_distance_m, plan.target_duration_s = 9000, 50 * 60
    plan.target_hr_min, plan.target_hr_max = zones[2]["lo"], zones[3]["hi"]
    today = db.query(DailyMetric).filter_by(profile_id=profile.id, date=TODAY).first()
    base_rhr = today.resting_hr or 52
    today.sleep_score, today.resting_hr = 51, base_rhr + 7
    db.flush()
    db.add(MorningCheck(profile_id=profile.id, date=TODAY, status="sent", planned_workout_id=plan.id,
                        reasons_json=json.dumps([{"key": "sleep", "value": 51, "base": 76}, {"key": "rhr", "value": base_rhr + 7, "base": base_rhr}])))
    db.commit()


def seed_chat(db, profile: Profile) -> None:
    thread = CoachThread(profile_id=profile.id)
    db.add(thread)
    db.flush()
    sat = TODAY + dt.timedelta(days=(5 - TODAY.weekday()) % 7 or 7)
    turns = [
        ("user", TX["question"], None),
        ("assistant", TX["answer"].format(sat=sat.isoformat()), 0.087),
    ]
    for seq, (role, text, cost) in enumerate(turns):
        db.add(CoachMessage(
            thread_id=thread.id, profile_id=profile.id, seq=seq, role=role, channel="web",
            content_json=json.dumps([{"type": "text", "text": text}]), display_text=text,
            model="claude-opus-5-5" if role == "assistant" else None, cost_usd=cost,
            input_tokens=5200 if cost else None, output_tokens=640 if cost else None,
        ))


def main() -> None:
    init_db()
    db = SessionLocal()
    if db.query(Profile).count():
        sys.exit(TX["not_empty"])
    deniz = seed_profile(db, "Deniz", "#2f6fed", "male", 180, 84.0, 79.0, easy_pace=362, base_hr=146, scale=1.0, adherence=0.85, lthr=172)
    ece = seed_profile(db, "Ece", "#15996b", "female", 166, 61.0, 58.0, easy_pace=388, base_hr=150, scale=0.6, adherence=0.8, lthr=176)
    db.commit()
    for profile, male in ((deniz, True), (ece, False)):
        seed_strength(db, profile)
        seed_feedback(db, profile)
        seed_health(db, profile, male)
    seed_morning(db, deniz)
    db.add(Race(profile_id=deniz.id, date=TODAY + dt.timedelta(weeks=7), name=TX["race_a"], distance_km=21.1,
                priority="A", goal=TX["race_a_goal"], notes=TX["race_a_notes"]))
    db.add(Race(profile_id=ece.id, date=TODAY + dt.timedelta(weeks=16), name=TX["race_b"], distance_km=10, priority="B"))
    seed_chat(db, deniz)
    db.commit()
    print(TX["done"].format(n=db.query(Activity).count()))


if __name__ == "__main__":
    main()
