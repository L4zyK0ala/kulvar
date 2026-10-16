"""Duman testleri: her sayfa iki dilde açılıyor mu, çeviri kataloğu eksiksiz mi, maliyet hesabı doğru mu."""
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import coach_chat
from app.main import app
from app.models import ExerciseMedia, PlannedWorkout, StrengthRoutine

ROOT = Path(__file__).resolve().parent.parent
PAGES = ["/", "/dashboard", "/activities", "/strength", "/nutrition", "/settings",
         "/coach-brief", "/coach-brief?tab=hedefler", "/coach-brief?tab=saglik", "/panel-onizleme"]


@pytest.fixture
def client(db, profile):
    c = TestClient(app)  # bağlam yöneticisi yok: zamanlayıcı ve Telegram başlatılmaz
    c.cookies.set("kulvar_profile", profile.id)
    return c


@pytest.mark.parametrize("lang", ["tr", "en"])
@pytest.mark.parametrize("path", PAGES)
def test_page_renders(client, path, lang):
    client.cookies.set("kulvar_lang", lang)
    r = client.get(path)
    assert r.status_code == 200, r.text[:500]


def test_strength_day_renders_exercise_cards(client, db, profile):
    db.add(ExerciseMedia(template_id="GS", title="Goblet Squat", frames=json.dumps(["uploads/exercise/GS/0.jpg", "uploads/exercise/GS/1.jpg"]),
                         garmin_category="SQUAT", garmin_name="GOBLET_SQUAT", mapped_by="auto"))
    routine = StrengthRoutine(profile_id=profile.id, hevy_id="1", title="Ev D1 (Alt vücut)",
                              exercises_json=json.dumps([{"template_id": "GS", "title": "Goblet Squat", "rest_seconds": 60, "sets": [{"reps": 12}] * 3}]))
    db.add(routine)
    db.add(PlannedWorkout(profile_id=profile.id, date=dt.date.today(), title="Kuvvet", workout_type="strength", notes="Ev D1: squat"))
    db.commit()
    html = client.get("/coach-brief").text
    assert "exercise-card" in html and "Goblet Squat" in html and "3×12" in html


def test_i18n_catalog_complete():
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_i18n.py")], capture_output=True, text=True,
                            env={**os.environ}, cwd=ROOT)
    assert result.returncode == 0, result.stdout[-2000:]


def test_cost_with_and_without_fallback_iterations():
    usage = SimpleNamespace(input_tokens=1_000_000, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    plain = coach_chat._cost(usage, "claude-opus-5-5")  # beta dışı çağrının Usage'ında iterations yok
    assert plain > 0
    usage.iterations = None
    assert coach_chat._cost(usage, "claude-opus-5-5") == plain
    step = SimpleNamespace(model="claude-opus-5-5", input_tokens=1_000_000, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    usage.iterations = [step, step]
    assert coach_chat._cost(usage, "claude-opus-5-5") == pytest.approx(2 * plain)
