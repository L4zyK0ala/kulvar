"""Zaman yardımcıları.

Veritabanındaki zaman damgaları saat dilimi bilgisi olmadan (naive) UTC olarak saklanır ve kod da
onları naive karşılaştırır. Python 3.12'de kullanımdan kaldırılan datetime.utcnow() yerine aynı
değeri üreten utcnow() kullanılır.
"""
from __future__ import annotations

import datetime as dt


def utcnow() -> dt.datetime:
    """Şu anki UTC zamanı, saat dilimi bilgisi olmadan (eski datetime.utcnow() ile aynı değer)."""
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)
