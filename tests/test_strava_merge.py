"""Hevy + Garmin → Strava'da tek kuvvet kaydı (başlık ve açıklamaya Hevy ayrıntıları)."""
import datetime as dt
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import strava_merge as sm
from app.crypto import encrypt_payload
from app.integrations import strava
from app.main import app
from app.models import Activity, Credential, StravaEnrichment

NOW = dt.datetime(2026, 10, 14, 15, 0)
HEVY = {"title": "Ev D1 (Alt vücut + itiş)", "exercises": [
    {"title": "Warm Up", "sets": [{"type": "normal", "duration_seconds": 300}]},
    {"title": "Goblet Squat", "sets": [{"type": "warmup", "reps": 15, "weight_kg": 2}] + [{"type": "normal", "reps": 12, "weight_kg": 5}] * 3},
    {"title": "Push Up", "sets": [{"type": "normal", "reps": 10}] * 3},
]}


class FakeStrava:
    def __init__(self, details):
        self.details, self.puts = details, []

    def get_activity(self, i):
        return self.details[i]

    def update_activity(self, i, **kw):
        self.puts.append((i, kw))
        self.details[i].description = kw["description"]


@pytest.fixture
def workout(db, profile):
    db.add(Activity(profile_id=profile.id, source="hevy", external_id="h1", activity_type=HEVY["title"],
                    start_time=dt.datetime(2026, 10, 13, 19, 21), duration_s=37 * 60, raw_json=json.dumps(HEVY)))
    db.commit()


def _strava(db, profile, ext, start, minutes=30, kind="WeightTraining"):
    db.add(Activity(profile_id=profile.id, source="strava", external_id=ext, activity_type=kind, start_time=start, duration_s=minutes * 60))
    db.commit()


def test_describe():
    title, text = sm.describe(Activity(raw_json=json.dumps(HEVY)), "tr")
    assert title == "Ev D1 (Alt vücut + itiş)"
    assert "• Goblet Squat: 3×12 · 5 kg" in text and "• Push Up: 3×10" in text
    assert "6 set · 180 kg toplam hacim" in text  # ısınma hareketi ve ısınma setleri sayılmaz
    assert text.endswith("· Kulvar")
    assert "6 sets · 180 kg total volume" in sm.describe(Activity(raw_json=json.dumps(HEVY)), "en")[1]


def test_garmin_upload_gets_hevy_details_once(db, profile, workout):
    _strava(db, profile, "100", dt.datetime(2026, 10, 13, 19, 28))  # saat Hevy'den 7 dk sonra başlatılmış
    client = FakeStrava({100: SimpleNamespace(external_id="garmin_ping_1", description=None)})
    assert sm.enrich_recent(db, profile, client, NOW) == 1
    assert client.puts[0][1]["name"] == "Ev D1 (Alt vücut + itiş)"
    assert sm.enrich_recent(db, profile, client, NOW) == 0  # aynı içerik tekrar yazılmaz


def test_edited_hevy_workout_is_rewritten(db, profile, workout):
    _strava(db, profile, "100", dt.datetime(2026, 10, 13, 19, 28))
    client = FakeStrava({100: SimpleNamespace(external_id="garmin_ping_1", description=None)})
    sm.enrich_recent(db, profile, client, NOW)
    hevy = db.query(Activity).filter_by(source="hevy").one()
    hevy.raw_json = json.dumps({**HEVY, "title": "Ev D1 (düzenlendi)"})
    db.commit()
    assert sm.enrich_recent(db, profile, client, NOW) == 1
    assert client.puts[-1][1]["name"] == "Ev D1 (düzenlendi)"


def test_hevy_copy_and_user_text_untouched(db, profile, workout):
    _strava(db, profile, "100", dt.datetime(2026, 10, 13, 19, 21))
    _strava(db, profile, "101", dt.datetime(2026, 10, 13, 19, 25))
    client = FakeStrava({100: SimpleNamespace(external_id="hevy_upload_1.json", description="Hevy"),
                         101: SimpleNamespace(external_id="garmin_ping_2", description="Bugün yorgundum")})
    assert sm.enrich_recent(db, profile, client, NOW) == 0 and client.puts == []
    assert db.get(StravaEnrichment, "100").status == "hevy_copy"
    assert db.get(StravaEnrichment, "101").status == "user_text"


def test_no_match_for_runs_or_other_times(db, profile, workout):
    _strava(db, profile, "200", dt.datetime(2026, 10, 13, 19, 21), kind="Run")
    _strava(db, profile, "201", dt.datetime(2026, 10, 13, 7, 0))
    client = FakeStrava({})
    assert sm.enrich_recent(db, profile, client, NOW) == 0


def test_scope_and_settings_button(db, profile):
    assert not strava.can_write({})  # eski bağlantı: izin bilgisi yok
    assert strava.can_write({"scope": "read,activity:write,activity:read_all"})
    db.add(Credential(profile_id=profile.id, source="strava", encrypted_payload=encrypt_payload({"access_token": "x"}), last_sync_status="ok"))
    db.commit()
    client = TestClient(app)
    client.cookies.set("kulvar_profile", profile.id)
    assert f"/settings/{profile.id}/strava/authorize" in client.get("/settings").text


def test_same_strava_account_cannot_be_linked_to_two_profiles(db, profile, monkeypatch):
    from app.models import Profile
    from app.routers import settings as settings_router
    other = Profile(name="Ece", language="tr")
    db.add(other)
    db.commit()
    db.add(Credential(profile_id=other.id, source="strava", encrypted_payload=encrypt_payload({"athlete_id": 42})))
    db.commit()
    mine = Credential(profile_id=profile.id, source="strava", encrypted_payload=encrypt_payload({"athlete_id": 7}))
    db.add(mine)
    db.commit()
    assert "başka bir profile" in settings_router._strava_account_conflict(db, profile.id, mine, 42)
    assert "farklı bir Strava" in settings_router._strava_account_conflict(db, profile.id, mine, 99)
    assert settings_router._strava_account_conflict(db, profile.id, mine, 7) is None  # aynı hesapla izin yükseltme


def test_strength_page_compact_sets(db, profile):
    db.add(Activity(profile_id=profile.id, source="hevy", external_id="h1", activity_type=HEVY["title"],
                    start_time=dt.datetime(2026, 10, 13, 19, 21), duration_s=37 * 60, raw_json=json.dumps(HEVY)))
    db.commit()
    client = TestClient(app)
    client.cookies.set("kulvar_profile", profile.id)
    html = client.get("/strength").text
    assert "3×12 · 5 kg" in html and "1×300 sn" in html
    assert "6 set" in html  # ısınma hareketi/setleri çalışma setine sayılmaz (Strava açıklamasıyla aynı)
    client.cookies.set("kulvar_lang", "en")
    assert "1×300 s" in client.get("/strength").text
