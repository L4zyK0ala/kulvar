"""Profil başına nabız profili: laktat eşiği (LTHR), nabız bölgeleri ve plan hedeflerinin çözümü.

LTHR ve saatteki bölgeler Garmin'den günde bir kez çekilir (elle girilen LTHR korunur). Plan
satırındaki NABIZ alanı "Z2" ya da "Z3-Z4" gibi bölge olarak da yazılabilir; bu, o profilin
LTHR bölgelerine göre bpm'e çevrilir — aynı satır her kişi için kendi değerleriyle uygulanır.

LTHR bölgeleri Joe Friel'in koşu bölgeleridir: Z1 <%85 · Z2 %85–89 · Z3 %90–94 · Z4 %95–99 · Z5 ≥%100.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re

from sqlalchemy.orm import Session

from .clock import utcnow
from .models import Profile

logger = logging.getLogger("kulvar.heart")

_LTHR_FLOORS = (0.85, 0.90, 0.95, 1.0)  # Z2..Z5 alt sınırları (LTHR oranı)


def _r(x: float) -> int:
    return int(x + 0.5)


def lthr_zones(lthr: int | None) -> list[dict]:
    """[{n, lo, hi}] — Z1'in altı ve Z5'in üstü açık (None)."""
    if not lthr:
        return []
    floors = [_r(lthr * f) for f in _LTHR_FLOORS]
    edges = [None, *floors, None]
    return [{"n": n, "lo": edges[n - 1], "hi": (edges[n] - 1) if edges[n] else None} for n in range(1, 6)]


def garmin_zones(profile: Profile) -> list[dict]:
    """Saatte tanımlı bölgeler (Garmin'den gelen alt sınırlar)."""
    try:
        floors = json.loads(profile.garmin_hr_zones or "{}").get("floors") or []
    except ValueError:
        return []
    if len(floors) != 5:
        return []
    return [{"n": n + 1, "lo": floors[n], "hi": (floors[n + 1] - 1) if n < 4 else None} for n in range(5)]


def garmin_zone_method(profile: Profile) -> str | None:
    try:
        return json.loads(profile.garmin_hr_zones or "{}").get("method")
    except ValueError:
        return None


def zone_label(z: dict) -> str:
    if z["lo"] is None:
        return f"<{z['hi'] + 1}"
    if z["hi"] is None:
        return f"≥{z['lo']}"
    return f"{z['lo']}–{z['hi']}"


_ZONE_RE = re.compile(r"^\s*z\s*([1-5])\s*(?:[-–]\s*z?\s*([1-5]))?\s*$", re.I)


def resolve_hr(raw: str | None, profile: Profile | None) -> tuple[int | None, int | None]:
    """NABIZ alanını (min, max) bpm'e çevirir: "155", "120-155", "Z2" (Z2 tavanı), "Z3-Z4" (aralık)."""
    from .routers.coach import parse_hr_target

    m = _ZONE_RE.match(raw or "")
    if not m:
        return parse_hr_target(raw or "")
    zones = lthr_zones(profile.lthr) if profile is not None else []
    if not zones:
        return None, None  # bölge yazılmış ama bu profilin LTHR'si yok
    a = int(m.group(1))
    b = int(m.group(2)) if m.group(2) else None
    if b is None:  # tek bölge: o bölgenin üst sınırı tavan
        hi = zones[a - 1]["hi"] or (zones[4]["lo"] + 10)
        return None, hi
    a, b = min(a, b), max(a, b)
    lo = zones[a - 1]["lo"] or zones[0]["hi"] - 20
    hi = zones[b - 1]["hi"] or (zones[4]["lo"] + 10)
    return lo, hi


def easy_floor(profile: Profile | None) -> int | None:
    """Yalnız tavan verilen hedefte Garmin'e gidecek alt sınır: saatin Z1 tabanı (kişiye özel)."""
    if profile is None:
        return None
    zones = garmin_zones(profile)
    return zones[0]["lo"] if zones else None


def refresh_from_garmin(client, profile: Profile, db: Session) -> str:
    """Garmin'den LTHR ve koşu nabız bölgelerini çeker; elle girilmiş LTHR'ye dokunmaz."""
    notes = []
    try:
        lt = (client.get_lactate_threshold(latest=True) or {}).get("speed_and_heart_rate") or {}
        hr = lt.get("heartRate")
        if hr and profile.hr_profile_source != "manual":
            profile.lthr = int(hr)
            date_raw = (lt.get("calendarDate") or "")[:10]
            profile.lthr_date = dt.date.fromisoformat(date_raw) if date_raw else None
            notes.append(f"LTHR {profile.lthr}")
    except Exception as exc:  # noqa: BLE001 - nabız profili senkronu durdurmasın
        logger.warning("Garmin LTHR okunamadı (%s): %s", profile.name, exc)
    try:
        zones = client.get_heart_rate_zones() or []
        running = next((z for z in zones if z.get("sport") == "RUNNING"), None) or next(
            (z for z in zones if z.get("sport") == "DEFAULT"), None)
        if running:
            floors = [running.get(f"zone{i}Floor") for i in range(1, 6)]
            if all(isinstance(f, (int, float)) for f in floors):
                profile.garmin_hr_zones = json.dumps({"floors": [int(f) for f in floors], "method": running.get("trainingMethod")})
            profile.hr_max = running.get("maxHeartRateUsed") or profile.hr_max
            profile.hr_rest = running.get("restingHeartRateUsed") or profile.hr_rest
            notes.append("bölgeler")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Garmin nabız bölgeleri okunamadı (%s): %s", profile.name, exc)
    if profile.hr_profile_source != "manual":
        profile.hr_profile_source = "garmin"
    profile.hr_profile_updated_at = utcnow()
    db.commit()
    return ", ".join(notes)


def needs_refresh(profile: Profile) -> bool:
    updated = profile.hr_profile_updated_at
    return updated is None or updated.date() < utcnow().date()


def brief_lines(profile: Profile) -> list[str]:
    """Koç özetindeki nabız bölümü (Türkçe; koç verisi)."""
    zones = lthr_zones(profile.lthr)
    if not zones:
        return []
    src = "elle girildi" if profile.hr_profile_source == "manual" else (
        f"Garmin, {profile.lthr_date:%d.%m.%Y} ölçümü" if profile.lthr_date else "Garmin")
    lines = ["## Nabız Bölgeleri",
             f"- Laktat eşiği nabzı (LTHR): {profile.lthr} bpm ({src})"
             + (f" · maks {profile.hr_max} · dinlenik {profile.hr_rest}" if profile.hr_max else ""),
             "- LTHR bölgeleri: " + " · ".join(f"Z{z['n']} {zone_label(z)}" for z in zones)]
    gz = garmin_zones(profile)
    if gz:
        lines.append(f"- Saatteki bölgeler ({garmin_zone_method(profile) or '?'}): " + " · ".join(f"Z{z['n']} {zone_label(z)}" for z in gz))
    lines.append("")
    return lines
