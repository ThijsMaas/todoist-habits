"""Move a recurring task's current occurrence without losing the recurrence.

Todoist API v1 has no "move the date, keep the pattern" call: sending only `due_date` flattens the task into a
one-off. Two strategies, each verified against the returned task:

  B. "<string> starting <YYYY-MM-DD>" (localised keyword, e.g. "vanaf" for nl) pins the first occurrence to an
     explicit date. Verified live (nl): recurrence and time survive and Todoist normalises `due.string` back to the
     bare pattern. Primary for explicit targets.
  A. resubmit the bare recurrence string as `due_string`. Todoist resolves the next occurrence from *now*: verified
     live to give tomorrow for "elke dag" in the afternoon, so it is only the fallback for explicit targets, and the
     sole option for fixed schedules ("next natural occurrence").

`due_lang` is always sent so Todoist parses the string in the language it was written in. Should Todoist ever keep a
"starting" suffix in `due.string`, `models.strip_starting` removes it before the string is reused.
"""
from __future__ import annotations

import logging
from datetime import date
from zoneinfo import ZoneInfo

from .api import TodoistClient
from .models import Habit, parse_due_date, starting_string

log = logging.getLogger(__name__)


class RescheduleError(RuntimeError):
    pass


def _ok(task: dict, target: date | None, today: date, tz: ZoneInfo) -> bool:
    due = task.get("due") or {}
    if not due.get("is_recurring"):
        return False
    actual = parse_due_date(due, tz)
    if actual is None:
        return False
    return actual == target if target is not None else actual >= today


def reschedule(client: TodoistClient, habit: Habit, target: date | None, today: date, tz: ZoneInfo) -> tuple[dict, str]:
    """Reschedule to `target` (None = "next natural occurrence from now", used for fixed schedules).
    Returns (updated task, strategy used). Raises RescheduleError if neither strategy verifies."""
    attempts: list[tuple[str, str]] = []
    if target is not None:
        attempts.append(("B", starting_string(habit.recurrence_string, habit.lang, target)))
    if target is None or target == today:
        attempts.append(("A", habit.recurrence_string))

    last: dict | None = None
    for strategy, due_string in attempts:
        last = client.update_task(habit.task_id, due_string=due_string, due_lang=habit.lang)
        if _ok(last, target, today, tz):
            log.info("rescheduled %r -> %s via %s (%r)", habit.name, target or "next occurrence", strategy, due_string)
            return last, strategy
        log.warning("strategy %s gave due=%s for %r; trying next", strategy, (last.get("due") or {}).get("date"), habit.name)

    raise RescheduleError(f"could not reschedule {habit.name!r} to {target}: final due={(last or {}).get('due')}")
