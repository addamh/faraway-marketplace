from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import settings
from .jobs import run_scrape

log = logging.getLogger(__name__)
_scheduler: BackgroundScheduler | None = None


def start() -> BackgroundScheduler | None:
    global _scheduler
    if not settings.scheduler_enabled or _scheduler is not None:
        return _scheduler
    _scheduler = BackgroundScheduler(timezone=settings.schedule_tz)
    _scheduler.add_job(
        run_scrape,
        CronTrigger(hour=settings.schedule_hour, minute=settings.schedule_minute, timezone=settings.schedule_tz),
        kwargs={"trigger": "schedule"},
        id="daily-scrape",
        coalesce=True,
        misfire_grace_time=3600,
        max_instances=1,
    )
    _scheduler.start()
    log.info("Daily scrape scheduled at %02d:%02d %s", settings.schedule_hour, settings.schedule_minute, settings.schedule_tz)
    return _scheduler


def next_run_time():
    if _scheduler is None:
        return None
    job = _scheduler.get_job("daily-scrape")
    return job.next_run_time if job else None


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
