"""APScheduler — все cron-jobs в памяти. Тиковая архитектура: jobs дёргают
БД и решают что делать. Persistence не нужна, т.к. список jobs описан в коде."""

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from aiogram import Bot

from src.config import settings
from src.services import pattern_detector, proactive


def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")

    # Каждые 5 минут — task reminders
    scheduler.add_job(
        proactive.check_task_reminders,
        IntervalTrigger(minutes=5),
        args=[bot, settings.allowed_user_id],
        id="task_reminders",
        replace_existing=True,
    )

    # Каждый час — морнинг и эвенинг чекины (внутри проверяется локальное время)
    scheduler.add_job(
        proactive.check_morning_brief,
        IntervalTrigger(minutes=30),
        args=[bot, settings.allowed_user_id],
        id="morning_brief",
        replace_existing=True,
    )

    scheduler.add_job(
        proactive.check_evening_checkin,
        IntervalTrigger(minutes=15),
        args=[bot, settings.allowed_user_id],
        id="evening_checkin",
        replace_existing=True,
    )

    # Каждые 3 часа — habit nudges
    scheduler.add_job(
        proactive.check_habit_nudges,
        IntervalTrigger(hours=3),
        args=[bot, settings.allowed_user_id],
        id="habit_nudges",
        replace_existing=True,
    )

    # Воскресенье 12:00 UTC = 19:00 Asia/Bangkok — обзор + план следующей недели
    scheduler.add_job(
        proactive.weekly_review,
        CronTrigger(day_of_week="sun", hour=12, minute=0),
        args=[bot, settings.allowed_user_id],
        id="weekly_review",
        replace_existing=True,
    )

    # Challenge — каждые 30 мин (внутри проверка дня недели/времени локально)
    scheduler.add_job(
        proactive.check_challenge,
        IntervalTrigger(minutes=30),
        args=[bot, settings.allowed_user_id],
        id="challenge",
        replace_existing=True,
    )

    # Pattern detector — каждые 30 мин (внутри проверка локального окна 13-19h и cooldown'ов)
    scheduler.add_job(
        pattern_detector.run_pattern_detection,
        IntervalTrigger(minutes=30),
        args=[bot, settings.allowed_user_id],
        id="pattern_detector",
        replace_existing=True,
    )

    # Якоря осознанности — каждые 30 мин (внутри окно, минимум 2.5ч между, лимит 3/день)
    scheduler.add_job(
        proactive.check_awareness_anchor,
        IntervalTrigger(minutes=30),
        args=[bot, settings.allowed_user_id],
        id="awareness_anchor",
        replace_existing=True,
    )

    # Life state update — раз в сутки, 04:30 UTC (~11:30 в Asia/Bangkok)
    scheduler.add_job(
        proactive.daily_life_state_update,
        CronTrigger(hour=4, minute=30),
        args=[bot, settings.allowed_user_id],
        id="life_state_update",
        replace_existing=True,
    )

    return scheduler
