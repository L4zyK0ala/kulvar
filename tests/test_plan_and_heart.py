"""Koçun plan satırlarının ayrıştırılması ve nabız hedefleri (LTHR bölgeleri)."""
import datetime as dt

import pytest

from app import heart
from app.models import Profile
from app.routers.coach import hr_target_label, parse_hr_target, parse_plan_text

PLAN = """\
TARİH | TİP | BAŞLIK | MESAFE_KM | SÜRE_DK | NOT | NABIZ
---|---|---|---|---|---|---
2026-10-12 | koşu | Pazartesi kolay | 6 | 40 | Z2'de kal | 155
13.10.2026 | Kuvvet | | | 30 | D1 — alt vücut |
2026-10-17 | running | Uzun koşu | 14,5 | | | 120-150
yarın | koşu | geçersiz tarih | 5 | | |
Bu satırda boru yok
"""


def test_parse_plan_rows():
    rows, skipped = parse_plan_text(PLAN)
    assert skipped == 1  # "yarın" tarih değil
    assert [r["date"] for r in rows] == [dt.date(2026, 10, 12), dt.date(2026, 10, 13), dt.date(2026, 10, 17)]
    easy, strength, long_run = rows
    assert easy["workout_type"] == "running"
    assert easy["target_distance_m"] == 6000 and easy["target_duration_s"] == 2400
    assert (easy["target_hr_min"], easy["target_hr_max"]) == (None, 155)
    assert strength["workout_type"] == "strength"
    assert strength["title"]  # boş başlık türün etiketiyle dolar
    assert strength["notes"] == "D1 — alt vücut"
    assert long_run["target_distance_m"] == 14500  # virgüllü ondalık
    assert (long_run["target_hr_min"], long_run["target_hr_max"]) == (120, 150)


@pytest.mark.parametrize("raw, expected", [
    ("155", (None, 155)),
    ("120-155", (120, 155)),
    ("155–120", (120, 155)),  # ters sıra ve uzun tire
    ("150 bpm", (None, 150)),
    ("", (None, None)),
    ("tavan 155", (None, None)),
    ("30", (None, None)),  # mantıksız tavan
    ("150-152", (None, None)),  # 5 bpm'den dar aralık
])
def test_parse_hr_target(raw, expected):
    assert parse_hr_target(raw) == expected


def test_hr_target_label():
    class W:
        target_hr_min, target_hr_max = None, 155
    assert hr_target_label(W) == "♥ ≤155"
    W.target_hr_min = 120
    assert hr_target_label(W) == "♥ 120–155"


def test_lthr_zones_friel():
    zones = heart.lthr_zones(170)
    assert [z["n"] for z in zones] == [1, 2, 3, 4, 5]
    assert zones[0] == {"n": 1, "lo": None, "hi": 144}  # <%85
    assert (zones[1]["lo"], zones[1]["hi"]) == (145, 152)  # %85–89
    assert zones[4] == {"n": 5, "lo": 170, "hi": None}
    assert heart.lthr_zones(None) == []


def test_resolve_hr_zones_are_per_profile():
    deniz, ece = Profile(name="Deniz", lthr=173), Profile(name="Ece", lthr=165)
    assert heart.resolve_hr("Z2", deniz) == (None, heart.lthr_zones(173)[1]["hi"])
    assert heart.resolve_hr("Z2", ece) == (None, heart.lthr_zones(165)[1]["hi"])
    assert heart.resolve_hr("Z2", deniz) != heart.resolve_hr("Z2", ece)
    lo, hi = heart.resolve_hr("z3-z4", deniz)
    assert lo == heart.lthr_zones(173)[2]["lo"] and hi == heart.lthr_zones(173)[3]["hi"]
    assert heart.resolve_hr("Z2", Profile(name="no-lthr")) == (None, None)  # LTHR yoksa bölge çözülemez
    assert heart.resolve_hr("140-150", None) == (140, 150)  # bpm her zaman çalışır


def test_plan_zone_resolves_with_profile():
    rows, _ = parse_plan_text("2026-10-12 | koşu | Kolay | 6 | | | Z2", Profile(name="A", lthr=170))
    assert rows[0]["target_hr_max"] == 152
