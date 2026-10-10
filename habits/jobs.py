from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import httpx

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
    reminders: list[datetime] = field(default_factory=list)  # wanted reminder times today (past ones included)


class Tracker:
    def __init__(self, client: TodoistClient, db: Database, cfg: Config):
        self.client, self.db, self.cfg = client, db, cfg
        self._reminders_allowed: bool | None = None  # plan check, cached until the next midnight job

    def now(self) -> datetime:
        return datetime.now(self.cfg.tz)

    # -- data capture ----------------------------------------------------------------

    def register(self, today: date) -> list[tuple[dict, Habit]]:
        """Fetch habit (and optional chore) tasks and upsert them. Chores are never missed and need no recurrence."""
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
        at_risk = True  # x-per-week: only remind while behind schedule
        due = parse_due_date(task.get("due"), self.cfg.tz)
        if habit.type == X_PER_WEEK:
            ws = weekly_stats({c.date() for c in completions}, habit.weekly_goal, habit.tracking_since, today)
            block = render_weekly(ws, self.cfg.early_warning)
            at_risk = ws.warning
            if self.cfg.title_progress:
                content = f"{habit.name} ({ws.this_week}/{ws.goal})"
            if self.cfg.warn_priority:
                if ws.warning and not habit.priority_raised and task["priority"] != P1:
                    priority = P1
                elif not ws.warning and habit.priority_raised:
                    priority = habit.base_priority
            if self.cfg.hide_after_goal and ws.needed == 0:
                next_monday = week_start(today) + timedelta(days=7)
                if due is not None and due < next_monday:
                    hide_until = next_monday
        elif habit.type == DAILY:
            block = render_daily(daily_stats(completions, habit.tracking_since, today))
        elif habit.type == CHORE:
            block = render_chore(chore_stats(completions, today))
        else:
            block = render_fixed(fixed_stats(completions, self.db.event_dates(habit.task_id, MISSED), today))
        description = OLD_PLUGIN_LINE_RE.sub("", task.get("description") or "")
        # Completing a recurring task moves its due date forward, so "due today or overdue" means "not done yet".
        reminders: list[datetime] = []
        if at_risk and due is not None and due <= today and self.reminders_allowed():
            times = self.cfg.reminder_times.get(priority if priority is not None else task["priority"], ())
            reminders = [datetime.combine(today, t, tzinfo=self.cfg.tz) for t in times]
        return Update(upsert_block(description, block), content, priority, hide_until, block, reminders)

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

    def write_all(self, pairs: list[tuple[dict, Habit]], now: datetime) -> int:
        today = now.date()
        changed = 0
        wanted: dict[str, list[datetime]] = {}
        for task, habit in pairs:
            upd = self.plan_update(task, habit, today)
            changed += self.apply_update(task, habit, upd, today)
            wanted[habit.task_id] = upd.reminders
        self.sync_reminders(wanted, now)
        return changed

    # -- reminders --------------------------------------------------------------------------------

    def reminders_allowed(self) -> bool:
        if self._reminders_allowed is None:
            self._reminders_allowed = self.cfg.reminders and bool(self.client.plan_limits().get("reminders"))
            if self.cfg.reminders and not self._reminders_allowed:
                log.warning("REMINDERS is on, but the Todoist plan does not include reminders; not creating any")
        return self._reminders_allowed

    def sync_reminders(self, wanted: dict[str, list[datetime]], now: datetime) -> None:
        """Make the tracker's own one-off reminders match `wanted` (task id -> fire times today). Todoist fires a
        one-off reminder even after its occurrence is completed, so pending ones are deleted as soon as the task is
        no longer due. Ones that already fired are left alone for the rest of the day and removed afterwards.
        Reminders the user created are never touched, and one the user deleted is not recreated."""
        def name(task_id: str) -> str:
            habit = self.db.get_habit(task_id)
            return repr(habit.name) if habit else f"task {task_id}"

        have = {(task_id, fire_at): rid for rid, task_id, fire_at in self.db.reminders()}
        for (task_id, fire_at), rid in have.items():
            pending = fire_at > now
            if (pending and fire_at not in wanted.get(task_id, ())) or (not pending and fire_at.date() < now.date()):
                try:
                    self.client.delete_reminder(rid)
                except httpx.HTTPError as exc:
                    log.error("could not delete reminder %s: %s", rid, exc)
                    continue
                self.db.delete_reminder(rid)
                if pending:
                    log.info("removed %s reminder for %s", fire_at.strftime("%H:%M"), name(task_id))
        for task_id, times in wanted.items():
            for fire_at in times:
                if fire_at <= now or (task_id, fire_at) in have:
                    continue
                try:
                    reminder = self.client.add_reminder(task_id, fire_at)
                except httpx.HTTPError as exc:
                    log.error("could not add %s reminder for %s: %s", fire_at.strftime("%H:%M"), name(task_id), exc)
                    continue
                self.db.add_reminder(reminder["id"], task_id, fire_at)
                log.info("added %s reminder for %s", fire_at.strftime("%H:%M"), name(task_id))

    # -- jobs ----------------------------------------------------------------------------------

    def refresh(self, now: datetime) -> None:
        """Poll job: register habits, pull completions, rewrite stats blocks, sync reminders."""
        pairs = self.register(now.date())
        self.sync_completions(now)
        self.write_all(pairs, now)

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
        self._reminders_allowed = None  # pick up plan changes once a day
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
        self.write_all(self.register(today), now)
        self.db.set_state("last_midnight", today.isoformat())

    # -- scheduler ---------------------------------------------------------------------------------

    def midnight_pending(self, now: datetime) -> bool:
        return now.time() >= self.cfg.midnight_time and self.db.get_state("last_midnight") != now.date().isoformat()

    def seconds_until_next_wakeup(self, now: datetime) -> float:
        cfg = self.cfg
        next_poll = now + timedelta(minutes=cfg.poll_interval_min)
        mid_day = now.date() if now.time() < cfg.midnight_time else now.date() + timedelta(days=1)
        next_midnight = datetime.combine(mid_day, cfg.midnight_time, tzinfo=cfg.tz)
        # Poll shortly before each pending reminder so a habit completed since the last poll doesn't get notified.
        lead = timedelta(minutes=cfg.reminder_lead_min)
        checks = [fire_at - lead for _, _, fire_at in self.db.reminders() if fire_at - lead > now]
        # Same-tzinfo aware datetimes subtract on wall-clock time; go through UTC so DST transitions are exact.
        target = min(t.astimezone(timezone.utc) for t in [next_poll, next_midnight, *checks])
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
