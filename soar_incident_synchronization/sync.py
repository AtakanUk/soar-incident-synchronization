"""Day-to-day synchronization between the MSSP and the customer SOAR instance.

Each action is triggered for one incident (normally by a playbook on the side
where the change happened) and applies the change to the linked incident on
the other side. The link is a pair of custom fields holding the other side's
incident ID.
"""

from __future__ import annotations

import copy
import traceback
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from .client import SoarApiClient
from .config import AppConfig, SyncDirection
from .logging_utils import get_logger
from .mappings import FieldValueMapper, MappingError
from .notifier import send_error_mail

TASK_FIELDS = ("name", "instructions", "due_date", "required", "phase_id")
ARTIFACT_FIELDS = ("type", "value", "description", "properties", "hash", "relating")


@dataclass
class SyncResult:
    action: str
    source_incident_id: int
    target_incident_id: int | None = None
    status: str = "done"
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def copy_fields(record: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    return {
        name: copy.deepcopy(record[name])
        for name in names
        if record.get(name) not in (None, "", [])
    }


class IncidentSynchronizer:
    """Synchronize linked incidents between the MSSP and customer SOAR instances."""

    CREATE_FIELDS = (
        "name",
        "description",
        "discovered_date",
        "create_date",
        "plan_status",
        "resolution_summary",
        "reporter",
        "state",
        "country",
        "zip",
    )

    def __init__(
        self,
        config: AppConfig,
        mssp_client: SoarApiClient,
        customer_client: SoarApiClient,
        mappers: dict[SyncDirection, FieldValueMapper] | None = None,
    ) -> None:
        self.config = config
        self.fields = config.fields
        self.mssp = mssp_client
        self.customer = customer_client
        self._mappers: dict[SyncDirection, FieldValueMapper] = dict(mappers or {})
        self.logger = get_logger("incident_sync", config.logging)

    # Helpers --------------------------------------------------------------------

    def mapper(self, direction: SyncDirection) -> FieldValueMapper:
        """Value mapper for one direction, discovered from field metadata on first use."""
        if direction not in self._mappers:
            source, target = self._clients(direction)
            resolution = self.fields.customer_resolution_field
            extra = (
                [("resolution_id", resolution)]
                if direction == SyncDirection.MSSP_TO_CUSTOMER
                else [(resolution, "resolution_id")]
            )
            self._mappers[direction] = FieldValueMapper.discover(
                source, target, extra, self.fields.override_maps.get(direction.value)
            )
        return self._mappers[direction]

    def _clients(self, direction: SyncDirection) -> tuple[SoarApiClient, SoarApiClient]:
        if direction == SyncDirection.MSSP_TO_CUSTOMER:
            return self.mssp, self.customer
        return self.customer, self.mssp

    def _link_fields(self, direction: SyncDirection) -> tuple[str, str]:
        """(field on the source holding the target ID, field on the target holding the source ID)."""
        if direction == SyncDirection.MSSP_TO_CUSTOMER:
            return self.fields.customer_reference_field, self.fields.mssp_reference_field
        return self.fields.mssp_reference_field, self.fields.customer_reference_field

    def _counterpart_id(self, incident: dict[str, Any], direction: SyncDirection) -> int | None:
        value = (incident.get("properties") or {}).get(self._link_fields(direction)[0])
        return int(value) if value not in (None, "") else None

    def _require_counterpart(self, incident: dict[str, Any], direction: SyncDirection) -> int:
        target_id = self._counterpart_id(incident, direction)
        if target_id is None:
            raise LookupError(
                f"Incident {incident.get('id')} has no linked incident "
                f"({self._link_fields(direction)[0]} is empty)"
            )
        return target_id

    def _guarded(self, action: str, incident_id: int, run: Callable[[], SyncResult]) -> SyncResult:
        try:
            result = run()
        except Exception as error:
            self.logger.error(
                "%s failed for incident %s: %s", action, incident_id, error, exc_info=True
            )
            send_error_mail(
                self.config.notifications,
                component_name="incident_sync",
                function_name=action,
                error_details={
                    "Incident ID": incident_id,
                    "Error Type": type(error).__name__,
                    "Message": str(error),
                    "Traceback": traceback.format_exc(),
                },
            )
            raise
        self.logger.info(
            "%s incident %s -> %s: %s %s",
            action,
            incident_id,
            result.target_incident_id,
            result.status,
            result.details,
        )
        return result

    def run(
        self, action: str, incident_id: int, direction: SyncDirection, note_id: int | None = None
    ) -> SyncResult:
        """Dispatch by action name (used by the CLI and the SOAR function)."""
        actions = {
            "create": lambda: self.create_counterpart(incident_id, direction),
            "severity": lambda: self.sync_severity(incident_id, direction),
            "close": lambda: self.sync_close(incident_id, direction),
            "reopen": lambda: self.sync_reopen(incident_id, direction),
            "note": lambda: self.sync_note(incident_id, direction, note_id=note_id),
            "report": lambda: self.sync_report(incident_id, direction),
            "analyst": lambda: self.sync_analyst_assignment(incident_id),
        }
        if action not in actions:
            raise ValueError(f"Unknown action {action!r}; expected one of {', '.join(actions)}")
        return actions[action]()

    # Creation ---------------------------------------------------------------------

    def create_counterpart(
        self,
        source_incident_id: int,
        direction: SyncDirection,
        *,
        copy_comments: bool = True,
        copy_tasks: bool = True,
        copy_artifacts: bool = False,
    ) -> SyncResult:
        def run() -> SyncResult:
            source, target = self._clients(direction)
            source_link, target_link = self._link_fields(direction)
            mapper = self.mapper(direction)
            incident = source.get_incident(source_incident_id, want_findings=True)

            existing = self._counterpart_id(incident, direction)
            if existing is not None:
                return SyncResult(
                    "create", source_incident_id, existing, "skipped", {"reason": "already linked"}
                )

            payload = copy_fields(incident, self.CREATE_FIELDS)
            for name in ("state", "country"):
                if name in payload:
                    payload[name] = mapper.map_value(name, name, payload[name])
            payload["severity_code"] = mapper.map_value(
                "severity_code", "severity_code", incident.get("severity_code"), strict=True
            )
            properties = copy.deepcopy(
                self.fields.customer_create_properties
                if direction == SyncDirection.MSSP_TO_CUSTOMER
                else self.fields.mssp_create_properties
            )
            source_properties = incident.get("properties") or {}
            for name in self.fields.copy_property_fields:
                if source_properties.get(name) not in (None, ""):
                    properties[name] = mapper.map_value(name, name, source_properties[name])
            properties[target_link] = str(source_incident_id)
            self._add_resolution(payload, properties, incident, direction, mapper)
            payload["properties"] = properties

            created = target.post_incident(payload)
            target_id = int(created["id"])
            source.patch_fields(source_incident_id, {f"properties.{source_link}": str(target_id)})

            details = {
                "comments": 0,
                "tasks": 0,
                "task_notes": 0,
                "artifacts": 0,
                "failed_items": 0,
            }
            if copy_comments:
                self._copy_comments(source, target, source_incident_id, target_id, details)
            if copy_tasks:
                self._copy_tasks(source, target, source_incident_id, target_id, details)
            if copy_artifacts:
                self._copy_artifacts(source, target, source_incident_id, target_id, details)
            return SyncResult("create", source_incident_id, target_id, details=details)

        return self._guarded("create", source_incident_id, run)

    def _add_resolution(self, payload, properties, incident, direction, mapper) -> None:
        """A closed source incident is created closed, with the resolution translated."""
        if incident.get("plan_status") != "C":
            return
        resolution_field = self.fields.customer_resolution_field
        if direction == SyncDirection.MSSP_TO_CUSTOMER:
            value = incident.get("resolution_id")
            # resolution_id is required to close; the customer's own close reason is filled when it has a match.
            payload["resolution_id"] = mapper.map_value(
                "resolution_id", "resolution_id", value, strict=True
            )
            try:
                properties[resolution_field] = mapper.map_value(
                    "resolution_id", resolution_field, value, strict=True
                )
            except MappingError as exc:
                self.logger.warning("%s left empty: %s", resolution_field, exc)
        else:
            value = (incident.get("properties") or {}).get(resolution_field)
            payload["resolution_id"] = mapper.map_value(
                resolution_field, "resolution_id", value, strict=True
            )
        payload.setdefault("resolution_summary", "Closed on the linked incident.")

    def _copy_comments(
        self, source, target, source_id: int, target_id: int, details: dict[str, int]
    ) -> None:
        for comment in source.get_comments(source_id):
            if not comment.get("text"):
                continue
            try:
                target.add_comment(target_id, comment["text"])
                details["comments"] += 1
            except Exception as exc:
                details["failed_items"] += 1
                self.logger.warning(
                    "Comment %s of incident %s not copied: %s", comment.get("id"), source_id, exc
                )

    def _copy_tasks(
        self, source, target, source_id: int, target_id: int, details: dict[str, int]
    ) -> None:
        for task in source.get_tasks(source_id):
            payload = copy_fields(task, TASK_FIELDS)
            if "name" not in payload:
                continue
            try:
                created = target.create_task(target_id, payload)
                details["tasks"] += 1
            except Exception as exc:
                details["failed_items"] += 1
                self.logger.warning(
                    "Task %r of incident %s not copied: %s", task.get("name"), source_id, exc
                )
                continue
            for note in task.get("notes") or []:
                if not note.get("text"):
                    continue
                try:
                    target.add_task_note(created["id"], note["text"])
                    details["task_notes"] += 1
                except Exception as exc:
                    details["failed_items"] += 1
                    self.logger.warning("Note of task %r not copied: %s", task.get("name"), exc)

    def _copy_artifacts(
        self, source, target, source_id: int, target_id: int, details: dict[str, int]
    ) -> None:
        for artifact in source.get_artifacts(source_id):
            payload = copy_fields(artifact, ARTIFACT_FIELDS)
            if "type" not in payload or "value" not in payload:
                continue
            try:
                target.add_artifact(target_id, payload)
                details["artifacts"] += 1
            except Exception as exc:
                details["failed_items"] += 1
                self.logger.warning(
                    "Artifact %r of incident %s not copied: %s",
                    artifact.get("value"),
                    source_id,
                    exc,
                )

    # Updates ---------------------------------------------------------------------

    def sync_severity(self, source_incident_id: int, direction: SyncDirection) -> SyncResult:
        def run() -> SyncResult:
            source, target = self._clients(direction)
            incident = source.get_incident(source_incident_id)
            target_id = self._require_counterpart(incident, direction)
            severity = self.mapper(direction).map_value(
                "severity_code", "severity_code", incident.get("severity_code"), strict=True
            )
            target.patch_fields(target_id, {"severity_code": severity})
            return SyncResult(
                "severity", source_incident_id, target_id, details={"severity_code": severity}
            )

        return self._guarded("severity", source_incident_id, run)

    def sync_close(self, source_incident_id: int, direction: SyncDirection) -> SyncResult:
        def run() -> SyncResult:
            source, target = self._clients(direction)
            incident = source.get_incident(source_incident_id)
            target_id = self._require_counterpart(incident, direction)
            if incident.get("plan_status") != "C":
                return SyncResult(
                    "close",
                    source_incident_id,
                    target_id,
                    "skipped",
                    {"reason": "source is not closed"},
                )

            updates: dict[str, Any] = {"plan_status": "C"}
            properties: dict[str, Any] = {}
            self._add_resolution(updates, properties, incident, direction, self.mapper(direction))
            updates["resolution_summary"] = (
                incident.get("resolution_summary") or "Closed on the linked incident."
            )
            updates.update({f"properties.{name}": value for name, value in properties.items()})
            target.patch_fields(target_id, updates)
            return SyncResult("close", source_incident_id, target_id, details=updates)

        return self._guarded("close", source_incident_id, run)

    def sync_reopen(self, source_incident_id: int, direction: SyncDirection) -> SyncResult:
        def run() -> SyncResult:
            source, target = self._clients(direction)
            incident = source.get_incident(source_incident_id)
            target_id = self._require_counterpart(incident, direction)
            if incident.get("plan_status") != "A":
                return SyncResult(
                    "reopen",
                    source_incident_id,
                    target_id,
                    "skipped",
                    {"reason": "source is not active"},
                )
            target.patch_fields(target_id, {"plan_status": "A"})
            return SyncResult("reopen", source_incident_id, target_id)

        return self._guarded("reopen", source_incident_id, run)

    def sync_note(
        self, source_incident_id: int, direction: SyncDirection, *, note_id: int | None = None
    ) -> SyncResult:
        """Copy one note (the given one, or the newest) to the linked incident."""

        def run() -> SyncResult:
            source, target = self._clients(direction)
            incident = source.get_incident(source_incident_id)
            target_id = self._counterpart_id(incident, direction)
            if target_id is None:
                return SyncResult(
                    "note",
                    source_incident_id,
                    None,
                    "skipped",
                    {"reason": "incident is not linked"},
                )
            if note_id is not None:
                note = source.get_comment(source_incident_id, note_id)
            else:
                notes = [
                    item for item in source.get_comments(source_incident_id) if item.get("text")
                ]
                note = max(notes, key=lambda item: item.get("create_date") or 0, default=None)
            if not note or not note.get("text"):
                raise LookupError(f"No note to copy on incident {source_incident_id}")
            target.add_comment(target_id, note["text"])
            return SyncResult(
                "note", source_incident_id, target_id, details={"note_id": note.get("id")}
            )

        return self._guarded("note", source_incident_id, run)

    def sync_report(self, source_incident_id: int, direction: SyncDirection) -> SyncResult:
        """Copy the incident report (description, recommendation, report date).

        On the MSSP side the report is written as notes of two tasks; on the
        customer side it lives in three custom fields.
        """

        def run() -> SyncResult:
            source, target = self._clients(direction)
            incident = source.get_incident(source_incident_id)
            target_id = self._require_counterpart(incident, direction)
            properties = incident.get("properties") or {}
            fields = self.fields
            if direction == SyncDirection.MSSP_TO_CUSTOMER:
                tasks = source.get_tasks(source_incident_id)
                if not tasks:
                    raise LookupError(
                        f"Incident {source_incident_id} has no tasks to read the report from"
                    )
                description = self._first_task_note(tasks, fields.mssp_description_task_name)
                recommendation = self._first_task_note(tasks, fields.mssp_recommendation_task_name)
            else:
                description = properties.get(fields.report_description_field)
                recommendation = properties.get(fields.report_recommendation_field)
            updates = {
                f"properties.{fields.report_description_field}": description or "",
                f"properties.{fields.report_recommendation_field}": recommendation or "",
                f"properties.{fields.report_date_field}": properties.get(fields.report_date_field),
            }
            target.patch_fields(target_id, updates)
            return SyncResult(
                "report", source_incident_id, target_id, details={"fields": sorted(updates)}
            )

        return self._guarded("report", source_incident_id, run)

    def sync_analyst_assignment(self, source_incident_id: int) -> SyncResult:
        """Tell the customer that an MSSP analyst picked up the incident."""
        direction = SyncDirection.MSSP_TO_CUSTOMER

        def run() -> SyncResult:
            incident = self.mssp.get_incident(source_incident_id)
            analyst = (incident.get("properties") or {}).get(self.fields.analyst_field)
            if not analyst:
                return SyncResult(
                    "analyst",
                    source_incident_id,
                    None,
                    "skipped",
                    {"reason": "no analyst assigned"},
                )
            target_id = self._require_counterpart(incident, direction)
            self.customer.patch_fields(
                target_id, {f"properties.{self.fields.analyst_assigned_field}": True}
            )
            return SyncResult(
                "analyst", source_incident_id, target_id, details={"analyst_assigned": True}
            )

        return self._guarded("analyst", source_incident_id, run)

    @staticmethod
    def _first_task_note(tasks: list[dict[str, Any]], task_name: str) -> str:
        for task in tasks:
            if task.get("name") == task_name and task.get("notes"):
                return task["notes"][0].get("text", "")
        return ""
