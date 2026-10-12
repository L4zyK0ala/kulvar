from __future__ import annotations

from fastapi import Cookie, Depends
from sqlalchemy.orm import Session

from .db import get_db
from .models import Profile

COOKIE_NAME = "kulvar_profile"
LEGACY_COOKIE_NAME = "fitdash_profile"  # eski ad: main.migrate_cookies yeni ada taşır


def get_current_profile(
    profile_id: str | None = Cookie(default=None, alias=COOKIE_NAME),
    db: Session = Depends(get_db),
) -> Profile | None:
    if not profile_id:
        return None
    return db.get(Profile, profile_id)
