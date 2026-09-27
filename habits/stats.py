"""Pure stats functions. All dates are local calendar dates; `today` is injected for testability."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DONE, NOT_DONE = "🟩", "⬜"
WEEK_MET, WEEK_FAILED = "✅", "❌"


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _longest_run(days: list[date]) -> int:
    """Longest run of consecutive calendar days in a sorted, distinct list."""
    best = run = 0
    prev: date | None = None
    for d in days:
        run = run + 1 if prev is not None and d - prev == timedelta(days=1) else 1
        best = max(best, run)
        prev = d
    return best


def _current_run(done: set[date], today: date) -> int:
    d = today if today in done else today - timedelta(days=1)
    n = 0
    while d in done:
        n += 1
        d -= timedelta(days=1)
    return n


def _avg_time_of_day(stamps: list[datetime]) -> str | None:
    """Circular mean of time-of-day so 23:30 and 00:30 average to 00:00, not 12:00."""
    if not stamps:
        return None
    x = y = 0.0
    for s in stamps:
        angle = 2 * math.pi * (s.hour * 60 + s.minute) / 1440
        x += math.cos(angle)
        y += math.sin(angle)
    minutes = round(math.atan2(y, x) / (2 * math.pi) * 1440) % 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _rate(done: set[date], days: list[date]) -> float | None:
    return None if not days else sum(1 for d in days if d in done) / len(days)


def _pct(rate: float | None) -> str:
    return "n/a" if rate is None else f"{round(rate * 100)}%"


# -- daily -----------------------------------------------------------------------------


@dataclass
class DailyStats:
    current_streak: int
    longest_streak: int
    rate7: float | None
    rate30: float | None
    rate90: float | None
    total: int
    avg_time: str | None
    best_weekday: str | None
    worst_weekday: str | None
    heatmap: str


def _evaluated_days(done: set[date], tracking_since: date, today: date, window: int) -> list[date]:
    """Days that count toward a rate: last `window` days within tracking; today only once it's done."""
    start = max(tracking_since, today - timedelta(days=window - 1))
    days = [start + timedelta(days=i) for i in range((today - start).days + 1)]
    return [d for d in days if d != today or d in done]


def daily_stats(completions: list[datetime], tracking_since: date, today: date) -> DailyStats:
    done = {c.date() for c in completions}
    days90 = _evaluated_days(done, tracking_since, today, 90)
    by_weekday: dict[int, list[date]] = {}
    for d in days90:
        by_weekday.setdefault(d.weekday(), []).append(d)
    weekday_rates = {wd: _rate(done, days) for wd, days in by_weekday.items()}
    best = worst = None
    # Needs two full weeks of evaluated days before weekday differences mean anything.
    if len(days90) >= 14 and max(weekday_rates.values()) > min(weekday_rates.values()):
        best = WEEKDAYS[max(weekday_rates, key=lambda wd: (weekday_rates[wd], -wd))]
        worst = WEEKDAYS[min(weekday_rates, key=lambda wd: (weekday_rates[wd], wd))]
    return DailyStats(
        current_streak=_current_run(done, today),
        longest_streak=_longest_run(sorted(done)),
        rate7=_rate(done, _evaluated_days(done, tracking_since, today, 7)),
        rate30=_rate(done, _evaluated_days(done, tracking_since, today, 30)),
        rate90=_rate(done, days90),
        total=len(done),
        avg_time=_avg_time_of_day(completions),
        best_weekday=best,
        worst_weekday=worst,
        heatmap="".join(DONE if today - timedelta(days=i) in done else NOT_DONE for i in range(13, -1, -1)),
    )


def render_daily(s: DailyStats) -> str:
    lines = [
        f"🔥 Streak: {s.current_streak} day{'s' if s.current_streak != 1 else ''} (longest {s.longest_streak})",
        f"📈 Rate: 7d {_pct(s.rate7)} · 30d {_pct(s.rate30)} · 90d {_pct(s.rate90)}",
        f"✅ Total: {s.total}" + (f" · avg time {s.avg_time}" if s.avg_time else ""),
    ]
    if s.best_weekday:
        lines.append(f"📅 Best day: {s.best_weekday} · worst: {s.worst_weekday}")
    lines.append(f"14d: {s.heatmap}")
    return "\n".join(lines)


# -- x per week --------------------------------------------------------------------------


@dataclass
class WeeklyStats:
    goal: int
    this_week: int
    needed: int
    days_left: int  # remaining days in the week including today
    week_streak: int
    pct_weeks_met: float | None
    avg_per_week: float | None
    heatmap: str  # last complete weeks, oldest first
    warning: bool


def weekly_stats(completion_dates: set[date], goal: int, tracking_since: date, today: date, heatmap_weeks: int = 8) -> WeeklyStats:
    this_start = week_start(today)
    counts: dict[date, int] = {}
    for d in completion_dates:
        counts[week_start(d)] = counts.get(week_start(d), 0) + 1
    # Only weeks fully inside the tracking window and already over count as evaluated.
    first_full = tracking_since if tracking_since.weekday() == 0 else week_start(tracking_since) + timedelta(days=7)
    complete_weeks = []
    w = first_full
    while w < this_start:
        complete_weeks.append(w)
        w += timedelta(days=7)
    met = [counts.get(w, 0) >= goal for w in complete_weeks]
    this_week = counts.get(this_start, 0)

    streak = 0
    for ok in reversed(met):
        if not ok:
            break
        streak += 1
    if this_week >= goal:
        streak += 1

    needed = max(goal - this_week, 0)
    days_left = 7 - today.weekday()
    return WeeklyStats(
        goal=goal,
        this_week=this_week,
        needed=needed,
        days_left=days_left,
        week_streak=streak,
        pct_weeks_met=(sum(met) / len(met)) if met else None,
        avg_per_week=(sum(counts.get(w, 0) for w in complete_weeks) / len(complete_weeks)) if complete_weeks else None,
        heatmap="".join(WEEK_MET if ok else WEEK_FAILED for ok in met[-heatmap_weeks:]),
        warning=needed > 0 and days_left - needed <= 1,
    )


def render_weekly(s: WeeklyStats, early_warning: bool) -> str:
    lines = [
        f"🎯 This week: {s.this_week}/{s.goal}" + (" ✅" if s.needed == 0 else ""),
        f"🔥 Week streak: {s.week_streak} · weeks met {_pct(s.pct_weeks_met)}"
        + (f" · avg {s.avg_per_week:.1f}/week" if s.avg_per_week is not None else ""),
    ]
    if s.heatmap:
        lines.append(f"Weeks: {s.heatmap}")
    if early_warning and s.warning:
        lines.append(f"⚠️ {s.needed} more needed in {s.days_left} day{'s' if s.days_left != 1 else ''}")
    return "\n".join(lines)


# -- fixed schedules (e.g. every mon, wed, fri) -------------------------------------------------


@dataclass
class FixedStats:
    current_streak: int  # consecutive completed occurrences
    longest_streak: int
    rate30: float | None  # completed / (completed + missed) by event date
    rate90: float | None
    total: int
    avg_time: str | None
    heatmap: str  # last 14 occurrences


def fixed_stats(completions: list[datetime], misses: set[date], today: date) -> FixedStats:
    done = {c.date() for c in completions}
    occurrences = sorted(done | misses)
    outcomes = [d in done for d in occurrences]
    current = 0
    for ok in reversed(outcomes):
        if not ok:
            break
        current += 1
    longest = run = 0
    for ok in outcomes:
        run = run + 1 if ok else 0
        longest = max(longest, run)

    def rate(window: int) -> float | None:
        start = today - timedelta(days=window - 1)
        recent = [ok for d, ok in zip(occurrences, outcomes) if d >= start]
        return sum(recent) / len(recent) if recent else None

    return FixedStats(
        current_streak=current,
        longest_streak=longest,
        rate30=rate(30),
        rate90=rate(90),
        total=len(done),
        avg_time=_avg_time_of_day(completions),
        heatmap="".join(DONE if ok else NOT_DONE for ok in outcomes[-14:]),
    )


def render_fixed(s: FixedStats) -> str:
    lines = [
        f"🔥 Streak: {s.current_streak} (longest {s.longest_streak})",
        f"📈 Rate: 30d {_pct(s.rate30)} · 90d {_pct(s.rate90)}",
        f"✅ Total: {s.total}" + (f" · avg time {s.avg_time}" if s.avg_time else ""),
    ]
    if s.heatmap:
        lines.append(f"Last: {s.heatmap}")
    return "\n".join(lines)


# -- chores (stats only) ------------------------------------------------------------------------------


@dataclass
class ChoreStats:
    total: int
    last_done: date | None
    days_since: int | None
    avg_interval_days: float | None  # mean gap between consecutive completion days


def chore_stats(completions: list[datetime], today: date) -> ChoreStats:
    days = sorted({c.date() for c in completions})
    gaps = [(b - a).days for a, b in zip(days, days[1:])]
    return ChoreStats(
        total=len(days),
        last_done=days[-1] if days else None,
        days_since=(today - days[-1]).days if days else None,
        avg_interval_days=sum(gaps) / len(gaps) if gaps else None,
    )


def render_chore(s: ChoreStats) -> str:
    if s.last_done is None:
        return "✅ Done 0× (no completions tracked yet)"
    line = f"✅ Done {s.total}× · last {s.last_done} ({s.days_since} day{'s' if s.days_since != 1 else ''} ago)"
    if s.avg_interval_days is not None:
        line += f" · avg every {s.avg_interval_days:.1f} days"
    return line
