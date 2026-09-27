from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from .api import TodoistClient
from .config import Config
from .db import COMPLETED, MISSED, Database
from .description import upsert_block
from .models import CHORE, DAILY, FIXED, X_PER_WEEK, Habit, habit_from_task, parse_due_date, to_local
from .reschedule import RescheduleError, reschedule
from .stats import (
    chore_stats, daily_stats, fixed_stats, render_chore, render_daily, render_fixed, render_weekly, week_start, weekly_stats,
)

log = logging.getLogger(__name__)

# Residue of the Todoist habit-tracker plugin this replaces; removed from descriptions of tracked tasks.
OLD_PLUGIN_LINE_RE = re.compile(r"^\*\*Current streak:\*\*.*\n?", re.MULTILINE)
P1 = 4  # Todoist API priority 4 == "P1" in the apps


@dataclass
class Update:
    description: str
    content: str
    priority: int | None  # new API priority to set, or None
    hide_until: date | None  # push x-per-week task to this date (goal reached)
    block: str


class Tracker:
    def __init__(self, client: TodoistClient, db: Database, cfg: Config):
        self.client, self.db, self.cfg = client, db, cfg

    def now(self) -> datetime:
        return datetime.now(self.cfg.tz)

    # -- data capture ----------------------------------------------------------------

    def register(self, today: date) -> list[tuple[dict, Habit]]:
        """Fetch habit (and optional chore) tasks and upsert them. Chores are stats-only and need no recurrence."""
        sources = [(self.cfg.habit_filter, False)]
        if self.cfg.chore_filter:
            sources.append((self.cfg.chore_filter, True))
        pairs = []
        for query, chore in sources:
            for task in self.client.tasks_by_filter(query):
                habit = habit_from_task(task, self.cfg.tz, today, self.cfg.backfill_days, chore=chore)
                if habit is None:
                    log.warning("skipping %r (%s): habit task is not recurring", task["content"], task["id"])
                    continue
                pairs.append((task, self.db.upsert_habit(habit)))
        return pairs

    def sync_completions(self, now: datetime) -> int:
        """Pull item:completed / item:uncompleted from the activity log. Overlaps one day with the last sync;
        inserts are idempotent so re-seeing events is harmless."""
        last = self.db.get_state("last_sync")
        since = (datetime.fromisoformat(last) - timedelta(days=1)).date() if last else now.date() - timedelta(days=self.cfg.backfill_days)
        until = now.date() + timedelta(days=1)  # exclusive upper bound
        known = {h.task_id for h in self.db.all_habits()}
        events = sorted(
            self.client.activities(["item:completed", "item:uncompleted"], since.isoformat(), until.isoformat()),
            key=lambda e: e["event_date"],
        )
        added = 0
        for ev in events:
            task_id = str(ev.get("object_id"))
            if task_id not in known:
                continue
            ts = to_local(ev["event_date"], self.cfg.tz)
            if ev["event_type"] == "completed":
                added += self.db.add_event(task_id, ts.date(), COMPLETED, ts)
            elif ev["event_type"] == "uncompleted":
                self.db.remove_latest_completion(task_id, ts)
        self.db.set_state("last_sync", now.isoformat())
        log.info("synced %d activity events, %d new completions", len(events), added)
        return added

    # -- stats & description --------------------------------------------------------------

    def plan_update(self, task: dict, habit: Habit, today: date) -> Update:
        completions = self.db.completions(habit.task_id)
        content = task["content"]
        priority = None
        hide_until = None
        if habit.type == X_PER_WEEK:
            ws = weekly_stats({c.date() for c in completions}, habit.weekly_goal, habit.tracking_since, today)
            block = render_weekly(ws, self.cfg.early_warning)
            if self.cfg.title_progress:
                content = f"{habit.name} ({ws.this_week}/{ws.goal})"
            if self.cfg.warn_priority:
                if ws.warning and not habit.priority_raised and task["priority"] != P1:
                    priority = P1
                elif not ws.warning and habit.priority_raised:
                    priority = habit.base_priority
            if self.cfg.hide_after_goal and ws.needed == 0:
                next_monday = week_start(today) + timedelta(days=7)
                due = parse_due_date(task.get("due"), self.cfg.tz)
                if due is not None and due < next_monday:
                    hide_until = next_monday
        elif habit.type == DAILY:
            block = render_daily(daily_stats(completions, habit.tracking_since, today))
        elif habit.type == CHORE:
            block = render_chore(chore_stats(completions, today))
        else:
            block = render_fixed(fixed_stats(completions, self.db.event_dates(habit.task_id, MISSED), today))
        description = OLD_PLUGIN_LINE_RE.sub("", task.get("description") or "")
        return Update(upsert_block(description, block), content, priority, hide_until, block)

    def apply_update(self, task: dict, habit: Habit, upd: Update, today: date) -> bool:
        fields: dict = {}
        if upd.description != (task.get("description") or ""):
            fields["description"] = upd.description
        if upd.content != task["content"]:
            fields["content"] = upd.content
        if upd.priority is not None:
            fields["priority"] = upd.priority
        if fields:
            self.client.update_task(task["id"], **fields)
            log.info("updated %r: %s", habit.name, ", ".join(fields))
        if upd.priority == P1:
            self.db.set_priority_state(habit.task_id, True, task["priority"])
        elif upd.priority is not None:
            self.db.set_priority_state(habit.task_id, False, habit.base_priority)
        if upd.hide_until is not None:
            try:
                reschedule(self.client, habit, upd.hide_until, today, self.cfg.tz)
                log.info("goal reached: hid %r until %s", habit.name, upd.hide_until)
            except RescheduleError as exc:
                log.error("%s", exc)
        return bool(fields)

    def write_all(self, pairs: list[tuple[dict, Habit]], today: date) -> int:
        return sum(self.apply_update(task, habit, self.plan_update(task, habit, today), today) for task, habit in pairs)

    # -- jobs ----------------------------------------------------------------------------------

    def refresh(self, now: datetime) -> None:
        """Poll job: register habits, pull completions, rewrite stats blocks."""
        today = now.date()
        pairs = self.register(today)
        self.sync_completions(now)
        self.write_all(pairs, today)

    def log_misses(self, habits: list[Habit], today: date) -> int:
        """Daily habits: every tracked day up to yesterday without a completion is a miss. Idempotent, so this
        fills gaps from downtime and counts postponed days too."""
        stamp = datetime.combine(today, self.cfg.midnight_time, tzinfo=self.cfg.tz)
        added = 0
        for habit in habits:
            if habit.type != DAILY:
                continue
            done = self.db.event_dates(habit.task_id, COMPLETED)
            day = habit.tracking_since
            while day < today:
                if day not in done:
                    added += self.db.add_event(habit.task_id, day, MISSED, stamp)
                day += timedelta(days=1)
        return added

    def midnight(self, now: datetime) -> None:
        today = now.date()
        pairs = self.register(today)
        self.sync_completions(now)
        missed = self.log_misses([h for _, h in pairs], today)
        log.info("midnight %s: logged %d missed day(s)", today, missed)
        for task, habit in pairs:
            due = parse_due_date(task.get("due"), self.cfg.tz)
            if due is None or due >= today:
                continue
            try:
                if habit.type in (DAILY, X_PER_WEEK):
                    reschedule(self.client, habit, today, today, self.cfg.tz)
                elif habit.type == FIXED:
                    self.db.add_event(habit.task_id, due, MISSED, now)
                    reschedule(self.client, habit, None, today, self.cfg.tz)
            except RescheduleError as exc:
                log.error("%s", exc)
        # Re-fetch: due dates changed, and the stats blocks must reflect the new misses.
        self.write_all(self.register(today), today)
        self.db.set_state("last_midnight", today.isoformat())

    # -- scheduler ---------------------------------------------------------------------------------

    def midnight_pending(self, now: datetime) -> bool:
        return now.time() >= self.cfg.midnight_time and self.db.get_state("last_midnight") != now.date().isoformat()

    def seconds_until_next_wakeup(self, now: datetime) -> float:
        cfg = self.cfg
        next_poll = now + timedelta(minutes=cfg.poll_interval_min)
        mid_day = now.date() if now.time() < cfg.midnight_time else now.date() + timedelta(days=1)
        next_midnight = datetime.combine(mid_day, cfg.midnight_time, tzinfo=cfg.tz)
        # Same-tzinfo aware datetimes subtract on wall-clock time; go through UTC so DST transitions are exact.
        target = min(next_poll.astimezone(timezone.utc), next_midnight.astimezone(timezone.utc))
        return max((target - now.astimezone(timezone.utc)).total_seconds(), 1.0)

    def run_forever(self) -> None:
        log.info("running: poll every %d min, midnight job at %s %s", self.cfg.poll_interval_min, self.cfg.midnight_time, self.cfg.tz)
        while True:
            now = self.now()
            try:
                if self.midnight_pending(now):
                    self.midnight(now)
                else:
                    self.refresh(now)
            except Exception:  # keep the loop alive across API hiccups
                log.exception("job failed")
            time.sleep(self.seconds_until_next_wakeup(self.now()))
