from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

# noqa: F401 - importlar CONNECT_FUNCS/SYNC_FUNCS registry'sini doldurur
from .integrations import garmin, hevy, myfitnesspal, strava  # noqa: F401
from . import i18n, telegram_bot
from .db import SessionLocal, init_db
from .deps import COOKIE_NAME, LEGACY_COOKIE_NAME
from .models import Profile
from .routers import activities, coach, coach_chat, dashboard, health, nutrition, profiles, settings, strength, sync
from .scheduler import start_scheduler, stop_scheduler

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _reset_interrupted_jobs() -> None:
    from . import health, routine_builder

    db = SessionLocal()
    try:
        health.reset_interrupted(db)
        routine_builder.reset_interrupted(db)
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _reset_interrupted_jobs()
    start_scheduler()
    telegram_bot.start()
    yield
    telegram_bot.stop()
    stop_scheduler()


app = FastAPI(title="Kulvar", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


_COOKIE_RENAMES = {LEGACY_COOKIE_NAME: COOKIE_NAME, i18n.LEGACY_LANG_COOKIE: i18n.LANG_COOKIE}


@app.middleware("http")
async def resolve_language(request: Request, call_next):
    """İstek dilini belirler: seçili profilin dili → kulvar_lang çerezi → Accept-Language."""
    lang = None
    if not request.url.path.startswith("/static"):
        profile_id = request.cookies.get(COOKIE_NAME)
        if profile_id:
            db = SessionLocal()
            try:
                profile = db.get(Profile, profile_id)
                lang = profile.language if profile else None
            finally:
                db.close()
        lang = lang or request.cookies.get(i18n.LANG_COOKIE) or i18n.from_accept_language(request.headers.get("accept-language"))
    request.state.lang = i18n.normalize(lang)
    return await call_next(request)

@app.middleware("http")
async def migrate_cookies(request: Request, call_next):
    """Kinetra/FitDash döneminden kalan çerezleri yeni adlara taşır; profil seçimi ve dil korunur."""
    moved = {new: request.cookies[old] for old, new in _COOKIE_RENAMES.items()
             if old in request.cookies and new not in request.cookies}
    if moved:
        cookie = "; ".join([f"{k}={v}" for k, v in request.cookies.items()] + [f"{k}={v}" for k, v in moved.items()])
        request.scope["headers"] = [(k, v) for k, v in request.scope["headers"] if k != b"cookie"] + [(b"cookie", cookie.encode())]
    response = await call_next(request)
    for old, new in _COOKIE_RENAMES.items():
        if new in moved:
            response.set_cookie(new, moved[new], max_age=60 * 60 * 24 * 365, httponly=new == COOKIE_NAME, samesite="lax")
        if old in request.cookies:
            response.delete_cookie(old)
    return response


app.include_router(profiles.router)
app.include_router(dashboard.router)
app.include_router(settings.router)
app.include_router(activities.router)
app.include_router(strength.router)
app.include_router(nutrition.router)
app.include_router(coach.router)
app.include_router(coach_chat.router)
app.include_router(health.router)
app.include_router(sync.router)
