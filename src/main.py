"""Точка входа: настраиваем aiogram + scheduler + embeddings warmup, запускаем polling."""

import asyncio
import socket

# Заставляем Python использовать системное хранилище сертификатов ОС вместо bundled certifi.
# Без этого на Windows-серверах с TLS-инспекцией (антивирус/firewall с HTTPS scanning) handshake
# к api.telegram.org падает с CERTIFICATE_VERIFY_FAILED, потому что MITM-CA лежит в Windows trust
# store, а Python о нём не знает. Должно идти ДО любого import aiohttp/aiogram.
try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

import structlog
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import ClientSession, TCPConnector

from src.bot import onboarding
from src.bot.handlers import (
    chat,
    commands,
    diary,
    experiments,
    location,
    planner,
    tasks,
    voice,
)
from src.bot.middleware import AuthMiddleware
from src.config import settings
from src.core.embeddings import warmup as embeddings_warmup
from src.db.client import close_pool, get_pool
from src.logging_setup import setup_logging
from src.services.scheduler import setup_scheduler

log = structlog.get_logger()


class _IPv4Session(AiohttpSession):
    """Force IPv4 + явный DNS — частый фикс под Docker Desktop на Windows."""

    async def create_session(self) -> ClientSession:
        if self._session is None or self._session.closed:
            connector = TCPConnector(family=socket.AF_INET, limit=100, ttl_dns_cache=300)
            self._session = ClientSession(
                connector=connector, json_serialize=self.json_dumps
            )
        return self._session


def _build_session() -> AiohttpSession:
    if settings.telegram_proxy:
        log.info("bot.using_proxy", proxy=settings.telegram_proxy.split("@")[-1])
        return AiohttpSession(proxy=settings.telegram_proxy)
    return _IPv4Session()


async def main() -> None:
    setup_logging()
    log.info("startup", env=settings.env, allowed_user=settings.allowed_user_id)

    # Прогрев пула + проверка БД
    pool = await get_pool()
    await pool.fetchval("SELECT 1")
    log.info("db.connected")

    # Прогрев embeddings в фоне (тяжёлая загрузка модели на старте)
    asyncio.create_task(embeddings_warmup())

    session = _build_session()
    bot = Bot(
        token=settings.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=None),
    )
    dp = Dispatcher(storage=MemoryStorage())

    # Auth — раньше всех
    dp.update.outer_middleware(AuthMiddleware())

    # Порядок регистрации важен: онбординг (FSM) → команды → планировщики → задачи → дневник → локация → голос → чат-catchall
    dp.include_router(onboarding.router)
    dp.include_router(commands.router)
    dp.include_router(planner.router)
    dp.include_router(tasks.router)
    dp.include_router(diary.router)
    dp.include_router(location.router)
    dp.include_router(experiments.router)
    if settings.enable_voice:
        dp.include_router(voice.router)
    dp.include_router(chat.router)

    scheduler = setup_scheduler(bot)
    scheduler.start()
    log.info("scheduler.started", jobs=[j.id for j in scheduler.get_jobs()])

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("bot.polling_start")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()
        await close_pool()
        log.info("shutdown")


if __name__ == "__main__":
    asyncio.run(main())
