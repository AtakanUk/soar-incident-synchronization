"""Thin IBM SOAR REST wrapper used by synchronization, migration and backfill."""

from __future__ import annotations

import logging
import time
from typing import Any

from .config import SoarInstanceConfig

RETRY_SLEEP_SECONDS = 2
MAX_RETRIES = 3
LOGGER = logging.getLogger(__name__)


def _as_list(response: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(response, list):
        return response
    if isinstance(response, dict) and isinstance(response.get(key), list):
        return response[key]
    return []


def html_text(text: Any) -> dict[str, str]:
    """Comment/note payload; SOAR returns note text as HTML, so it is posted back as HTML."""
    return {"format": "html", "content": str(text)}


class SoarApiClient:
    """Small facade over ``resilient.SimpleClient`` with retries."""

    def __init__(self, config: SoarInstanceConfig, client: Any = None):
        self.config = config
        self._client = client

    @property
    def name(self) -> str:
        return self.config.name

    def connect(self) -> Any:
        if self._client is None:
            import resilient  # pylint: disable=import-outside-toplevel

            self._client = resilient.get_client(self.config.to_auth())
        return self._client

    def request(self, method: str, uri: str, payload: Any = None, **kwargs: Any) -> Any:
        client = self.connect()
        call = getattr(client, method)
        # A repeated POST could create a record twice; only reads and patches are retried.
        attempts = MAX_RETRIES if method != "post" or "query_paged" in uri else 1
        for attempt in range(1, attempts + 1):
            try:
                if method == "get":
                    return call(uri, **kwargs)
                return call(uri, payload, **kwargs)
            except Exception as exc:
                # 4xx answers will not change on a retry.
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if attempt == attempts or (status is not None and 400 <= status < 500):
                    raise
                LOGGER.warning(
                    "%s %s %s failed (attempt %d): %s", self.name, method.upper(), uri, attempt, exc
                )
                time.sleep(RETRY_SLEEP_SECONDS * attempt)
        raise RuntimeError("unreachable")

    # Incidents ---------------------------------------------------------------

    def get_incident(self, incident_id: int, want_findings: bool = False) -> dict[str, Any]:
        suffix = "?want_findings=true" if want_findings else ""
        incident = self.request("get", f"/incidents/{int(incident_id)}{suffix}")
        if not incident:
            raise LookupError(f"Incident {incident_id} not found on {self.name} SOAR")
        return incident

    def post_incident(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("post", "/incidents", payload)

    def patch_fields(self, incident_id: int, updates: dict[str, Any]) -> Any:
        """Patch top-level fields (``plan_status``) and custom fields (``properties.<name>``).

        SOAR expects custom fields under their plain API name in a patch, so the
        ``properties.`` prefix is only used here to look up the current value.
        """
        import resilient  # pylint: disable=import-outside-toplevel

        incident = self.get_incident(incident_id)
        properties = incident.get("properties") or {}
        patch = resilient.Patch(incident)
        for key, value in updates.items():
            if key.startswith("properties."):
                name = key.split(".", 1)[1]
                patch.add_value(name, value, old_value=properties.get(name))
            else:
                patch.add_value(key, value, old_value=incident.get(key))
        # The other system is the source of truth for these fields, so concurrent edits are overwritten.
        return self.request(
            "patch", f"/incidents/{int(incident_id)}", patch, overwrite_conflict=True
        )

    def query_incidents(
        self,
        *,
        start: int = 0,
        length: int = 100,
        filters: list[dict[str, Any]] | None = None,
        sort_desc: bool = False,
    ) -> list[dict[str, Any]]:
        payload = {
            "filters": filters or [],
            "sorts": [{"field_name": "id", "type": "desc" if sort_desc else "asc"}],
            "start": start,
            "length": length,
        }
        return _as_list(
            self.request("post", "/incidents/query_paged?return_level=normal", payload), "data"
        )

    def find_incident_by_property(self, field_name: str, value: Any) -> int | None:
        filters = [
            {
                "conditions": [
                    {"field_name": f"properties.{field_name}", "method": "equals", "value": value}
                ]
            }
        ]
        found = self.query_incidents(length=1, filters=filters, sort_desc=True)
        return int(found[0]["id"]) if found else None

    # Notes, tasks, artifacts --------------------------------------------------

    def get_comments(self, incident_id: int) -> list[dict[str, Any]]:
        return _as_list(self.request("get", f"/incidents/{int(incident_id)}/comments"), "comments")

    def get_comment(self, incident_id: int, comment_id: int) -> dict[str, Any]:
        return self.request("get", f"/incidents/{int(incident_id)}/comments/{int(comment_id)}")

    def add_comment(self, incident_id: int, text: str) -> Any:
        return self.request(
            "post", f"/incidents/{int(incident_id)}/comments", {"text": html_text(text)}
        )

    def get_tasks(self, incident_id: int) -> list[dict[str, Any]]:
        uri = f"/incidents/{int(incident_id)}/tasks?want_layouts=false&want_notes=true"
        return _as_list(self.request("get", uri), "tasks")

    def create_task(self, incident_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("post", f"/incidents/{int(incident_id)}/tasks", payload)

    def add_task_note(self, task_id: int, text: str) -> Any:
        return self.request("post", f"/tasks/{int(task_id)}/comments", {"text": html_text(text)})

    def get_artifacts(self, incident_id: int) -> list[dict[str, Any]]:
        return _as_list(
            self.request("get", f"/incidents/{int(incident_id)}/artifacts"), "artifacts"
        )

    def add_artifact(self, incident_id: int, payload: dict[str, Any]) -> Any:
        return self.request("post", f"/incidents/{int(incident_id)}/artifacts", payload)

    # Metadata -------------------------------------------------------------------

    def get_field_definitions(self) -> list[dict[str, Any]]:
        try:
            response = self.request("get", "/types/incident/fields")
        except Exception as exc:
            LOGGER.warning(
                "Could not read incident field definitions from %s SOAR: %s", self.name, exc
            )
            return []
        return [item for item in _as_list(response, "fields") if isinstance(item, dict)]
