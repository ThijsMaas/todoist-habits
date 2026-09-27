from datetime import date
from zoneinfo import ZoneInfo

from habits.models import DAILY, FIXED, X_PER_WEEK, base_name, classify, habit_from_task, parse_due_date, strip_starting, to_local

TZ = ZoneInfo("Europe/Amsterdam")


def task(string, labels=("habit",), recurring=True, date_="2026-09-27", **extra):
    return {"id": "1", "content": "Exercise", "labels": list(labels), "priority": 1, "added_at": "2026-09-01T10:00:00Z",
            "due": {"date": date_, "string": string, "is_recurring": recurring, "lang": "en"}, **extra}


def test_classification():
    assert classify(task("every day")) == (DAILY, None)
    assert classify(task("every day at 8:00")) == (DAILY, None)
    assert classify(task("every morning")) == (DAILY, None)
    assert classify(task("daily")) == (DAILY, None)
    assert classify(task("elke dag")) == (DAILY, None)
    assert classify(task("elke! dag")) == (DAILY, None)
    assert classify(task("dagelijks")) == (DAILY, None)
    assert classify(task("elke avond")) == (DAILY, None)
    assert classify(task("elke! 4 dagen")) == (FIXED, None)
    assert classify(task("elke 4 weken zaterdag")) == (FIXED, None)
    assert classify(task("every day starting 2026-09-20")) == (DAILY, None)
    assert classify(task("every day", labels=("habit", "3x-week"))) == (X_PER_WEEK, 3)
    assert classify(task("every mon, wed, fri")) == (FIXED, None)
    assert classify(task("every weekday")) == (FIXED, None)
    assert classify(task("tomorrow", recurring=False)) is None


def test_strip_starting_and_base_name():
    assert strip_starting("every day at 8:00 starting 2026-09-27") == "every day at 8:00"
    assert strip_starting("every day starting on 2026-09-27 08:00") == "every day"
    assert strip_starting("every day") == "every day"
    assert strip_starting("elke dag vanaf 2026-09-27") == "elke dag"
    assert base_name("Exercise (2/3)") == "Exercise"
    assert base_name("Exercise") == "Exercise"


def test_due_date_parsing_forms():
    assert parse_due_date({"date": "2026-09-27"}, TZ) == date(2026, 9, 27)
    assert parse_due_date({"date": "2026-09-27T08:00:00"}, TZ) == date(2026, 9, 27)
    # 23:30Z on the 26th is 01:30 local on the 27th (CEST)
    assert parse_due_date({"date": "2026-09-26T23:30:00Z"}, TZ) == date(2026, 9, 27)
    assert parse_due_date(None, TZ) is None


def test_completion_at_0030_local_counts_for_the_new_day_across_dst():
    # 22:30Z on 26 Sep is 00:30 CEST on 27 Sep
    assert to_local("2026-09-26T22:30:00Z", TZ).date() == date(2026, 9, 27)
    # After DST ends (25 Oct 2026), CET: 23:30Z on 31 Oct is 00:30 on 1 Nov
    assert to_local("2026-10-31T23:30:00Z", TZ).date() == date(2026, 11, 1)
    # 22:30Z on 31 Oct is still 23:30 CET on 31 Oct
    assert to_local("2026-10-31T22:30:00Z", TZ).date() == date(2026, 10, 31)


def test_tracking_since_is_bounded_by_added_date_and_backfill_not_due_date():
    today = date(2026, 9, 27)
    h = habit_from_task(task("every day", date_="2026-09-25"), TZ, today, backfill_days=7)
    assert h.tracking_since == date(2026, 9, 20)  # overdue due date does not raise the bound; backfill wins
    h = habit_from_task(task("every day", date_="2026-09-01"), TZ, today, backfill_days=30)
    assert h.tracking_since == date(2026, 9, 1)  # task creation date wins
    h = habit_from_task(task("every day", date_="2026-09-28"), TZ, today, backfill_days=7)
    assert h.tracking_since == date(2026, 9, 20)  # completed today -> due tomorrow; must not push tracking into the future
