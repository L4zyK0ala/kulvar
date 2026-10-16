"""Kan değerlerinin seyri: çıkarılan sonuçların ayrıştırılması, saklanması, görünümü ve koç özeti."""
import datetime as dt
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import labs
from app.main import app
from app.models import HealthRecord, LabResult

D1, D2 = dt.date(2026, 7, 14), dt.date(2026, 10, 3)
CLAUDE = """İşte değerler:
```json
[{"key": "alt", "name": "ALT", "value": 83, "unit": "U/L", "ref_low": null, "ref_high": 41, "date": "2026-10-03"},
 {"key": "alt", "name": "ALT", "value": "73", "unit": "U/L", "ref_high": 41, "date": "2026-07-14"},
 {"key": "ferritin", "name": "Ferritin", "value": "45,2", "unit": "ng/mL", "ref_low": 30, "ref_high": 400, "date": "2026-10-03"},
 {"key": "nonsense", "name": "Lipaz", "value": 30, "unit": "U/L", "date": "bilinmiyor"},
 {"key": "hbsag", "name": "HBsAg", "value": "negatif", "date": "2026-10-03"},
 {"key": "alt", "name": "ALT", "value": 83, "date": "2026-10-03"}]
```"""


def _record(db, profile):
    r = HealthRecord(profile_id=profile.id, kind="blood", record_date=D2, file_path="x.pdf")
    db.add(r)
    db.commit()
    return r


def test_parse_cleans_rows():
    rows = labs.parse(CLAUDE, D2)
    keys = [(r["key"], r["date"], r["value"]) for r in rows]
    assert ("alt", D2, 83.0) in keys and ("alt", D1, 73.0) in keys
    assert ("ferritin", D2, 45.2) in keys  # virgüllü ondalık
    assert ("other:lipaz", D2, 30.0) in keys  # listede olmayan: other, tarihsiz: belgenin tarihi
    assert len(rows) == 4  # sayı olmayan ve tekrar eden satırlar atıldı
    assert labs.parse("çıkarılacak değer yok", D2) == []


def test_store_dedupes_across_records_and_view(db, profile):
    first, second = _record(db, profile), _record(db, profile)
    assert labs.store(db, profile, first, labs.parse(CLAUDE, D2)) == 4
    assert labs.store(db, profile, second, labs.parse(CLAUDE, D2)) == 0  # aynı değerler başka belgede tekrar
    assert labs.store(db, profile, first, labs.parse(CLAUDE, D2)) == 4  # aynı belge yeniden: önceki çıkarım silinir
    view = labs.view(db, profile.id, "tr")
    alt = next(c for c in view["main"] if c["key"] == "alt")
    assert alt["value"] == 83 and alt["flag"] == "high" and alt["change"] == 10 and alt["spark"]
    assert [c["key"] for c in view["main"]] == ["ferritin", "alt"]  # MARKERS sırası
    assert view["other"][0]["label"] == "lipaz"
    lines = labs.brief_lines(db, profile)
    assert any("ALT: 73 (14.07.2026) → 83 (03.10.2026) U/L, ref –41 ↑ yüksek" in line for line in lines)


def test_extract_marks_record_and_backfill_skips(db, profile, monkeypatch):
    from app import coach_chat, health

    record = _record(db, profile)
    monkeypatch.setattr(health, "content_blocks", lambda r: [{"type": "document"}])
    monkeypatch.setattr(coach_chat, "api_key_configured", lambda: True)
    calls = []

    class Stream:
        def __enter__(self):
            calls.append(1)
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            usage = SimpleNamespace(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0, cache_creation_input_tokens=0)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="[]")], usage=usage, model="claude-opus-5-5", stop_reason="end_turn")

    monkeypatch.setattr(labs.anthropic, "Anthropic", lambda: SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: Stream()))))
    labs.backfill()
    labs.backfill()  # sonuç 0 olsa da belge işlendi sayılır: ikinci açılışta tekrar ücret ödenmez
    assert len(calls) == 1
    db.refresh(record)
    assert record.labs_extracted_at is not None


def test_health_tab_shows_values(db, profile):
    labs.store(db, profile, _record(db, profile), labs.parse(CLAUDE, D2))
    client = TestClient(app)
    client.cookies.set("kulvar_profile", profile.id)
    page = client.get("/coach-brief?tab=saglik").text
    assert 'id="degerler"' in page and "Ferritin" in page and "↑ yüksek" in page and "Diğer değerler (1)" in page


def test_values_come_from_the_analysis_call(db, profile, monkeypatch):
    """Kan tahlili yorumu değerleri de döndürür: belgeler tek kez gönderilir, ayrı çıkarma çağrısı yapılmaz."""
    from app import coach_chat, health

    record = _record(db, profile)
    reply = ("TARİH: 2026-10-03\n## Özet\n- ALT yüksek\nKOÇ İÇİN NOT: Yoğunluğu artırma.\n\n"
             "<<<DEĞERLER\n" + json.dumps([{"key": "alt", "name": "ALT", "value": 83, "unit": "U/L", "ref_high": 55, "date": "2026-10-03"}]) + "\n>>>")
    prompts = []

    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            usage = SimpleNamespace(input_tokens=50000, output_tokens=3000, cache_read_input_tokens=0, cache_creation_input_tokens=0)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=reply)], usage=usage, model="claude-opus-5-5", stop_reason="end_turn")

    def stream(**kw):
        prompts.append(kw["messages"][0]["content"][-1]["text"])
        return Stream()

    monkeypatch.setattr(health, "content_blocks", lambda r: [{"type": "document"}])
    monkeypatch.setattr(health, "total_bytes", lambda r: 1000)
    monkeypatch.setattr(coach_chat, "api_key_configured", lambda: True)
    monkeypatch.setattr(health.anthropic, "Anthropic", lambda: SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=stream))))
    monkeypatch.setattr(labs, "extract", lambda *a: (_ for _ in ()).throw(AssertionError("ayrı çağrı yapılmamalı")))
    assert health.analyze_record(db, profile, record)["ok"]
    assert "<<<DEĞERLER" in prompts[0] and len(prompts) == 1
    db.refresh(record)
    assert "<<<" not in record.ai_summary and record.coach_note == "Yoğunluğu artırma."
    assert record.labs_extracted_at is not None
    assert db.query(LabResult).one().value == 83
