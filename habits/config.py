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


@dataclass(frozen=True)
class Config:
    token: str
    api_url: str
    tz: ZoneInfo
    habit_filter: str  # Todoist filter query selecting habit tasks, e.g. "@habit" or "#Habits"
    chore_filter: str  # optional filter for stats-only chores, e.g. "#Chores"; empty disables
    poll_interval_min: int
    backfill_days: int
    db_path: Path
    midnight_time: time
    hide_after_goal: bool
    early_warning: bool
    warn_priority: bool
    title_progress: bool
    log_level: str

    @classmethod
    def from_env(cls, dotenv: Path | None = None) -> "Config":
        load_dotenv(dotenv or Path(".env"))
        token = os.environ.get("TODOIST_API_TOKEN", "")
        if not token:
            raise SystemExit("TODOIST_API_TOKEN is not set (env or .env)")
        hh, mm = os.environ.get("MIDNIGHT_TIME", "00:05").split(":")
        return cls(
            token=token,
            api_url=os.environ.get("TODOIST_API_URL", "https://api.todoist.com/api/v1/"),
            habit_filter=os.environ.get("HABIT_FILTER", "@habit"),
            chore_filter=os.environ.get("CHORE_FILTER", ""),
            tz=ZoneInfo(os.environ.get("TZ", "Europe/Amsterdam")),
            poll_interval_min=int(os.environ.get("POLL_INTERVAL", "10")),
            backfill_days=int(os.environ.get("BACKFILL_DAYS", "7")),
            db_path=Path(os.environ.get("DB_PATH", "habits.db")),
            midnight_time=time(int(hh), int(mm)),
            hide_after_goal=_bool(os.environ.get("HIDE_AFTER_GOAL", "true")),
            early_warning=_bool(os.environ.get("EARLY_WARNING", "true")),
            warn_priority=_bool(os.environ.get("WARN_PRIORITY", "true")),
            title_progress=_bool(os.environ.get("TITLE_PROGRESS", "true")),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
        )
