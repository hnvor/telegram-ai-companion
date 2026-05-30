"""Бесплатные geo-сервисы без API-ключей.

- Nominatim (OpenStreetMap) — geocoding и reverse geocoding.
  Polite usage: User-Agent обязателен, max 1 req/sec.
"""

import asyncio
from typing import Any

import httpx
import structlog

log = structlog.get_logger()

NOMINATIM_BASE = "https://nominatim.openstreetmap.org"
USER_AGENT = "telegram_ai_companion_bot/0.1 (single-user, contact: github.com/hnvor/telegram-ai-companion)"

# Простой rate-limiter для соблюдения 1 req/sec у Nominatim
_nominatim_lock = asyncio.Lock()
_last_nominatim_call: float = 0.0


async def _nominatim_get(path: str, params: dict[str, Any]) -> dict | list | None:
    global _last_nominatim_call
    async with _nominatim_lock:
        now = asyncio.get_event_loop().time()
        wait = max(0.0, 1.05 - (now - _last_nominatim_call))
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            async with httpx.AsyncClient(timeout=20.0, headers={"User-Agent": USER_AGENT}) as client:
                r = await client.get(f"{NOMINATIM_BASE}{path}", params=params)
                r.raise_for_status()
                _last_nominatim_call = asyncio.get_event_loop().time()
                return r.json()
        except Exception as e:
            log.warning("nominatim.failed", path=path, error=str(e))
            return None


async def geocode_city(query: str) -> tuple[float, float, str] | None:
    """Город/адрес → (lat, lon, display_name) или None."""
    data = await _nominatim_get(
        "/search",
        {"q": query, "format": "jsonv2", "limit": 1, "addressdetails": 1},
    )
    if not isinstance(data, list) or not data:
        return None
    item = data[0]
    try:
        return float(item["lat"]), float(item["lon"]), item.get("display_name", query)
    except Exception:
        return None


async def reverse_geocode(lat: float, lon: float) -> str | None:
    """Координаты → человекочитаемое название (город, страна)."""
    data = await _nominatim_get(
        "/reverse",
        {"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 10, "addressdetails": 1},
    )
    if not isinstance(data, dict):
        return None
    addr = data.get("address", {})
    parts = []
    for k in ("city", "town", "village", "municipality"):
        if k in addr:
            parts.append(addr[k])
            break
    if "country" in addr:
        parts.append(addr["country"])
    if parts:
        return ", ".join(parts)
    return data.get("display_name")
