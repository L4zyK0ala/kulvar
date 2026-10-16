"""Uzun süren işler (Claude ile belge yorumu, takviye değerlendirmesi, doktor notu) için arka plan.

Web isteği hemen döner, iş ayrı bir thread'de kendi veritabanı oturumuyla çalışır; nginx'in
60 sn'lik zaman aşımına takılmaz. Aynı anahtarla zaten çalışan bir iş varsa ikincisi başlatılmaz.
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger("kulvar.background")

_running: set[str] = set()
_lock = threading.Lock()


def start(key: str, fn, *args) -> bool:
    """fn(*args)'ı arka planda çalıştırır; aynı key zaten çalışıyorsa False döner."""
    with _lock:
        if key in _running:
            return False
        _running.add(key)

    def run():
        try:
            fn(*args)
        except Exception:  # noqa: BLE001 - arka plan işi servisi düşürmesin
            logger.exception("Arka plan işi başarısız: %s", key)
        finally:
            with _lock:
                _running.discard(key)

    threading.Thread(target=run, name=f"bg-{key}", daemon=True).start()
    return True


def is_running(key: str) -> bool:
    with _lock:
        return key in _running
