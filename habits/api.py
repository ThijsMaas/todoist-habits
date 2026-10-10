"""Thin client for the unified Todoist API v1 (https://developer.todoist.com/api/v1/)."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any, Iterator

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://api.todoist.com/api/v1/"
RETRY_STATUSES = {429, 500, 502, 503, 504}


class TodoistClient:
    def __init__(self, token: str, base_url: str = BASE_URL, transport: httpx.BaseTransport | None = None):
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    # -- low level -----------------------------------------------------------

    def _request(self, method: str, path: str, *, params: dict | None = None, json_body: dict | None = None) -> Any:
        for attempt in range(4):
            resp = self._http.request(method, path, params=params, json=json_body)
            if resp.status_code in RETRY_STATUSES and attempt < 3:
                delay = float(resp.headers.get("Retry-After", 2 ** attempt))
                log.warning("%s %s -> %s, retrying in %.0fs", method, path, resp.status_code, delay)
                time.sleep(delay)
                continue
            if resp.status_code >= 400:
                raise httpx.HTTPStatusError(
                    f"{method} {path} -> {resp.status_code}: {resp.text[:300]}", request=resp.request, response=resp
                )
            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()
        raise RuntimeError("unreachable")

    def _paginate(self, path: str, params: dict, key: str) -> Iterator[dict]:
        cursor: str | None = None
        while True:
            page = self._request("GET", path, params={**params, **({"cursor": cursor} if cursor else {})})
            yield from page.get(key, [])
            cursor = page.get("next_cursor")
            if not cursor:
                return

    # -- tasks -----------------------------------------------------------------

    def tasks_by_filter(self, query: str) -> list[dict]:
        return list(self._paginate("tasks/filter", {"query": query, "limit": 200}, "results"))

    def get_task(self, task_id: str) -> dict:
        return self._request("GET", f"tasks/{task_id}")

    def update_task(self, task_id: str, **fields: Any) -> dict:
        """POST /tasks/{id}. Fields: content, description, labels, priority, due_string, due_date, due_datetime..."""
        result = self._request("POST", f"tasks/{task_id}", json_body=fields)
        return result if isinstance(result, dict) else self.get_task(task_id)

    # -- activity log ------------------------------------------------------------

    def activities(self, event_types: list[str], date_from: str, date_to: str) -> Iterator[dict]:
        """Activity log events. event_types like "item:completed". Dates are YYYY-MM-DD (or ISO datetimes)."""
        params = {
            "object_event_types": json.dumps(event_types),
            "date_from": date_from,
            "date_to": date_to,
            "limit": 100,
        }
        yield from self._paginate("activities", params, "results")

    # -- reminders -------------------------------------------------------------------

    def add_reminder(self, task_id: str, fire_at: datetime) -> dict:
        """One-off absolute reminder. Sent as a floating local datetime, which Todoist reads in the account timezone.
        It does not follow the task: it stays put (and still fires) when the occurrence is completed."""
        due = {"date": fire_at.strftime("%Y-%m-%dT%H:%M:%S")}
        return self._request("POST", "reminders", json_body={"task_id": task_id, "reminder_type": "absolute", "due": due})

    def delete_reminder(self, reminder_id: str) -> bool:
        """Returns False if the reminder no longer exists (deleted in the app, or with its task)."""
        try:
            self._request("DELETE", f"reminders/{reminder_id}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return False
            raise
        return True

    # -- user ------------------------------------------------------------------------

    def user(self) -> dict:
        return self._request("GET", "user")

    def plan_limits(self) -> dict:
        """Current plan limits (`reminders`, `max_reminders_time`, ...) from the sync endpoint."""
        data = self._request("POST", "sync", json_body={"sync_token": "*", "resource_types": ["user_plan_limits"]})
        return data["user_plan_limits"]["current"]
