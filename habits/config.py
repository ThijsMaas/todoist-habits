from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo


def load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, '#' comments, optional quotes. Does not override existing env."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _time(value: str) -> time:
    hh, mm = value.strip().split(":")
    return time(int(hh), int(mm))


def _times(value: str) -> tuple[time, ...]:
    """Comma-separated HH:MM list; empty means none."""
    return tuple(sorted(_time(v) for v in value.split(",") if v.strip()))


# Default reminder times per Todoist API priority (4 == "P1" in the apps, 1 == "P4").
DEFAULT_REMINDER_TIMES = {4: "09:00,18:00,21:30", 3: "18:00,21:30", 2: "21:00", 1: "21:00"}


@dataclass(frozen=True)
class Config:
    token: str
    api_url: str
    tz: ZoneInfo
    habit_filter: str  # Todoist filter query selecting habit tasks, e.g. "@habit" or "#Habits"
    chore_filter: str  # optional filter for chores (never rescheduled or missed), e.g. "#Chores"; empty disables
    poll_interval_min: int
    backfill_days: int
    db_path: Path
    midnight_time: time
    hide_after_goal: bool
    early_warning: bool
    warn_priority: bool
    title_progress: bool
    reminders: bool  # plugin-managed Todoist reminders for habits/chores that are still due today
    reminder_times: dict[int, tuple[time, ...]]  # API priority -> local times of day
    reminder_lead_min: int  # re-check completions this many minutes before each reminder fires
    log_level: str

    @classmethod
    def from_env(cls, dotenv: Path | None = None) -> "Config":
        load_dotenv(dotenv or Path(".env"))
        token = os.environ.get("TODOIST_API_TOKEN", "")
        if not token:
            raise SystemExit("TODOIST_API_TOKEN is not set (env or .env)")
        reminder_times = {
            prio: _times(os.environ.get(f"REMINDER_TIMES_P{5 - prio}", default))
            for prio, default in DEFAULT_REMINDER_TIMES.items()
        }
        return cls(
            token=token,
            api_url=os.environ.get("TODOIST_API_URL", "https://api.todoist.com/api/v1/"),
            habit_filter=os.environ.get("HABIT_FILTER", "@habit"),
            chore_filter=os.environ.get("CHORE_FILTER", ""),
            tz=ZoneInfo(os.environ.get("TZ", "Europe/Amsterdam")),
            poll_interval_min=int(os.environ.get("POLL_INTERVAL", "10")),
            backfill_days=int(os.environ.get("BACKFILL_DAYS", "7")),
            db_path=Path(os.environ.get("DB_PATH", "habits.db")),
            midnight_time=_time(os.environ.get("MIDNIGHT_TIME", "00:05")),
            hide_after_goal=_bool(os.environ.get("HIDE_AFTER_GOAL", "true")),
            early_warning=_bool(os.environ.get("EARLY_WARNING", "true")),
            warn_priority=_bool(os.environ.get("WARN_PRIORITY", "true")),
            title_progress=_bool(os.environ.get("TITLE_PROGRESS", "true")),
            reminders=_bool(os.environ.get("REMINDERS", "true")),
            reminder_times=reminder_times,
            reminder_lead_min=int(os.environ.get("REMINDER_LEAD", "2")),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
        )
