from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env(name: str, default: str | None = None) -> str | None:
    """KULVAR_<ad> ortam değişkeni; yoksa eski kurulumlardan kalan FITDASH_<ad>."""
    value = os.environ.get(f"KULVAR_{name}")
    return value if value is not None else os.environ.get(f"FITDASH_{name}", default)


class Settings:
    def __init__(self) -> None:
        key = env("ENCRYPTION_KEY")
        if not key:
            raise RuntimeError(
                "KULVAR_ENCRYPTION_KEY .env dosyasında tanımlı değil. Üretmek için: "
                "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )
        self.encryption_key = key
        self.database_url = env("DATABASE_URL", f"sqlite:///{BASE_DIR / 'kulvar.db'}"
        )
        self.host = env("HOST", "0.0.0.0")
        self.port = int(env("PORT", "8010"))
        self.sync_interval_minutes = int(env("SYNC_INTERVAL_MINUTES", "45"))
        self.sync_lookback_days = int(env("SYNC_LOOKBACK_DAYS", "14"))
        self.public_url = env("PUBLIC_URL", "http://localhost:8010").rstrip("/")
        self.strava_client_id = os.environ.get("STRAVA_CLIENT_ID", "")
        self.strava_client_secret = os.environ.get("STRAVA_CLIENT_SECRET", "")
        self.local_timezone = env("TIMEZONE", "Europe/Istanbul")
        # Kulvar Koç sohbeti (Claude API). Anahtar SDK tarafından ANTHROPIC_API_KEY'den okunur.
        self.coach_model = env("COACH_MODEL", "claude-opus-5-5")
        self.coach_effort = env("COACH_EFFORT", "medium")
        self.coach_monthly_budget_usd = float(env("COACH_MONTHLY_BUDGET_USD", "10"))
        # Telegram botu (TELEGRAM_BOT_TOKEN ortam değişkeninden okunur) ve akşam kontrolü
        self.telegram_api_base = env("TELEGRAM_API_BASE", "https://api.telegram.org")
        self.evening_check_hour = int(env("EVENING_CHECK_HOUR", "21"))


settings = Settings()
