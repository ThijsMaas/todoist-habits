from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path

from .models import Habit

COMPLETED = "completed"
MISSED = "missed"

SCHEMA = """
CREATE TABLE IF NOT EXISTS habits (
    task_id           TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    type              TEXT NOT NULL,
    weekly_goal       INTEGER,
    recurrence_string TEXT NOT NULL,
    lang              TEXT NOT NULL DEFAULT 'en',
    created_at        TEXT NOT NULL,
    tracking_since    TEXT NOT NULL,
    base_priority     INTEGER NOT NULL DEFAULT 1,
    priority_raised   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY,
    task_id   TEXT NOT NULL,
    date      TEXT NOT NULL,
    kind      TEXT NOT NULL CHECK (kind IN ('completed', 'missed')),
    timestamp TEXT NOT NULL,
    UNIQUE (task_id, date, kind)
);
CREATE INDEX IF NOT EXISTS events_task_date ON events (task_id, date);
CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reminders (
    reminder_id TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL,
    fire_at     TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path | str):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        if "lang" not in {r["name"] for r in self.conn.execute("PRAGMA table_info(habits)")}:
            self.conn.execute("ALTER TABLE habits ADD COLUMN lang TEXT NOT NULL DEFAULT 'en'")

    def close(self) -> None:
        self.conn.close()

    # -- habits --------------------------------------------------------------

    def upsert_habit(self, habit: Habit) -> Habit:
        """Insert a new habit, or refresh mutable fields of an existing one. Returns the stored habit."""
        existing = self.get_habit(habit.task_id)
        if existing is None:
            self.conn.execute(
                "INSERT INTO habits (task_id, name, type, weekly_goal, recurrence_string, lang, created_at, tracking_since, base_priority)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (habit.task_id, habit.name, habit.type, habit.weekly_goal, habit.recurrence_string, habit.lang,
                 habit.created_at, habit.tracking_since.isoformat(), habit.base_priority),
            )
            self.conn.commit()
            return habit
        # Keep tracking_since and priority bookkeeping; refresh what the user may have edited.
        base_priority = existing.base_priority if existing.priority_raised else habit.base_priority
        self.conn.execute(
            "UPDATE habits SET name=?, type=?, weekly_goal=?, recurrence_string=?, lang=?, base_priority=? WHERE task_id=?",
            (habit.name, habit.type, habit.weekly_goal, habit.recurrence_string, habit.lang, base_priority, habit.task_id),
        )
        self.conn.commit()
        existing.name, existing.type, existing.weekly_goal = habit.name, habit.type, habit.weekly_goal
        existing.recurrence_string, existing.lang, existing.base_priority = habit.recurrence_string, habit.lang, base_priority
        return existing

    def get_habit(self, task_id: str) -> Habit | None:
        row = self.conn.execute("SELECT * FROM habits WHERE task_id=?", (task_id,)).fetchone()
        return self._row_to_habit(row) if row else None

    def all_habits(self) -> list[Habit]:
        return [self._row_to_habit(r) for r in self.conn.execute("SELECT * FROM habits ORDER BY name")]

    def set_priority_state(self, task_id: str, raised: bool, base_priority: int) -> None:
        self.conn.execute("UPDATE habits SET priority_raised=?, base_priority=? WHERE task_id=?",
                          (int(raised), base_priority, task_id))
        self.conn.commit()

    @staticmethod
    def _row_to_habit(row: sqlite3.Row) -> Habit:
        return Habit(
            task_id=row["task_id"], name=row["name"], type=row["type"], weekly_goal=row["weekly_goal"],
            recurrence_string=row["recurrence_string"], lang=row["lang"], created_at=row["created_at"],
            tracking_since=date.fromisoformat(row["tracking_since"]), base_priority=row["base_priority"],
            priority_raised=bool(row["priority_raised"]),
        )

    # -- events ------------------------------------------------------------------

    def add_event(self, task_id: str, day: date, kind: str, timestamp: datetime) -> bool:
        """Idempotent on (task_id, date, kind). A completion supersedes a previously logged miss for that day.
        Returns True if a row was inserted."""
        with self.conn:
            if kind == COMPLETED:
                self.conn.execute("DELETE FROM events WHERE task_id=? AND date=? AND kind=?", (task_id, day.isoformat(), MISSED))
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO events (task_id, date, kind, timestamp) VALUES (?, ?, ?, ?)",
                (task_id, day.isoformat(), kind, timestamp.isoformat()),
            )
        return cur.rowcount == 1

    def remove_latest_completion(self, task_id: str, before: datetime) -> None:
        """Undo the most recent completion at or before `before` (Todoist 'uncompleted' event)."""
        with self.conn:
            self.conn.execute(
                "DELETE FROM events WHERE id = (SELECT id FROM events WHERE task_id=? AND kind=? AND timestamp<=?"
                " ORDER BY timestamp DESC LIMIT 1)",
                (task_id, COMPLETED, before.isoformat()),
            )

    def completions(self, task_id: str) -> list[datetime]:
        rows = self.conn.execute(
            "SELECT timestamp FROM events WHERE task_id=? AND kind='completed' ORDER BY date", (task_id,)
        )
        return [datetime.fromisoformat(r["timestamp"]) for r in rows]

    def event_dates(self, task_id: str, kind: str) -> set[date]:
        rows = self.conn.execute("SELECT date FROM events WHERE task_id=? AND kind=?", (task_id, kind))
        return {date.fromisoformat(r["date"]) for r in rows}

    def last_event_date(self, task_id: str) -> date | None:
        row = self.conn.execute("SELECT MAX(date) AS d FROM events WHERE task_id=?", (task_id,)).fetchone()
        return date.fromisoformat(row["d"]) if row and row["d"] else None

    # -- reminders (only the ones this tracker created) ------------------------------------

    def reminders(self) -> list[tuple[str, str, datetime]]:
        """(reminder_id, task_id, fire_at) for every reminder the tracker created and has not deleted."""
        rows = self.conn.execute("SELECT reminder_id, task_id, fire_at FROM reminders ORDER BY fire_at")
        return [(r["reminder_id"], r["task_id"], datetime.fromisoformat(r["fire_at"])) for r in rows]

    def add_reminder(self, reminder_id: str, task_id: str, fire_at: datetime) -> None:
        self.conn.execute("INSERT INTO reminders (reminder_id, task_id, fire_at) VALUES (?, ?, ?)",
                          (reminder_id, task_id, fire_at.isoformat()))
        self.conn.commit()

    def delete_reminder(self, reminder_id: str) -> None:
        self.conn.execute("DELETE FROM reminders WHERE reminder_id=?", (reminder_id,))
        self.conn.commit()

    # -- state -----------------------------------------------------------------------

    def get_state(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (key, value))
        self.conn.commit()
