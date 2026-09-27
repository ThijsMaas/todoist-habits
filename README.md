# todoist-habits

Self-hosted habit tracker on top of Todoist. Tracks completions of selected tasks in its own SQLite database, writes
stats into each task's description, reschedules missed daily habits to today (keeping the recurrence), and supports
"N times per week" habits that Todoist can't do natively.

Tasks are selected with Todoist filter queries: `HABIT_FILTER` (default `@habit`; e.g. `#Habits` for a project) and an
optional `CHORE_FILTER` (e.g. `#Chores`) for stats-only tracking. Recurrence strings are understood in English and
Dutch (`every day`, `elke dag`, `dagelijks`, `elke avond`…); other languages fall back to "fixed".

## Task types

| Type | How to set up | Behaviour |
|---|---|---|
| daily | recurring `every day` / `every day at 8:00` / `every morning` / `daily` (or Dutch equivalents) matched by `HABIT_FILTER` | every tracked day without a completion is a miss; overdue task is moved to today at 00:05 |
| x-per-week | `every day` + label `@3x-week` (any `@<N>x-week`) | never a daily miss; week (Mon–Sun) is met if ≥ N distinct days completed; overdue task is moved to today silently |
| fixed | any other recurrence (`every mon, wed, fri`) | overdue occurrence is logged as a miss and the task advances to its next scheduled day |
| chore | anything matched by `CHORE_FILTER` | stats only (total, last done, average interval); never rescheduled, never marked missed |

A non-recurring habit task is skipped with a warning (`list` shows it). Leftover `**Current streak:** …` lines from the
Todoist habit-tracker plugin this replaces are removed from tracked descriptions.

## Stats block

Written between `--- habit stats ---` / `--- end habit stats ---` in the description. Only that region is ever
rewritten; text you put above or below it stays.

```
--- habit stats ---
🔥 Streak: 12 days (longest 31)
📈 Rate: 7d 86% · 30d 90% · 90d 81%
✅ Total: 73 · avg time 07:42
📅 Best day: Tue · worst: Sat
14d: 🟩🟩⬜🟩🟩🟩🟩🟩🟩🟩🟩⬜🟩🟩
--- end habit stats ---
```

x-per-week:

```
🎯 This week: 2/3
🔥 Week streak: 4 · weeks met 80% · avg 3.2/week
Weeks: ✅✅❌✅✅
⚠️ 1 more needed in 2 days
```

chore:

```
✅ Done 12× · last 2026-09-24 (3 days ago) · avg every 4.5 days
```

### x-per-week extras (each toggleable in `.env`)

- `TITLE_PROGRESS`: task title becomes `Exercise (2/3)`.
- `EARLY_WARNING`: when the days left in the week are about equal to the sessions still needed (`days_left − needed ≤ 1`)
  the block gets `⚠️ N more needed in D days`; with `WARN_PRIORITY` the task is also raised to P1 and restored to its
  previous priority once the warning clears.
- `HIDE_AFTER_GOAL`: once the goal is met the task is pushed to next Monday so it disappears for the rest of the week.

## Setup

```sh
cp .env.example .env          # add TODOIST_API_TOKEN, check TZ
uv venv && uv pip install -e ".[dev]"
todoist-habits list           # dry run: classification + timezone check
todoist-habits render         # sync completions, print blocks, write nothing
```

### Verify rescheduling on a dummy task first

Todoist's API cannot move a recurring task's date without touching the pattern; sending `due_date` alone turns it into
a one-off. The tracker sends `"<recurrence> starting <date>"` (`vanaf` for Dutch) with `due_lang` and verifies the
returned task is still recurring and due on the target date. Verified live: Todoist keeps the time and normalises
`due.string` back to the bare pattern. If that fails it falls back to resubmitting the bare recurrence string (which
resolves to the next occurrence from *now*, i.e. tomorrow when run in the afternoon) and verifies again. Prove this on
a throwaway task before enabling the midnight job:

```sh
todoist-habits check-reschedule <task_id>
```

Expected: `after: date=<today>… recurring=True`; `due.string` unchanged (Todoist normalises the `starting` suffix away).

### Commands

| Command | What it does |
|---|---|
| `list` | dry run: classification of every matched task + account/`TZ` mismatch check |
| `sync` | register habits and pull completions into SQLite |
| `render` | sync, then print the stats blocks without writing to Todoist |
| `update` | the poll job once: sync + write blocks/titles/priorities |
| `midnight` | the midnight job once: log misses, reschedule overdue habits, write blocks |
| `check-reschedule <task_id>` | live reschedule test on one task |
| `run` | long-running scheduler: `update` every `POLL_INTERVAL` min, `midnight` at `MIDNIGHT_TIME` |

`run` persists the date of the last midnight job, so after downtime it runs the midnight job immediately on start and
fills in every missed day. `update` and `midnight` exist for running the two jobs from cron instead.

### Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `TODOIST_API_TOKEN` | — | personal API token (Settings → Integrations → Developer) |
| `TZ` | `Europe/Amsterdam` | must equal the Todoist account timezone |
| `HABIT_FILTER` | `@habit` | Todoist filter query selecting habit tasks (e.g. `#Habits`) |
| `CHORE_FILTER` | empty | filter query for stats-only chores (e.g. `#Chores`) |
| `POLL_INTERVAL` | `10` | minutes between completion polls |
| `MIDNIGHT_TIME` | `00:05` | local time of the midnight job |
| `BACKFILL_DAYS` | `7` | how far back completions are pulled when a task is first seen; activity-log retention is plan dependent |
| `HIDE_AFTER_GOAL` / `EARLY_WARNING` / `WARN_PRIORITY` / `TITLE_PROGRESS` | `true` | x-per-week extras, see above |
| `DB_PATH` | `habits.db` | SQLite file (`/data/habits.db` in Docker) |
| `LOG_LEVEL` | `INFO` | |

## Running in Docker

```sh
docker compose up -d --build
docker logs -f todoist-habits
docker exec todoist-habits todoist-habits render   # what would be written, from the container's DB
```

`compose.yaml` reads `.env`, runs as uid 1000 and stores the SQLite file in the `/data` volume (`/opt/appdata/todoist-habits`
by default; change the bind mount to taste). The DB survives rebuilds; deleting it loses history older than
`BACKFILL_DAYS`. Server-specific deployment notes belong in `DEPLOY.md`, which is gitignored.

## How it works

- **Completions** come from the activity log (`GET /api/v1/activities?object_event_types=["item:completed"]`), not
  from `/tasks/completed/...`, which omits recurring completions. Each poll re-reads a one-day overlap; inserts are
  idempotent on `(task, date, kind)`. `item:uncompleted` retracts the latest completion.
- **Dates** are the local calendar date in `TZ` of the completion timestamp (a 00:30 completion counts for the new
  day, DST included). Weeks are Monday–Sunday.
- **Misses** (daily) are derived from our own event table at midnight: every date from `tracking_since` to yesterday
  without a completion. Postponing therefore counts as a miss. `tracking_since` = max(task creation, first sync −
  `BACKFILL_DAYS`); the due date is deliberately not used (a task completed today is due tomorrow).
- **Writes** happen only when the description/title/priority would actually change, so polling doesn't spam updates.
  Writing a description triggers `item:updated`, never `item:completed`, so there is no feedback loop.

## Tests

```sh
pytest
```

Fixed-date unit tests for stats, description marker handling, task classification and DST date attribution, plus
end-to-end job tests against an in-memory fake of the Todoist endpoints (3-day outage → 3 misses, x-per-week without
miss, strategy fallback, priority raise/restore, hide-after-goal, Dutch recurrences, stats-only chores).

## License

MIT
