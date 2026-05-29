"""Handler геолокации: F.location (через скрепку) + команда /where для ручного ввода города."""

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from src.db.client import get_pool

router = Router()
log = structlog.get_logger()


@router.message(F.location)
async def on_location(message: Message) -> None:
    user_id = message.from_user.id  # type: ignore[union-attr]
    loc = message.location
    if loc is None:
        return

    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO locations (user_id, lat, lon, accuracy_m, source)
        VALUES ($1, $2, $3, $4, 'telegram')
        """,
        user_id,
        float(loc.latitude),
        float(loc.longitude),
        float(loc.horizontal_accuracy) if loc.horizontal_accuracy else None,
    )

    # Reverse-геокодинг через Nominatim в фоне (не блокируем юзера)
    import asyncio

    asyncio.create_task(_reverse_geocode_and_label(user_id, loc.latitude, loc.longitude))

    await message.answer(
        f"📍 Запомнил твою локацию ({loc.latitude:.4f}, {loc.longitude:.4f}). "
        "Теперь смогу искать активности и погоду рядом."
    )


@router.message(Command("where"))
async def on_where_manual(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    user_id = message.from_user.id  # type: ignore[union-attr]
    pool = await get_pool()

    if len(parts) < 2:
        # Показываем последнюю
        row = await pool.fetchrow(
            "SELECT lat, lon, label, source, created_at FROM locations "
            "WHERE user_id = $1 ORDER BY created_at DESC LIMIT 1",
            user_id,
        )
        if row is None:
            await message.answer(
                "Локации ещё не было. Отправь геопозицию через скрепку → Геопозиция, "
                "или напиши: /where Хошимин"
            )
            return
        label = row["label"] or f"({row['lat']:.4f}, {row['lon']:.4f})"
        await message.answer(
            f"📍 Текущая: {label}\nИсточник: {row['source']}, обновлена: {row['created_at']:%Y-%m-%d %H:%M UTC}"
        )
        return

    label = parts[1].strip()
    # Геокодим через Nominatim
    from src.services.geo import geocode_city

    coords = await geocode_city(label)
    if coords is None:
        await message.answer(
            f"Не нашёл координаты для «{label}». Попробуй точнее (например: «Hồ Chí Minh, Vietnam»)."
        )
        return

    lat, lon, resolved_name = coords
    await pool.execute(
        """
        INSERT INTO locations (user_id, lat, lon, label, source)
        VALUES ($1, $2, $3, $4, 'manual')
        """,
        user_id,
        lat,
        lon,
        resolved_name,
    )
    await message.answer(f"📍 Запомнил: {resolved_name} ({lat:.4f}, {lon:.4f})")


async def _reverse_geocode_and_label(user_id: int, lat: float, lon: float) -> None:
    from src.services.geo import reverse_geocode

    label = await reverse_geocode(lat, lon)
    if not label:
        return
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE locations SET label = $2
        WHERE id = (
            SELECT id FROM locations
            WHERE user_id = $1 AND lat = $3 AND lon = $4
            ORDER BY created_at DESC LIMIT 1
        )
        """,
        user_id,
        label,
        lat,
        lon,
    )
