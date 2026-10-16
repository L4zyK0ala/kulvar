"""Test ortamı: geçici SQLite veritabanı, rastgele şifreleme anahtarı, dış servis yok.

Ortam değişkenleri app içe aktarılmadan önce ayarlanır; .env'deki gerçek anahtarlar boş değerlerle
gölgelenir (python-dotenv var olan değişkenin üstüne yazmaz), böylece testler hiçbir zaman Claude,
Telegram, Garmin ya da Hevy'ye gerçek istek atmaz.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="kulvar-test-"))
os.environ["KULVAR_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
os.environ["KULVAR_DATABASE_URL"] = f"sqlite:///{TMP / 'test.db'}"
os.environ["KULVAR_BACKUP_DIR"] = str(TMP / "backups")
for name in ("ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "KULVAR_BACKUP_COPY_DIR", "STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET"):
    os.environ[name] = ""


@pytest.fixture
def db():
    """Her test boş bir veritabanıyla başlar."""
    from app.db import Base, SessionLocal, engine, init_db

    Base.metadata.drop_all(engine)
    init_db()
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture
def profile(db):
    from app.models import Profile

    p = Profile(name="Deniz", language="tr")
    db.add(p)
    db.commit()
    return p


@pytest.fixture
def telegram(monkeypatch):
    """Telegram'a gidecek mesajları kaydeder: [(profil adı, metin, butonlar)]."""
    from app import telegram_bot

    sent: list[tuple] = []
    monkeypatch.setattr(telegram_bot, "token_for", lambda profile: "test-token")
    monkeypatch.setattr(telegram_bot, "send_to_profile",
                        lambda profile, text, buttons=None: sent.append((profile.name, text, buttons)) or str(len(sent)))
    return sent
