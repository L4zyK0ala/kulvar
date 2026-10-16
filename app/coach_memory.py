"""Koçun hafızası: kendi tuttuğu notlar (otomatik) ve talimat değişikliği önerileri (onaylı).

Koç cevabına iki tür işaretli blok ekleyebilir:

    <<<KOÇ NOTLARI
    ...notların tamamı (yeni sürüm)...
    >>>

    <<<TALİMAT DEĞİŞİKLİĞİ
    ESKİ: talimatlardaki metnin birebir kopyası (boşsa sona eklenir)
    YENİ: yerine geçecek metin (boşsa silinir)
    NEDEN: tek cümle
    >>>

Notlar hemen uygulanır; talimat önerileri kullanıcı onaylayana kadar bekler. Her değişiklik
InstructionRevision olarak tam metniyle saklanır ve geri alınabilir. Bloklar kullanıcıya
gösterilen metinden çıkarılır.
"""
from __future__ import annotations

import re

from sqlalchemy.orm import Session

from .clock import utcnow
from .models import InstructionRevision, Profile

NOTES_MAX_CHARS = 2500

_NOTES_RE = re.compile(r"<<<\s*KOÇ NOTLARI\s*\n(.*?)\n?\s*>>>", re.S | re.I)
_PROPOSAL_RE = re.compile(r"<<<\s*TAL[İI]MAT DE[ĞG][İI][ŞS][İI]KL[İI][ĞG][İI]\s*\n(.*?)\n?\s*>>>", re.S | re.I)
_FIELD_RE = re.compile(r"^(ESK[İI]|YEN[İI]|NEDEN)\s*:\s*", re.M | re.I)


def _fields(body: str) -> dict:
    """ESKİ/YENİ/NEDEN alanlarını (çok satırlı olabilir) ayırır."""
    parts = _FIELD_RE.split(body)
    out = {}
    for i in range(1, len(parts) - 1, 2):
        key = parts[i].upper().replace("İ", "I")
        out[{"ESKI": "old", "YENI": "new", "NEDEN": "reason"}[key]] = parts[i + 1].strip()
    return out


def extract(text: str) -> tuple[str, str | None, list[dict]]:
    """(blokları çıkarılmış metin, yeni notlar ya da None, öneriler)."""
    text = text or ""
    notes = None
    m = _NOTES_RE.search(text)
    if m:
        notes = m.group(1).strip()[:NOTES_MAX_CHARS] or None
    proposals = []
    for pm in _PROPOSAL_RE.finditer(text):
        f = _fields(pm.group(1))
        if f.get("new") or f.get("old"):
            proposals.append({"old": f.get("old", ""), "new": f.get("new", ""), "reason": f.get("reason", "")})
    clean = _PROPOSAL_RE.sub("", _NOTES_RE.sub("", text)).strip()
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    return clean, notes, proposals


def _record(db: Session, profile: Profile, kind: str, source: str, old_full, new_full, **extra) -> InstructionRevision:
    rev = InstructionRevision(profile_id=profile.id, kind=kind, source=source, status="applied",
                              old_full=old_full, new_full=new_full, decided_at=utcnow(), **extra)
    db.add(rev)
    return rev


def set_notes(db: Session, profile: Profile, notes: str | None, source: str = "coach") -> InstructionRevision | None:
    notes = (notes or "").strip()[:NOTES_MAX_CHARS] or None
    if notes == (profile.coach_notes or None):
        return None
    rev = _record(db, profile, "notes", source, profile.coach_notes, notes)
    profile.coach_notes, profile.coach_notes_updated_at = notes, utcnow()
    db.commit()
    return rev


def set_instructions(db: Session, profile: Profile, text: str | None, source: str = "user", reason: str | None = None) -> InstructionRevision | None:
    text = (text or "").strip() or None
    if text == (profile.coach_instructions or None):
        return None
    rev = _record(db, profile, "instructions", source, profile.coach_instructions, text, reason=reason)
    profile.coach_instructions = text
    db.commit()
    return rev


def _apply_snippet(current: str, old: str, new: str) -> str | None:
    """Önerideki değişikliği uygular; ESKİ metin tam bir kez geçmiyorsa None."""
    current = current or ""
    if not old.strip():
        return (current.rstrip() + "\n" + new.strip()).strip()
    if current.count(old) != 1:
        return None
    return current.replace(old, new).replace("\n\n\n", "\n\n").strip()


def add_proposals(db: Session, profile: Profile, proposals: list[dict]) -> list[InstructionRevision]:
    """Geçerli önerileri bekleyen revizyon olarak kaydeder; talimatta karşılığı olmayanları atlar."""
    created = []
    for p in proposals:
        if _apply_snippet(profile.coach_instructions or "", p["old"], p["new"]) is None:
            continue
        rev = InstructionRevision(profile_id=profile.id, kind="instructions", source="coach", status="pending",
                                  old_snippet=p["old"] or None, new_snippet=p["new"] or None, reason=p["reason"] or None)
        db.add(rev)
        created.append(rev)
    if created:
        db.commit()
    return created


def decide(db: Session, profile: Profile, rev: InstructionRevision, approve: bool) -> str:
    """Bekleyen öneriyi onaylar ya da reddeder: "applied" | "rejected" | "stale" | "done"."""
    if rev.profile_id != profile.id or rev.status != "pending":
        return "done"
    rev.decided_at = utcnow()
    if not approve:
        rev.status = "rejected"
        db.commit()
        return "rejected"
    new_full = _apply_snippet(profile.coach_instructions or "", rev.old_snippet or "", rev.new_snippet or "")
    if new_full is None:  # talimat bu arada değişmiş, metin artık bulunmuyor
        rev.status = "stale"
        db.commit()
        return "stale"
    rev.status, rev.old_full, rev.new_full = "applied", profile.coach_instructions, new_full
    profile.coach_instructions = new_full or None
    db.commit()
    return "applied"


def revert(db: Session, profile: Profile, rev: InstructionRevision) -> InstructionRevision | None:
    """Uygulanmış bir değişiklikten önceki tam metne döner (yeni bir revizyon olarak kaydedilir)."""
    if rev.profile_id != profile.id or rev.status != "applied":
        return None
    if rev.kind == "notes":
        return set_notes(db, profile, rev.old_full, source="revert")
    return set_instructions(db, profile, rev.old_full, source="revert", reason=None)


def process_reply(db: Session, profile: Profile, text: str) -> dict:
    """Koç cevabındaki blokları uygular. {"text": temiz metin, "notes_updated": bool, "proposals": [rev]}"""
    clean, notes, proposals = extract(text)
    notes_rev = set_notes(db, profile, notes, source="coach") if notes else None
    revs = add_proposals(db, profile, proposals) if proposals else []
    return {"text": clean, "notes_updated": notes_rev is not None, "proposals": revs}


def history(db: Session, profile: Profile, limit: int = 20) -> list[InstructionRevision]:
    return (db.query(InstructionRevision).filter(InstructionRevision.profile_id == profile.id)
            .order_by(InstructionRevision.created_at.desc()).limit(limit).all())


def pending(db: Session, profile: Profile) -> list[InstructionRevision]:
    return (db.query(InstructionRevision)
            .filter(InstructionRevision.profile_id == profile.id, InstructionRevision.status == "pending")
            .order_by(InstructionRevision.created_at).all())


def brief_lines(profile: Profile) -> list[str]:
    """Koç verisindeki notlar bölümü (talimatlar ve doktor notları her zaman önce gelir)."""
    if not profile.coach_notes:
        return ["## Koçun Notları", "- Henüz not yok. Haftalık değerlendirmede ilk notlarını yaz.", ""]
    stamp = f" (son güncelleme {profile.coach_notes_updated_at:%d.%m.%Y})" if profile.coach_notes_updated_at else ""
    return [f"## Koçun Notları{stamp}", profile.coach_notes.strip(), ""]
