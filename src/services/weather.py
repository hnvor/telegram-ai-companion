"""Погода через Open-Meteo. Бесплатно, без ключа.
Включает sunrise/sunset — отдельный API не нужен."""

import httpx
import structlog

log = structlog.get_logger()

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


async def get_weather(lat: float, lon: float, days: int = 3) -> dict | None:
    """Возвращает структуру с current + daily forecast на N дней."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "timezone": "auto",
        "forecast_days": max(1, min(days, 7)),
        "current": "temperature_2m,weather_code,wind_speed_10m,relative_humidity_2m,apparent_temperature,is_day",
        "daily": (
            "temperature_2m_max,temperature_2m_min,precipitation_sum,"
            "precipitation_probability_max,sunrise,sunset,uv_index_max,weather_code"
        ),
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(OPEN_METEO_URL, params=params)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.warning("openmeteo.failed", error=str(e))
        return None

    return _humanize(data)


WMO_CODES: dict[int, str] = {
    0: "ясно",
    1: "почти ясно",
    2: "переменная облачность",
    3: "пасмурно",
    45: "туман",
    48: "иней-туман",
    51: "морось слабая",
    53: "морось",
    55: "морось сильная",
    61: "дождь слабый",
    63: "дождь",
    65: "дождь сильный",
    71: "снег слабый",
    73: "снег",
    75: "снег сильный",
    77: "снежная крупа",
    80: "ливни слабые",
    81: "ливни",
    82: "ливни сильные",
    85: "снегопад слабый",
    86: "снегопад сильный",
    95: "гроза",
    96: "гроза с градом",
    99: "сильная гроза с градом",
}


def _describe_code(code: int | None) -> str:
    if code is None:
        return "?"
    return WMO_CODES.get(int(code), f"код {code}")


def _humanize(data: dict) -> dict:
    """Превращает сырой ответ в компактную структуру для LLM."""
    cur = data.get("current") or {}
    daily = data.get("daily") or {}

    days_out: list[dict] = []
    times = daily.get("time", []) or []
    for i, day in enumerate(times):
        days_out.append(
            {
                "date": day,
                "tmin_c": _safe(daily.get("temperature_2m_min"), i),
                "tmax_c": _safe(daily.get("temperature_2m_max"), i),
                "precip_mm": _safe(daily.get("precipitation_sum"), i),
                "precip_prob_max": _safe(daily.get("precipitation_probability_max"), i),
                "uv_max": _safe(daily.get("uv_index_max"), i),
                "sunrise": _safe(daily.get("sunrise"), i),
                "sunset": _safe(daily.get("sunset"), i),
                "summary": _describe_code(_safe(daily.get("weather_code"), i)),
            }
        )

    return {
        "timezone": data.get("timezone"),
        "current": {
            "t_c": cur.get("temperature_2m"),
            "feels_c": cur.get("apparent_temperature"),
            "humidity_pct": cur.get("relative_humidity_2m"),
            "wind_ms": cur.get("wind_speed_10m"),
            "is_day": bool(cur.get("is_day", 1)),
            "summary": _describe_code(cur.get("weather_code")),
        },
        "days": days_out,
    }


def _safe(arr, i):
    if not arr or i >= len(arr):
        return None
    return arr[i]
