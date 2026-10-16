"""Koç notları ve talimat değişikliği önerileri (onaylı)."""
from app import coach_memory

REPLY = """Bu hafta 2 koşu kaldı, Perşembe'ye yerleştirdim.

<<<KOÇ NOTLARI
- Sol Aşil hassas; tempo sonrası kontrol et.
- 2x5 kg dambıl var.
>>>

<<<TALİMAT DEĞİŞİKLİĞİ
ESKİ: Haftada 2 kuvvet.
YENİ: Haftada 2 kuvvet; ev rutinleri D1-D3 dönüşümlü.
NEDEN: Ev rutinleri eklendi.
>>>
<<<TALİMAT DEĞİŞİKLİĞİ
ESKİ: Talimatta olmayan metin
YENİ: x
NEDEN: y
>>>"""


def test_extract_strips_blocks():
    clean, notes, proposals = coach_memory.extract(REPLY)
    assert clean == "Bu hafta 2 koşu kaldı, Perşembe'ye yerleştirdim."
    assert notes.startswith("- Sol Aşil hassas")
    assert len(proposals) == 2
    assert proposals[0] == {"old": "Haftada 2 kuvvet.", "new": "Haftada 2 kuvvet; ev rutinleri D1-D3 dönüşümlü.",
                            "reason": "Ev rutinleri eklendi."}


def test_proposals_need_exact_single_match(db, profile):
    profile.coach_instructions = "Koşucu.\nHaftada 2 kuvvet."
    db.commit()
    _, _, proposals = coach_memory.extract(REPLY)
    created = coach_memory.add_proposals(db, profile, proposals)
    assert len(created) == 1  # talimatta bulunmayan ESKİ metin öneri olmaz
    assert coach_memory.decide(db, profile, created[0], approve=True) == "applied"
    assert profile.coach_instructions == "Koşucu.\nHaftada 2 kuvvet; ev rutinleri D1-D3 dönüşümlü."
    assert coach_memory.decide(db, profile, created[0], approve=True) == "done"  # ikinci kez uygulanmaz


def test_stale_proposal_is_not_applied(db, profile):
    profile.coach_instructions = "Haftada 2 kuvvet."
    db.commit()
    rev = coach_memory.add_proposals(db, profile, [{"old": "Haftada 2 kuvvet.", "new": "Haftada 3 kuvvet.", "reason": ""}])[0]
    profile.coach_instructions = "Kullanıcı talimatı elle değiştirdi."
    db.commit()
    assert coach_memory.decide(db, profile, rev, approve=True) == "stale"
    assert profile.coach_instructions == "Kullanıcı talimatı elle değiştirdi."


def test_process_reply_saves_notes_and_revert(db, profile):
    result = coach_memory.process_reply(db, profile, REPLY)
    assert profile.coach_notes.startswith("- Sol Aşil")
    assert "KOÇ NOTLARI" not in result.get("text", result.get("display", ""))
    rev = coach_memory.history(db, profile)[0]
    coach_memory.revert(db, profile, rev)
    assert profile.coach_notes is None


def test_same_notes_do_not_create_revision(db, profile):
    assert coach_memory.set_notes(db, profile, "Not") is not None
    assert coach_memory.set_notes(db, profile, "Not") is None
