FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Europe/Istanbul

# Uygulama yerel saatle çalışıyor ("bugün", akşam kontrolü): TZ'nin işlemesi için sistem saat dilimi verisi gerekli.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Sürümler requirements.lock'ta sabit; --no-deps, myfitnesspal'ın eski katı kısıtlarının çözümleyiciyi bozmasını önler.
COPY requirements.lock .
RUN pip install --no-deps -r requirements.lock

COPY app ./app
COPY scripts ./scripts

# Root olmayan kullanıcı; kalıcı veri klasörleri ona ait (adlandırılmış volume'lar bu sahipliği devralır).
RUN useradd --system --uid 10001 --home-dir /app kulvar \
 && mkdir -p /data /app/app/data /app/app/static/uploads \
 && chown -R kulvar:kulvar /data /app/app/data /app/app/static/uploads
USER kulvar

ENV KULVAR_DATABASE_URL=sqlite:////data/kulvar.db
EXPOSE 8010
VOLUME ["/data", "/app/app/data", "/app/app/static/uploads"]

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8010/', timeout=4).status == 200 else 1)"

# Tek süreç: zamanlayıcı ve Telegram dinleyicileri uygulamanın içinde çalışır; birden fazla worker başlatma.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8010"]
