"""Регистр инструментов агента + диспетчер вызовов.

Используется через Anthropic native tool use API: даём Claude список tool definitions,
он сам решает когда звать. Каждый вызов логируется в `tool_calls` для аудита.
"""

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import structlog

from src.db.repo import TasksRepo
from src.db.repo_extra import LocationsRepo, ToolCallsRepo
from src.domain.models import TaskItem
from src.services import weather, wiki

log = structlog.get_logger()


# ============================================================================
# Tool definitions для Anthropic
# ============================================================================


TOOLS_SPEC: list[dict[str, Any]] = [
    {
        "name": "get_user_location",
        "description": (
            "Получить последнюю известную локацию пользователя (город + координаты). "
            "Используй когда нужно понять где пользователь, чтобы привязать ответ к городу."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_weather",
        "description": (
            "Получить текущую погоду + прогноз на 1-7 дней для координат. "
            "Возвращает температуру, осадки, ветер, восход/закат, описание погоды. "
            "Использовать когда планируешь активности на улице, или пользователь спросил про погоду."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "lat": {"type": "number"},
                "lon": {"type": "number"},
                "days": {"type": "integer", "default": 3, "description": "1-7"},
            },
            "required": ["lat", "lon"],
        },
    },
    {
        "name": "schedule_reminder",
        "description": (
            "Создать напоминание от своего имени в конкретное время. Бот САМ напишет пользователю в указанный момент. "
            "Используй всегда, когда пользователь просит напомнить что-то к определённому времени "
            "(«завтра утром», «через час», «в понедельник в 10»). "
            "Переведи относительное время в ISO-8601 datetime с учётом часового пояса пользователя. "
            "Текущее время и часовой пояс пользователя см. в system context. "
            "Одно напоминание — одна задача. Для регулярных — ставь ближайшее, при срабатывании предложишь повторить."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Короткий заголовок напоминания (до 200 символов)",
                },
                "remind_at": {
                    "type": "string",
                    "description": "ISO-8601 datetime с TZ offset, например '2026-04-18T08:00:00+07:00'",
                },
                "details": {
                    "type": "string",
                    "description": "Дополнительный контекст, опционально",
                },
            },
            "required": ["title", "remind_at"],
        },
    },
    {
        "name": "wiki_geosearch",
        "description": (
            "Найти достопримечательности и интересные места рядом с точкой (по Wikipedia). "
            "Полезно для планирования прогулок, поиска культурных активностей, идей куда сходить новенького. "
            "Возвращает названия и ссылки на статьи."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "lat": {"type": "number"},
                "lon": {"type": "number"},
                "radius_m": {"type": "integer", "default": 5000, "description": "Макс 10000"},
                "lang": {"type": "string", "default": "en", "description": "ISO язык: en, ru, vi, ..."},
            },
            "required": ["lat", "lon"],
        },
    },
]


# ============================================================================
# Dispatcher
# ============================================================================


ToolFunc = Callable[..., Awaitable[Any]]


async def _t_get_user_location(user_id: int, **_kwargs) -> dict:
    loc = await LocationsRepo.latest(user_id)
    if loc is None:
        return {"error": "Локация ещё не задана. Попроси пользователя отправить геопозицию или указать город через /where."}
    return {
        "lat": loc["lat"],
        "lon": loc["lon"],
        "label": loc.get("label"),
        "updated_at": loc["created_at"].isoformat(),
    }


async def _t_get_weather(user_id: int, *, lat, lon, days=3) -> dict:
    data = await weather.get_weather(float(lat), float(lon), days=int(days))
    return data or {"error": "Погода временно недоступна"}


async def _t_wiki_geosearch(user_id: int, *, lat, lon, radius_m=5000, lang="en") -> dict:
    res = await wiki.geo_search(float(lat), float(lon), radius_m=int(radius_m), lang=str(lang))
    return {"count": len(res), "results": res}


async def _t_schedule_reminder(user_id: int, *, title, remind_at, details=None) -> dict:
    try:
        iso = str(remind_at).replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception as e:
        return {"error": f"invalid remind_at (ожидается ISO-8601): {e}"}

    if dt <= datetime.now(timezone.utc):
        return {"error": "remind_at должен быть в будущем"}

    task = await TasksRepo.create(
        TaskItem(
            user_id=user_id,
            title=str(title)[:200],
            details=str(details)[:1000] if details else None,
            remind_at=dt,
        )
    )
    return {
        "ok": True,
        "task_id": task.id,
        "remind_at_utc": dt.astimezone(timezone.utc).isoformat(),
    }


DISPATCH: dict[str, ToolFunc] = {
    "get_user_location": _t_get_user_location,
    "get_weather": _t_get_weather,
    "wiki_geosearch": _t_wiki_geosearch,
    "schedule_reminder": _t_schedule_reminder,
}


async def execute_tool(user_id: int, tool_name: str, tool_input: dict) -> dict:
    """Выполняет tool-call с логированием. Никогда не падает — оборачивает ошибки в {error: ...}."""
    handler = DISPATCH.get(tool_name)
    if handler is None:
        result = {"error": f"unknown tool: {tool_name}"}
        await ToolCallsRepo.log(user_id, tool_name, tool_input, error="unknown_tool")
        return result

    started = time.monotonic()
    try:
        out = await asyncio.wait_for(handler(user_id, **tool_input), timeout=45.0)
        duration = int((time.monotonic() - started) * 1000)
        await ToolCallsRepo.log(user_id, tool_name, tool_input, output_data=out, duration_ms=duration)
        return out
    except asyncio.TimeoutError:
        await ToolCallsRepo.log(user_id, tool_name, tool_input, error="timeout")
        return {"error": "tool timed out"}
    except Exception as e:
        log.warning("tool.failed", tool=tool_name, error=str(e))
        await ToolCallsRepo.log(user_id, tool_name, tool_input, error=str(e))
        return {"error": str(e)}
