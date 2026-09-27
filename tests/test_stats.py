from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from habits.stats import daily_stats, fixed_stats, render_weekly, weekly_stats

TZ = ZoneInfo("Europe/Amsterdam")
TODAY = date(2026, 9, 30)  # Wednesday


def dt(d: date, hh=9, mm=0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=TZ)


def days_back(*offsets: int) -> list[datetime]:
    return [dt(TODAY - timedelta(days=o)) for o in offsets]


def test_daily_streaks_and_rates():
    # done: today, yesterday, -2 ; gap at -3 ; done -4, -5, -6, -7
    s = daily_stats(days_back(0, 1, 2, 4, 5, 6, 7), tracking_since=TODAY - timedelta(days=29), today=TODAY)
    assert s.current_streak == 3
    assert s.longest_streak == 4
    assert s.total == 7
    assert s.rate7 == 6 / 7  # window includes today because today is done
    assert s.rate30 == 7 / 30
    assert s.heatmap.endswith("🟩🟩🟩") and s.heatmap[-4] == "⬜"
    assert len(s.heatmap) == 14


def test_daily_today_not_done_excludes_today_from_rate_and_streak_counts_from_yesterday():
    s = daily_stats(days_back(1, 2), tracking_since=TODAY - timedelta(days=6), today=TODAY)
    assert s.current_streak == 2
    assert s.rate7 == 2 / 6  # 6 evaluated days: tracking_since..yesterday


def test_daily_rate_bounded_by_tracking_since():
    s = daily_stats(days_back(1), tracking_since=TODAY - timedelta(days=1), today=TODAY)
    assert s.rate7 == 1.0 and s.rate90 == 1.0


def test_daily_weekday_best_worst():
    since = TODAY - timedelta(days=27)  # 4 full weeks
    done = [dt(d) for i in range(28) if (d := since + timedelta(days=i)).weekday() in (0, 1, 2, 3, 4)]  # weekdays only
    s = daily_stats(done, tracking_since=since, today=TODAY)
    assert s.best_weekday in {"Mon", "Tue", "Wed", "Thu", "Fri"}
    assert s.worst_weekday in {"Sat", "Sun"}


def test_average_time_is_circular():
    s = daily_stats([dt(TODAY, 23, 30), dt(TODAY - timedelta(days=1), 0, 30)], TODAY - timedelta(days=5), TODAY)
    assert s.avg_time == "00:00"


def test_weekly_progress_streak_and_warning():
    # tracking since Monday 2026-08-31; goal 3; weeks: met, met, failed, met ; this week (Mon 28 Sep) 2 done
    since = date(2026, 8, 31)
    done = set()
    for wk, n in enumerate([3, 4, 2, 3]):
        for i in range(n):
            done.add(since + timedelta(weeks=wk, days=i))
    done |= {date(2026, 9, 28), date(2026, 9, 29)}
    s = weekly_stats(done, goal=3, tracking_since=since, today=TODAY)
    assert (s.this_week, s.goal, s.needed, s.days_left) == (2, 3, 1, 5)
    assert s.week_streak == 1
    assert s.pct_weeks_met == 0.75
    assert s.avg_per_week == 3.0
    assert s.heatmap == "✅✅❌✅"
    assert s.warning is False


def test_weekly_warning_when_days_left_about_equal_needed():
    friday = date(2026, 10, 2)
    s = weekly_stats(set(), goal=3, tracking_since=date(2026, 9, 28), today=friday)  # 3 days left, 3 needed
    assert s.warning and s.days_left == 3 and s.needed == 3
    assert "⚠️ 3 more needed in 3 days" in render_weekly(s, early_warning=True)
    assert "⚠️" not in render_weekly(s, early_warning=False)
    thursday = date(2026, 10, 1)
    assert weekly_stats(set(), goal=2, tracking_since=date(2026, 9, 28), today=thursday).warning is False  # 4 left, 2 needed


def test_weekly_goal_met_counts_in_streak_and_no_warning():
    s = weekly_stats({date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30)}, 3, date(2026, 9, 28), TODAY)
    assert s.needed == 0 and s.warning is False and s.week_streak == 1 and s.heatmap == ""


def test_weekly_partial_first_week_not_evaluated():
    # tracking starts Wednesday: that week must not count as a failed week
    s = weekly_stats(set(), goal=3, tracking_since=date(2026, 9, 16), today=TODAY)
    assert s.heatmap == "❌"  # only the full week of 21 Sep is evaluated


def test_fixed_stats_use_logged_misses():
    done = [dt(date(2026, 9, 28)), dt(date(2026, 9, 25)), dt(date(2026, 9, 21))]
    misses = {date(2026, 9, 23)}
    s = fixed_stats(done, misses, TODAY)
    assert s.current_streak == 2 and s.longest_streak == 2 and s.total == 3
    assert s.rate30 == 0.75
    assert s.heatmap == "🟩⬜🟩🟩"
