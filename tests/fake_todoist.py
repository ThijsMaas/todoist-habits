"""In-memory stand-in for the Todoist v1 endpoints the tracker uses, mounted as an httpx transport."""
from __future__ import annotations

import json
from datetime import date, timedelta
from urllib.parse import parse_qs

import httpx

from habits.api import TodoistClient
from habits.models import strip_starting


class FakeTodoist:
    def __init__(self, today: date, plain_string_lands_on: int = 0, starting_unsupported: bool = False, reminders_allowed: bool = True):
        """plain_string_lands_on: day offset from today that a bare recurrence string resolves to (0 = today).
        starting_unsupported: a "... starting <date>" string is parsed as a one-off (recurrence lost).
        reminders_allowed: the plan's `reminders` limit."""
        self.today = today
        self.plain_offset = plain_string_lands_on
        self.starting_unsupported = starting_unsupported
        self.reminders_allowed = reminders_allowed
        self.tasks: dict[str, dict] = {}
        self.activity: list[dict] = []
        self.updates: list[tuple[str, dict]] = []
        self.reminders: dict[str, dict] = {}
        self.reminders_created = 0

    # -- fixtures ------------------------------------------------------------------

    def add_task(self, task_id, content, string, due_date: date, labels=("habit",), description="", priority=1,
                 with_time=False, added: date | None = None, lang="en", project=None):
        """`added` defaults to the due date: a habit task is normally created on its first scheduled day."""
        self.tasks[task_id] = {
            "id": task_id, "content": content, "description": description, "labels": list(labels), "priority": priority,
            "added_at": f"{(added or due_date).isoformat()}T10:00:00Z", "project": project,
            "due": {"date": due_date.isoformat() + ("T08:00:00" if with_time else ""), "string": string, "is_recurring": True, "lang": lang},
        }
        return self.tasks[task_id]

    def complete(self, task_id, at_utc: str, event_type="completed", next_due: date | None = None):
        """next_due: move the task to its next occurrence, as Todoist does when a recurring task is completed."""
        self.activity.append({"id": str(len(self.activity) + 1), "object_type": "item", "object_id": task_id,
                              "event_type": event_type, "event_date": at_utc, "extra_data": {}})
        if next_due is not None:
            self.tasks[task_id]["due"]["date"] = next_due.isoformat()

    def reminder_times(self, task_id) -> list[str]:
        return sorted(r["due"]["date"] for r in self.reminders.values() if r["item_id"] == task_id)

    # -- transport -------------------------------------------------------------------

    def client(self) -> TodoistClient:
        return TodoistClient("fake", transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1/")
        qs = parse_qs(request.url.query.decode())
        if path == "user":
            return httpx.Response(200, json={"tz_info": {"timezone": "Europe/Amsterdam"}})
        if path == "sync":
            return httpx.Response(200, json={"user_plan_limits": {"current": {"reminders": self.reminders_allowed}}})
        if path == "reminders" and request.method == "POST":
            body = json.loads(request.content)
            assert body["reminder_type"] == "absolute" and body["task_id"] in self.tasks
            self.reminders_created += 1
            rid = f"r{self.reminders_created}"
            self.reminders[rid] = {"id": rid, "item_id": body["task_id"], "type": "absolute", "due": body["due"]}
            return httpx.Response(200, json=self.reminders[rid])
        if path.startswith("reminders/") and request.method == "DELETE":
            if self.reminders.pop(path.split("/")[1], None) is None:
                return httpx.Response(404, json={"error": "Reminder not found"})
            return httpx.Response(204)
        if path == "tasks/filter":
            query = qs["query"][0]
            if query.startswith("@"):
                hits = [t for t in self.tasks.values() if query[1:] in t["labels"]]
            elif query.startswith("#"):
                hits = [t for t in self.tasks.values() if t.get("project") == query[1:]]
            else:
                raise AssertionError(f"unsupported filter {query!r}")
            return httpx.Response(200, json={"results": hits, "next_cursor": None})
        if path == "activities":
            types = json.loads(qs["object_event_types"][0])
            since, until = qs["date_from"][0], qs["date_to"][0]
            hits = [e for e in self.activity if f"item:{e['event_type']}" in types and since <= e["event_date"][:10] < until]
            return httpx.Response(200, json={"results": hits, "next_cursor": None})
        if path.startswith("tasks/"):
            task = self.tasks[path.split("/")[1]]
            if request.method == "GET":
                return httpx.Response(200, json=task)
            body = json.loads(request.content)
            self.updates.append((task["id"], body))
            self.apply_update(task, body)
            return httpx.Response(200, json=task)
        return httpx.Response(404, text=path)

    def apply_update(self, task: dict, body: dict) -> None:
        for key in ("content", "description", "labels", "priority"):
            if key in body:
                task[key] = body[key]
        if "due_string" in body:
            s = body["due_string"]
            lang = body.get("due_lang", "en")
            has_time = "at " in strip_starting(s)
            if s.lower().startswith(("every", "elke")):
                assert (lang == "nl") == s.lower().startswith("elke"), f"due_lang {lang!r} does not match {s!r}"
                base = strip_starting(s)
                if base != s:  # "... starting YYYY-MM-DD" / "... vanaf YYYY-MM-DD"
                    assert (" vanaf " in s) == (lang == "nl"), f"wrong 'starting' keyword for {lang}: {s!r}"
                    day = date.fromisoformat(s.rsplit(" ", 1)[1])
                    if self.starting_unsupported:
                        task["due"] = {"date": day.isoformat(), "string": s, "is_recurring": False, "lang": lang}
                        return
                else:
                    day = self.today + timedelta(days=self.plain_offset)
                task["due"] = {"date": day.isoformat() + ("T08:00:00" if has_time else ""), "string": s, "is_recurring": True, "lang": lang}
            else:  # a plain date flattens recurrence, like the real API
                task["due"] = {"date": s, "string": s, "is_recurring": False, "lang": lang}
        if "due_date" in body:
            task["due"] = {"date": body["due_date"], "string": body["due_date"], "is_recurring": False, "lang": "en"}
