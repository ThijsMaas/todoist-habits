from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

GOAL_LABEL_RE = re.compile(r"^(\d+)x-week$", re.IGNORECASE)
TITLE_PROGRESS_RE = re.compile(r"\s*\(\d+/\d+\)\s*$")

# Keyword Todoist uses per language to pin the first occurrence of a recurrence ("every day starting 2026-10-01").
STARTING_WORD = {"en": "starting", "nl": "vanaf"}
STARTING_RE = re.compile(r"\s+(starting(\s+on)?|vanaf)\s+.*$", re.IGNORECASE)
DAILY_RE = re.compile(
    r"^(every|ev|elke|iedere)!?\s+(day|morning|afternoon|evening|night|1\s+day|dag|ochtend|middag|avond|nacht|1\s+dag)\b"
    r"|^(daily|dagelijks)\b",
    re.IGNORECASE,
)

DAILY = "daily"
X_PER_WEEK = "x_per_week"
FIXED = "fixed"
CHORE = "chore"  # stats only: never rescheduled, never marked missed


@dataclass
class Habit:
    task_id: str
    name: str  # base title without "(2/3)" suffix
    type: str  # daily | x_per_week | fixed | chore
    weekly_goal: int | None
    recurrence_string: str  # canonical, without any "starting ..." suffix
    lang: str  # due.lang, passed back as due_lang so Todoist parses the string in the right language
    created_at: str  # task added_at as given by Todoist
    tracking_since: date
    base_priority: int = 1
    priority_raised: bool = False


def strip_starting(recurrence: str) -> str:
    return STARTING_RE.sub("", recurrence).strip()


def starting_string(recurrence: str, lang: str, day: date) -> str:
    return f"{recurrence} {STARTING_WORD.get(lang, STARTING_WORD['en'])} {day.isoformat()}"


def base_name(content: str) -> str:
    return TITLE_PROGRESS_RE.sub("", content).strip()


def weekly_goal(task: dict) -> int | None:
    for label in task.get("labels", []):
        if m := GOAL_LABEL_RE.match(label):
            return int(m.group(1))
    return None


def classify(task: dict) -> tuple[str, int | None] | None:
    """Return (type, weekly_goal) or None if the task cannot be tracked (not recurring)."""
    due = task.get("due") or {}
    if not due.get("is_recurring"):
        return None
    goal = weekly_goal(task)
    if goal:
        return X_PER_WEEK, goal
    if DAILY_RE.match(strip_starting(due.get("string", ""))):
        return DAILY, None
    return FIXED, None


def parse_due_date(due: dict | None, tz: ZoneInfo) -> date | None:
    """Local calendar date of a task's due object. Handles YYYY-MM-DD, floating datetimes, and UTC (Z) datetimes."""
    if not due or not due.get("date"):
        return None
    raw: str = due["date"]
    if len(raw) == 10:
        return date.fromisoformat(raw)
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.date()  # floating local time
    return dt.astimezone(tz).date()


def to_local(iso: str, tz: ZoneInfo) -> datetime:
    """Parse a Todoist UTC timestamp (e.g. 2025-01-10T10:00:00Z or with micros) into local time."""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


def habit_from_task(task: dict, tz: ZoneInfo, today: date, backfill_days: int, chore: bool = False) -> Habit | None:
    if chore:
        htype, goal = CHORE, None
    else:
        kind = classify(task)
        if kind is None:
            return None
        htype, goal = kind
    due = task.get("due") or {}
    added = to_local(task["added_at"], tz).date() if task.get("added_at") else today
    # Tracking starts at task creation, bounded by how far back completions can be backfilled. The due date is
    # deliberately not used: an overdue one is the first missed day, and a task completed today is due tomorrow.
    candidates = [added, date.fromordinal(today.toordinal() - backfill_days)]
    return Habit(
        task_id=task["id"],
        name=base_name(task["content"]),
        type=htype,
        weekly_goal=goal,
        recurrence_string=strip_starting(due.get("string") or ""),
        lang=due.get("lang") or "en",
        created_at=task.get("added_at", ""),
        tracking_since=max(candidates),
        base_priority=task.get("priority", 1),
    )
