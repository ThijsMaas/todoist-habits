from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .api import TodoistClient
from .config import Config
from .db import Database
from .jobs import Tracker
from .models import classify, habit_from_task, parse_due_date, strip_starting
from .reschedule import RescheduleError, reschedule


def cmd_list(t: Tracker, _: argparse.Namespace) -> None:
    """Dry run: show tracked tasks, how they are classified, and whether the account timezone matches TZ."""
    user = t.client.user()
    tz_info = user.get("tz_info") or {}
    account_tz = tz_info.get("timezone") or user.get("timezone") or "unknown"
    print(f"account timezone: {account_tz}   configured TZ: {t.cfg.tz}" + ("" if account_tz == str(t.cfg.tz) else "   <-- MISMATCH"))
    sources = [(t.cfg.habit_filter, False)] + ([(t.cfg.chore_filter, True)] if t.cfg.chore_filter else [])
    for query, chore in sources:
        print(f"\n{query}:")
        for task in t.client.tasks_by_filter(query):
            due = task.get("due") or {}
            if chore:
                label = "chore (stats only)"
            else:
                kind = classify(task)
                label = f"{kind[0]} (goal {kind[1]}/week)" if kind and kind[1] else (kind[0] if kind else "SKIPPED: not recurring")
            print(f"  {task['id']}  {task['content']!r:40}  due={due.get('date')}  "
                  f"string={strip_starting(due.get('string') or '')!r} lang={due.get('lang')}  -> {label}")


def cmd_sync(t: Tracker, _: argparse.Namespace) -> None:
    now = t.now()
    pairs = t.register(now.date())
    added = t.sync_completions(now)
    print(f"{len(pairs)} habits registered, {added} new completions")


def cmd_render(t: Tracker, _: argparse.Namespace) -> None:
    """Sync, then print the stats blocks without writing to Todoist."""
    now = t.now()
    pairs = t.register(now.date())
    t.sync_completions(now)
    for task, habit in pairs:
        upd = t.plan_update(task, habit, now.date())
        extras = [f"title -> {upd.content!r}"] if upd.content != task["content"] else []
        if upd.priority is not None:
            extras.append(f"priority -> {upd.priority}")
        if upd.hide_until:
            extras.append(f"hide until {upd.hide_until}")
        print(f"== {habit.name} [{habit.type}] {' · '.join(extras)}\n{upd.block}\n")


def cmd_update(t: Tracker, _: argparse.Namespace) -> None:
    t.refresh(t.now())


def cmd_midnight(t: Tracker, _: argparse.Namespace) -> None:
    t.midnight(t.now())


def cmd_run(t: Tracker, _: argparse.Namespace) -> None:
    t.run_forever()


def cmd_check_reschedule(t: Tracker, args: argparse.Namespace) -> None:
    """Live test of the recurrence-preserving reschedule on a dummy task. Prints before/after and exits non-zero on failure."""
    task = t.client.get_task(args.task_id)
    before = task.get("due") or {}
    if not before.get("is_recurring"):
        sys.exit(f"task {args.task_id} is not recurring; give it e.g. 'every day at 8:00' first")
    today = t.now().date()
    habit = habit_from_task(task, t.cfg.tz, today, t.cfg.backfill_days)
    print(f"before: date={before.get('date')} string={before.get('string')!r} lang={before.get('lang')} recurring={before.get('is_recurring')}")
    try:
        after_task, strategy = reschedule(t.client, habit, today, today, t.cfg.tz)
    except RescheduleError as exc:
        sys.exit(f"FAIL: {exc}")
    after = after_task["due"]
    print(f"after:  date={after.get('date')} string={after.get('string')!r} recurring={after.get('is_recurring')}  (strategy {strategy})")
    print(f"OK: due is today ({parse_due_date(after, t.cfg.tz)}), still recurring, time of day "
          + ("preserved" if (len(before.get("date", "")) > 10) == (len(after.get("date", "")) > 10) else "CHANGED"))
    if after.get("string") != before.get("string"):
        print(f"note: due.string changed from {before.get('string')!r} to {after.get('string')!r}; the suffix is stripped before reuse")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="todoist-habits", description="Todoist habit tracker")
    parser.add_argument("--env", type=Path, default=Path(".env"), help="path to .env file")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, fn, doc in [
        ("list", cmd_list, "dry run: list @habit tasks and their classification"),
        ("sync", cmd_sync, "register habits and pull completions into SQLite"),
        ("render", cmd_render, "sync and print stats blocks without writing to Todoist"),
        ("update", cmd_update, "sync and write stats blocks (poll job, run once)"),
        ("midnight", cmd_midnight, "log misses, reschedule overdue habits, write stats (run once)"),
        ("run", cmd_run, "long-running scheduler: poll + midnight job"),
    ]:
        sub.add_parser(name, help=doc).set_defaults(fn=fn)
    p = sub.add_parser("check-reschedule", help="live test: reschedule a dummy recurring task to today")
    p.add_argument("task_id")
    p.set_defaults(fn=cmd_check_reschedule)
    args = parser.parse_args(argv)

    cfg = Config.from_env(args.env)
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    client = TodoistClient(cfg.token, base_url=cfg.api_url)
    db = Database(cfg.db_path)
    try:
        args.fn(Tracker(client, db, cfg), args)
    finally:
        db.close()
        client.close()


if __name__ == "__main__":
    main()
