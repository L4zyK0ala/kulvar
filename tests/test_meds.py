"""İlaç/takviye saatleri, günleri, Telegram hatırlatmaları ve koç değerlendirmesinin ayrıştırılması."""
import datetime as dt

from app import meds
from app.models import Medication, MedicationLog

MONDAY = dt.date(2026, 10, 12)


def test_parse_times():
    assert meds.parse_times("8:00, 20.00 08:00;7:5 25:00 abc") == ["08:00", "20:00"]
    assert meds.parse_times(None) == []


def test_parse_days():
    assert meds.parse_days(["3", "0", "0"]) == "0,3"
    assert meds.parse_days([str(i) for i in range(7)]) is None  # her gün = boş
    assert meds.parse_days(["9", "x"]) is None


def test_is_due_day_weekdays_and_dates():
    med = Medication(name="B12", days="0,3")  # Pazartesi, Perşembe
    assert meds.is_due_day(med, MONDAY)
    assert not meds.is_due_day(med, MONDAY + dt.timedelta(days=1))
    assert meds.is_due_day(med, MONDAY + dt.timedelta(days=3))
    every_day = Medication(name="D", start_date=MONDAY, end_date=MONDAY + dt.timedelta(days=2))
    assert not meds.is_due_day(every_day, MONDAY - dt.timedelta(days=1))
    assert meds.is_due_day(every_day, MONDAY + dt.timedelta(days=1))
    assert not meds.is_due_day(every_day, MONDAY + dt.timedelta(days=3))


def test_parse_evaluation_blocks():
    text = """Genel olarak iyi.

<<<TAKVİYE
AD: D vitamini
KARAR: Gerekli
ZAMAN:
NEDEN: 25(OH)D düşük
>>>
<<<TAKVİYE
AD: Magnezyum
KARAR: doktora sor
ZAMAN: akşam
NEDEN: böbrek değerleri
>>>
<<<TAKVİYE
AD: Bilinmeyen
KARAR: belki
>>>"""
    summary, results = meds.parse_evaluation(text)
    assert summary == "Genel olarak iyi."
    assert [r["name"] for r in results] == ["D vitamini", "Magnezyum"]  # geçersiz karar atlanır
    assert results[0]["verdict"] == "needed"
    assert results[0]["timing"] is None  # boş ZAMAN sonraki satırı yutmamalı
    assert results[0]["reason"] == "25(OH)D düşük"
    assert results[1]["verdict"] == "ask_doctor" and results[1]["timing"] == "akşam"


def _med(db, profile, **kw):
    profile.telegram_chat_id = "chat"
    med = Medication(profile_id=profile.id, name="Omega 3", times="08:00", **kw)
    db.add(med)
    db.commit()
    return med


def test_reminder_sent_once_in_window(db, profile, telegram):
    _med(db, profile)
    assert meds.run_reminders(dt.datetime.combine(MONDAY, dt.time(7, 59))) == 0  # henüz değil
    assert meds.run_reminders(dt.datetime.combine(MONDAY, dt.time(8, 0))) == 1
    assert meds.run_reminders(dt.datetime.combine(MONDAY, dt.time(8, 1))) == 0  # aynı saat ikinci kez gitmez
    assert len(telegram) == 1 and "Omega 3" in telegram[0][1]
    assert meds.run_reminders(dt.datetime.combine(MONDAY, dt.time(11, 0))) == 0  # geç kalma penceresi dışında


def test_reminder_respects_weekdays(db, profile, telegram):
    _med(db, profile, days="3")  # sadece Perşembe
    assert meds.run_reminders(dt.datetime.combine(MONDAY, dt.time(8, 0))) == 0
    assert meds.run_reminders(dt.datetime.combine(MONDAY + dt.timedelta(days=3), dt.time(8, 0))) == 1


def test_unanswered_reminder_becomes_missed_next_day(db, profile, telegram):
    med = _med(db, profile)
    meds.run_reminders(dt.datetime.combine(MONDAY, dt.time(8, 0)))
    meds.run_reminders(dt.datetime.combine(MONDAY + dt.timedelta(days=1), dt.time(0, 5)))
    db.expire_all()
    log = db.query(MedicationLog).filter_by(medication_id=med.id).one()
    assert log.status == "missed"


def test_respond_take_and_done(db, profile, telegram):
    med = _med(db, profile)
    meds.run_reminders(dt.datetime.combine(MONDAY, dt.time(8, 0)))
    db.expire_all()
    log = db.query(MedicationLog).filter_by(medication_id=med.id).one()
    assert meds.respond(db, log, "take") == "taken"
    assert meds.respond(db, log, "skip") == "done"  # cevaplanmış hatırlatma değişmez


def test_units_per_intake():
    assert meds.units_per_intake("2 kapsül") == 2
    assert meds.units_per_intake("yarım tablet") == 0.5
    assert meds.units_per_intake("1000 IU") == 1  # birim adet değil
    assert meds.units_per_intake(None) == 1


def test_end_date_from_pack_quantity():
    thursday = dt.date(2026, 10, 15)
    # 30 tablet, günde 2 alım, Pazartesi + Perşembe → 15 alım günü
    assert meds.end_date_for(30, "1 tablet", ["08:00", "20:00"], [0, 3], thursday) == dt.date(2026, 12, 3)
    assert meds.end_date_for(30, "1000 IU", ["08:00"], [], thursday) == thursday + dt.timedelta(days=29)
    assert meds.end_date_for(30, "yarım tablet", [], [], thursday) == thursday + dt.timedelta(days=59)
    assert meds.end_date_for(1, "2 tablet", [], [], thursday) is None  # tek alıma bile yetmez
    assert meds.end_date_for(None, "1 tablet", [], [], thursday) is None


def test_save_computes_end_date_when_missing(db, profile):
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    client.cookies.set("kulvar_profile", profile.id)
    form = {"name": "Demir", "dose": "1 tablet", "times": "08:00, 20:00", "days": ["0", "3"], "start_date": "2026-10-15", "quantity": "30"}
    assert client.post(f"/health/{profile.id}/meds", data=form, follow_redirects=False).status_code == 303
    db.expire_all()
    med = db.query(Medication).filter_by(name="Demir").one()
    assert med.quantity == 30 and med.end_date == dt.date(2026, 12, 3)
    form["end_date"] = "2026-11-01"  # elle girilen bitiş korunur
    form["med_id"] = med.id
    client.post(f"/health/{profile.id}/meds", data=form, follow_redirects=False)
    db.expire_all()
    assert db.get(Medication, med.id).end_date == dt.date(2026, 11, 1)
