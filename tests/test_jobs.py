from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from habits.config import Config
from habits.db import COMPLETED, MISSED, Database
from habits.description import END, START
from habits.jobs import P1, Tracker
from tests.fake_todoist import FakeTodoist

TZ = ZoneInfo("Europe/Amsterdam")
TODAY = date(2026, 9, 30)  # Wednesday
MIDNIGHT = datetime(2026, 9, 30, 0, 5, tzinfo=TZ)


def make_cfg(**over) -> Config:
    base = dict(token="x", api_url="http://fake/api/v1/", tz=TZ, habit_filter="@habit", chore_filter="", poll_interval_min=10,
                backfill_days=7, db_path=Path(":memory:"), midnight_time=time(0, 5),
                hide_after_goal=True, early_warning=True, warn_priority=True, title_progress=True, log_level="INFO")
    return Config(**{**base, **over})


@pytest.fixture
def fake():
    return FakeTodoist(TODAY)


def tracker(fake: FakeTodoist, **over) -> Tracker:
    return Tracker(fake.client(), Database(":memory:"), make_cfg(**over))


def utc(d: date, hh=7, mm=0) -> str:
    return f"{d.isoformat()}T{hh:02d}:{mm:02d}:00Z"


def at(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=TZ)


# -- acceptance: 3-day outage -> 3 misses, rescheduled to today, still recurring with time --------------------


def test_three_day_outage_logs_three_misses_and_reschedules_daily_habit(fake):
    fake.add_task("d1", "Meditate", "every day at 8:00", TODAY - timedelta(days=3), with_time=True, description="my own notes",
                  added=TODAY - timedelta(days=6))
    for back in (6, 5, 4):
        fake.complete("d1", utc(TODAY - timedelta(days=back)))
    t = tracker(fake)
    t.midnight(MIDNIGHT)

    assert t.db.event_dates("d1", MISSED) == {TODAY - timedelta(days=3), TODAY - timedelta(days=2), TODAY - timedelta(days=1)}
    assert t.db.event_dates("d1", COMPLETED) == {TODAY - timedelta(days=b) for b in (6, 5, 4)}
    due = fake.tasks["d1"]["due"]
    assert due["date"] == f"{TODAY}T08:00:00" and due["is_recurring"] and due["string"].startswith("every day at 8:00")
    desc = fake.tasks["d1"]["description"]
    assert desc.startswith("my own notes\n\n" + START) and desc.endswith(END)
    assert "Streak: 0 days (longest 3)" in desc
    assert t.db.get_state("last_midnight") == TODAY.isoformat()


def test_midnight_is_idempotent(fake):
    fake.add_task("d1", "Meditate", "every day", TODAY - timedelta(days=2))
    t = tracker(fake)
    t.midnight(MIDNIGHT)
    n = len(fake.updates)
    t.midnight(MIDNIGHT)
    assert t.db.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
    assert len(fake.updates) == n  # nothing overdue and description unchanged -> no API writes


def test_starting_suffix_is_primary_and_string_is_canonicalised():
    fake = FakeTodoist(TODAY, plain_string_lands_on=1)  # bare string would land tomorrow: must not be used first
    fake.add_task("d1", "Meditate", "every day at 8:00", TODAY - timedelta(days=1), with_time=True)
    t = tracker(fake)
    t.midnight(MIDNIGHT)
    due = fake.tasks["d1"]["due"]
    assert due["date"] == f"{TODAY}T08:00:00" and due["is_recurring"]
    assert [b["due_string"] for _, b in fake.updates if "due_string" in b] == [f"every day at 8:00 starting {TODAY}"]
    assert t.db.get_habit("d1").recurrence_string == "every day at 8:00"  # canonical string never carries the suffix


def test_falls_back_to_bare_string_when_starting_keyword_is_not_understood():
    fake = FakeTodoist(TODAY, starting_unsupported=True)
    fake.add_task("d1", "Meditate", "every day", TODAY - timedelta(days=1))
    tracker(fake).midnight(MIDNIGHT)
    due = fake.tasks["d1"]["due"]
    assert due["date"] == TODAY.isoformat() and due["is_recurring"] and due["string"] == "every day"
    assert [b["due_string"] for _, b in fake.updates if "due_string" in b] == [f"every day starting {TODAY}", "every day"]


def test_never_sends_due_date_only(fake):
    fake.add_task("d1", "Meditate", "every day", TODAY - timedelta(days=1))
    tracker(fake).midnight(MIDNIGHT)
    assert all("due_date" not in b and "due_datetime" not in b for _, b in fake.updates)


# -- x-per-week -----------------------------------------------------------------------------------------


def test_x_per_week_overdue_is_moved_without_miss(fake):
    fake.add_task("w1", "Exercise", "every day", TODAY - timedelta(days=2), labels=("habit", "3x-week"))
    fake.complete("w1", utc(TODAY - timedelta(days=2)))  # Monday
    t = tracker(fake)
    t.midnight(MIDNIGHT)
    assert t.db.event_dates("w1", MISSED) == set()
    assert fake.tasks["w1"]["due"]["date"] == TODAY.isoformat() and fake.tasks["w1"]["due"]["is_recurring"]
    assert fake.tasks["w1"]["content"] == "Exercise (1/3)"
    assert "This week: 1/3" in fake.tasks["w1"]["description"]


def test_week_failed_only_when_goal_unmet_by_sunday(fake):
    last_monday = TODAY - timedelta(days=9)
    sunday = last_monday + timedelta(days=6)
    fake.add_task("w1", "Exercise", "every day", last_monday, labels=("habit", "2x-week"))
    fake.complete("w1", utc(last_monday))
    t = tracker(fake, backfill_days=14)  # tracking_since = due date = last Monday
    t.refresh(at(sunday, 12))  # Sunday noon: 1/2, week still open -> no verdict yet
    desc = fake.tasks["w1"]["description"]
    assert "This week: 1/2" in desc and "Weeks:" not in desc
    fake.complete("w1", utc(sunday, 21, 30))  # 23:30 local Sunday: still counts for the week
    t.refresh(at(sunday + timedelta(days=1), 0, 30))
    assert "Weeks: ✅" in fake.tasks["w1"]["description"] and "This week: 0/2" in fake.tasks["w1"]["description"]


def test_week_unmet_by_sunday_is_failed(fake):
    last_monday = TODAY - timedelta(days=9)
    fake.add_task("w1", "Exercise", "every day", last_monday, labels=("habit", "2x-week"))
    fake.complete("w1", utc(last_monday))
    t = tracker(fake, backfill_days=14)
    t.refresh(at(TODAY, 12))
    assert "Weeks: ❌" in fake.tasks["w1"]["description"]


def test_early_warning_raises_priority_and_restores_after_goal(fake):
    friday = date(2026, 10, 2)
    fake.add_task("w1", "Exercise", "every day", friday, labels=("habit", "3x-week"), priority=2)
    t = tracker(fake)
    t.refresh(at(friday, 12))
    assert fake.tasks["w1"]["priority"] == P1
    assert "⚠️ 3 more needed in 3 days" in fake.tasks["w1"]["description"]
    assert t.db.get_habit("w1").priority_raised and t.db.get_habit("w1").base_priority == 2

    for d in (friday, friday + timedelta(days=1), friday + timedelta(days=2)):
        fake.complete("w1", utc(d, 10))
    t.refresh(at(friday + timedelta(days=2), 20))
    assert fake.tasks["w1"]["priority"] == 2
    assert not t.db.get_habit("w1").priority_raised
    assert "⚠️" not in fake.tasks["w1"]["description"]
    assert "This week: 3/3 ✅" in fake.tasks["w1"]["description"]


def test_hide_after_goal_pushes_to_next_monday(fake):
    fake.add_task("w1", "Exercise", "every day", TODAY, labels=("habit", "2x-week"))
    fake.complete("w1", utc(TODAY - timedelta(days=1)))
    fake.complete("w1", utc(TODAY))
    t = tracker(fake)
    t.refresh(at(TODAY, 12))
    next_monday = date(2026, 10, 5)
    assert fake.tasks["w1"]["due"]["date"] == next_monday.isoformat()
    assert fake.tasks["w1"]["due"]["is_recurring"]
    # hidden task is not overdue at the next midnight, so it stays put and gets no miss
    t.midnight(MIDNIGHT + timedelta(days=1))
    assert fake.tasks["w1"]["due"]["date"] == next_monday.isoformat()
    assert t.db.event_dates("w1", MISSED) == set()


def test_hide_after_goal_disabled_leaves_due_alone(fake):
    fake.add_task("w1", "Exercise", "every day", TODAY, labels=("habit", "1x-week"))
    fake.complete("w1", utc(TODAY))
    tracker(fake, hide_after_goal=False, title_progress=False).refresh(at(TODAY, 12))
    assert fake.tasks["w1"]["due"]["date"] == TODAY.isoformat()
    assert fake.tasks["w1"]["content"] == "Exercise"


# -- fixed schedules ------------------------------------------------------------------------------------


def test_fixed_schedule_logs_miss_and_advances_without_moving_to_today():
    fake = FakeTodoist(TODAY, plain_string_lands_on=2)  # "every mon, wed, fri" resolving to Friday
    monday = TODAY - timedelta(days=2)
    fake.add_task("f1", "Gym", "every mon, wed, fri", monday)
    t = tracker(fake)
    t.midnight(MIDNIGHT)
    assert t.db.event_dates("f1", MISSED) == {monday}
    assert fake.tasks["f1"]["due"]["date"] == (TODAY + timedelta(days=2)).isoformat()
    assert fake.tasks["f1"]["due"]["string"] == "every mon, wed, fri"


# -- project-based selection, Dutch recurrences, chores, old-plugin residue --------------------------------------


def test_project_filters_dutch_daily_and_stats_only_chores(fake):
    fake.add_task("h1", "Check Pil 💊", "elke dag", TODAY - timedelta(days=2), labels=["✅_streak"], lang="nl", project="Habits",
                  description="**Current streak:** 4 days")
    fake.add_task("c1", "Planten water geven", "elke! 4 dagen", TODAY - timedelta(days=3), labels=["Huis"], lang="nl", project="Chores",
                  description="**Current streak:** 1 day\nmy note")
    fake.complete("c1", utc(TODAY - timedelta(days=6)))
    fake.complete("c1", utc(TODAY - timedelta(days=2)))
    t = tracker(fake, habit_filter="#Habits", chore_filter="#Chores")
    t.midnight(MIDNIGHT)

    h1, c1 = t.db.get_habit("h1"), t.db.get_habit("c1")
    assert (h1.type, h1.lang, h1.recurrence_string) == ("daily", "nl", "elke dag")
    assert c1.type == "chore"
    # daily habit: misses logged, rescheduled with due_lang=nl, old plugin line removed, block written
    assert t.db.event_dates("h1", MISSED) == {TODAY - timedelta(days=2), TODAY - timedelta(days=1)}
    assert fake.tasks["h1"]["due"]["date"] == TODAY.isoformat() and fake.tasks["h1"]["due"]["is_recurring"]
    assert "Current streak:**" not in fake.tasks["h1"]["description"]
    assert fake.tasks["h1"]["description"].startswith(START)
    # chore: never rescheduled, never missed; residue removed but the user's note kept
    assert fake.tasks["c1"]["due"]["date"] == (TODAY - timedelta(days=3)).isoformat()
    assert t.db.event_dates("c1", MISSED) == set()
    assert fake.tasks["c1"]["description"].startswith("my note\n\n" + START)
    assert "✅ Done 2× · last 2026-09-28 (2 days ago) · avg every 4.0 days" in fake.tasks["c1"]["description"]
    assert all(("due_string" not in b) for tid, b in fake.updates if tid == "c1")


def test_dutch_strategy_b_uses_vanaf():
    fake = FakeTodoist(TODAY, plain_string_lands_on=1)
    fake.add_task("h1", "Lezen", "elke dag", TODAY - timedelta(days=1), lang="nl")
    tracker(fake).midnight(MIDNIGHT)
    due = fake.tasks["h1"]["due"]
    assert due["date"] == TODAY.isoformat() and due["string"] == f"elke dag vanaf {TODAY}" and due["lang"] == "nl"

# -- completion sync -------------------------------------------------------------------------------------


def test_completion_at_0030_local_counts_for_new_day_and_uncomplete_retracts(fake):
    fake.add_task("d1", "Meditate", "every day", TODAY)
    fake.complete("d1", utc(TODAY - timedelta(days=1), 22, 30))  # 00:30 CEST on TODAY
    t = tracker(fake)
    t.refresh(at(TODAY, 1))
    assert t.db.event_dates("d1", COMPLETED) == {TODAY}
    fake.complete("d1", utc(TODAY - timedelta(days=1), 22, 45), event_type="uncompleted")
    t.refresh(at(TODAY, 2))
    assert t.db.event_dates("d1", COMPLETED) == set()


def test_late_completion_clears_logged_miss(fake):
    fake.add_task("d1", "Meditate", "every day", TODAY - timedelta(days=1))
    t = tracker(fake)
    t.midnight(MIDNIGHT)
    assert t.db.event_dates("d1", MISSED) == {TODAY - timedelta(days=1)}
    fake.complete("d1", utc(TODAY - timedelta(days=1), 21))  # event arrives after the midnight run
    t.refresh(MIDNIGHT + timedelta(hours=1))
    assert t.db.event_dates("d1", MISSED) == set() and t.db.event_dates("d1", COMPLETED) == {TODAY - timedelta(days=1)}


def test_non_recurring_habit_is_skipped(fake):
    fake.add_task("x", "Oops", "today", TODAY)
    fake.tasks["x"]["due"]["is_recurring"] = False
    assert tracker(fake).register(TODAY) == []


# -- scheduler timing ----------------------------------------------------------------------------------------


def test_wakeup_crosses_dst_correctly():
    t = tracker(FakeTodoist(TODAY), poll_interval_min=25 * 60)
    # 25 Oct 2026 01:30 CEST (23:30Z). Clocks go back at 03:00 CEST. Next 00:05 CET on 26 Oct is 23:05Z: 23h35m away.
    now = datetime(2026, 10, 25, 1, 30, tzinfo=TZ)
    assert t.seconds_until_next_wakeup(now) == pytest.approx((23 + 35 / 60) * 3600)


def test_midnight_pending_after_downtime():
    t = tracker(FakeTodoist(TODAY))
    assert t.midnight_pending(at(TODAY, 15))  # never ran -> catch up now
    t.db.set_state("last_midnight", TODAY.isoformat())
    assert not t.midnight_pending(at(TODAY, 15))
    assert not t.midnight_pending(at(TODAY + timedelta(days=1), 0, 2))
    assert t.midnight_pending(at(TODAY + timedelta(days=1), 0, 5))
