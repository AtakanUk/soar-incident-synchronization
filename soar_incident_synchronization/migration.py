"""Bulk copy of incidents (with notes, tasks and artifacts) to another SOAR instance.

Used when moving to a new SOAR installation. The run is resumable: progress
is kept in a state file, every created incident carries the source incident
ID in a tracking field, and an incident that already exists on the target is
never created twice.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path
from typing import Any

from .client import SoarApiClient
from .config import SyncFieldConfig
from .mappings import FieldValueMapper
from .sync import ARTIFACT_FIELDS, TASK_FIELDS, copy_fields

LOGGER = logging.getLogger(__name__)


class MigrationState:
    """Resumable progress, written atomically after every incident."""

    def __init__(self, state_file: Path):
        self.state_file = state_file
        self.data: dict[str, Any] = {
            "source_to_target": {},
            "failed": {},
            "last_offset": 0,
            "stats": {
                "created": 0,
                "comments": 0,
                "artifacts": 0,
                "tasks": 0,
                "task_notes": 0,
                "failed_items": 0,
            },
        }
        if state_file.exists():
            loaded = json.loads(state_file.read_text(encoding="utf-8"))
            for key, value in loaded.items():
                if isinstance(value, dict) and isinstance(self.data.get(key), dict):
                    self.data[key].update(value)
                else:
                    self.data[key] = value

    def target_of(self, source_id: int) -> int | None:
        value = self.data["source_to_target"].get(str(source_id))
        return int(value) if value is not None else None

    def mark_completed(self, source_id: int, target_id: int) -> None:
        self.data["source_to_target"][str(source_id)] = target_id
        self.data["failed"].pop(str(source_id), None)

    def mark_failed(self, source_id: int, reason: str) -> None:
        self.data["failed"][str(source_id)] = reason

    def save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_name(self.state_file.name + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.state_file)


class IncidentMigrator:
    """Copy incidents from ``source`` to ``target``."""

    TOP_LEVEL_FIELDS = (
        "name",
        "description",
        "discovered_date",
        "severity_code",
        "incident_type_ids",
        "resolution_id",
        "resolution_summary",
        "plan_status",
        "confirmed",
    )
    ALWAYS_KEEP = frozenset({"name", "description", "resolution_id", "resolution_summary"})
    FREE_TEXT = frozenset({"name", "description", "resolution_summary", "discovered_date"})

    def __init__(
        self,
        source: SoarApiClient,
        target: SoarApiClient,
        state: MigrationState,
        mapper: FieldValueMapper,
        fields: SyncFieldConfig,
        *,
        dry_run: bool = False,
        payload_dir: Path | None = None,
        max_create: int | None = None,
    ) -> None:
        self.source = source
        self.target = target
        self.state = state
        self.mapper = mapper
        self.fields = fields
        self.dry_run = dry_run
        self.payload_dir = payload_dir
        self.max_create = max_create
        self.created_this_run = 0
        # Links to the other system would point at IDs that do not exist on the target.
        self.dropped_properties = {fields.mssp_reference_field, fields.customer_reference_field}

    # Payload -------------------------------------------------------------------

    def build_incident_payload(self, incident: dict[str, Any]) -> dict[str, Any]:
        candidates = self.TOP_LEVEL_FIELDS
        if self.mapper.target_top_level:
            candidates = tuple(
                name
                for name in candidates
                if name in self.mapper.target_top_level or name in self.ALWAYS_KEEP
            )
        payload = {
            name: copy.deepcopy(incident[name])
            for name in candidates
            if incident.get(name) is not None
        }
        for name in list(payload):
            if name not in self.FREE_TEXT:
                payload[name] = self.mapper.map_value(name, name, payload[name])

        properties: dict[str, Any] = {}
        source_properties = incident.get("properties") or {}
        if source_properties and not self.mapper.target_properties:
            LOGGER.warning(
                "Target custom fields unknown; custom fields of incident %s are not copied",
                incident.get("id"),
            )
        for name, value in source_properties.items():
            if value in (None, "", []) or name in self.dropped_properties:
                continue
            if name in self.mapper.target_properties:
                properties[name] = self.mapper.map_value(name, name, value)
        if self.fields.migration_tracking_field in self.mapper.target_properties:
            properties[self.fields.migration_tracking_field] = incident.get("id")
        if properties:
            payload["properties"] = properties

        payload.setdefault("name", f"Migrated incident {incident.get('id')}")
        # SOAR refuses a closed incident without a resolution.
        if payload.get("plan_status") == "C" and not payload.get("resolution_id"):
            if self.fields.migration_default_resolution:
                default = self.fields.migration_default_resolution
                payload["resolution_id"] = int(default) if default.isdigit() else default
                payload.setdefault(
                    "resolution_summary", self.fields.migration_default_resolution_summary
                )
            else:
                LOGGER.warning(
                    "Closed incident %s has no resolution; set a default resolution",
                    incident.get("id"),
                )
        return payload

    # Target side -----------------------------------------------------------------

    def find_existing_target_incident(self, source_id: int) -> int | None:
        known = self.state.target_of(source_id)
        if known:
            return known
        if self.fields.migration_tracking_field not in self.mapper.target_properties:
            return None
        try:
            return self.target.find_incident_by_property(
                self.fields.migration_tracking_field, source_id
            )
        except Exception as exc:
            LOGGER.warning("Duplicate check failed for incident %s: %s", source_id, exc)
            return None

    def create_target_incident(self, payload: dict[str, Any], source_id: int) -> int:
        try:
            created = self.target.post_incident(payload)
        except Exception as exc:
            # A field rejected by the target schema should not stop the incident itself.
            LOGGER.warning(
                "Full payload rejected for incident %s, retrying with core fields: %s",
                source_id,
                exc,
            )
            minimal = {
                name: payload[name]
                for name in (
                    "name",
                    "description",
                    "plan_status",
                    "resolution_id",
                    "resolution_summary",
                )
                if name in payload
            }
            tracking = (payload.get("properties") or {}).get(self.fields.migration_tracking_field)
            if tracking is not None:
                minimal["properties"] = {self.fields.migration_tracking_field: tracking}
            created = self.target.post_incident(minimal)
        if not isinstance(created, dict) or not created.get("id"):
            raise RuntimeError(
                f"Target did not return an incident ID for source incident {source_id}"
            )
        return int(created["id"])

    def copy_children(self, source_id: int, target_id: int) -> dict[str, int]:
        counts = {"comments": 0, "artifacts": 0, "tasks": 0, "task_notes": 0, "failed_items": 0}

        def attempt(kind: str, label: Any, action) -> Any:
            try:
                result = action()
                counts[kind] += 1
                return result
            except Exception as exc:
                counts["failed_items"] += 1
                LOGGER.warning("%s %r of incident %s not copied: %s", kind, label, source_id, exc)
                return None

        for comment in self.source.get_comments(source_id):
            if comment.get("text"):
                attempt(
                    "comments",
                    comment.get("id"),
                    lambda c=comment: self.target.add_comment(target_id, c["text"]),
                )
        for artifact in self.source.get_artifacts(source_id):
            payload = copy_fields(artifact, ARTIFACT_FIELDS)
            if "type" in payload and "value" in payload:
                attempt(
                    "artifacts",
                    payload["value"],
                    lambda p=payload: self.target.add_artifact(target_id, p),
                )
        for task in self.source.get_tasks(source_id):
            payload = copy_fields(task, TASK_FIELDS)
            if "name" not in payload:
                continue
            created = attempt(
                "tasks", payload["name"], lambda p=payload: self.target.create_task(target_id, p)
            )
            if not created:
                continue
            for note in task.get("notes") or []:
                if note.get("text"):
                    attempt(
                        "task_notes",
                        payload["name"],
                        lambda n=note, t=created: self.target.add_task_note(t["id"], n["text"]),
                    )
        return counts

    # Driver ------------------------------------------------------------------------

    def _dump_payload(self, source_id: int, payload: dict[str, Any]) -> None:
        if not self.payload_dir:
            return
        self.payload_dir.mkdir(parents=True, exist_ok=True)
        dump = {
            "source_incident_id": source_id,
            "incident": payload,
            "comments": self.source.get_comments(source_id),
            "artifacts": self.source.get_artifacts(source_id),
            "tasks": self.source.get_tasks(source_id),
        }
        path = self.payload_dir / f"incident_{source_id}.json"
        path.write_text(
            json.dumps(dump, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )

    def migrate_one_incident(self, source_id: int) -> tuple[str, int | None]:
        """Returns (outcome, target id): outcome is created, existing, planned or limit."""
        existing = self.find_existing_target_incident(source_id)
        if existing:
            if not self.dry_run:
                self.state.mark_completed(source_id, existing)
            return "existing", existing

        if (
            not self.dry_run
            and self.max_create is not None
            and self.created_this_run >= self.max_create
        ):
            return "limit", None
        incident = self.source.get_incident(source_id, want_findings=True)
        payload = self.build_incident_payload(incident)
        if self.dry_run:
            self._dump_payload(source_id, payload)
            return "planned", None

        target_id = self.create_target_incident(payload, source_id)
        self.created_this_run += 1
        counts = self.copy_children(source_id, target_id)
        stats = self.state.data["stats"]
        stats["created"] += 1
        for key, value in counts.items():
            stats[key] += value
        self.state.mark_completed(source_id, target_id)
        return "created", target_id

    def migrate_range(
        self, *, start: int = 0, limit: int | None = None, batch_size: int = 100
    ) -> dict[str, Any]:
        outcomes = {"created": 0, "existing": 0, "planned": 0, "limit": 0, "failed": 0}
        offset = start
        processed = 0
        while limit is None or processed < limit:
            page_size = batch_size if limit is None else min(batch_size, limit - processed)
            incidents = self.source.query_incidents(start=offset, length=page_size)
            if not incidents:
                break
            for incident in incidents:
                source_id = int(incident["id"])
                try:
                    outcome, target_id = self.migrate_one_incident(source_id)
                    outcomes[outcome] += 1
                    LOGGER.info("Incident %s: %s %s", source_id, outcome, target_id or "")
                except Exception as exc:
                    outcomes["failed"] += 1
                    LOGGER.exception("Incident %s failed: %s", source_id, exc)
                    if not self.dry_run:
                        self.state.mark_failed(source_id, str(exc))
                processed += 1
                if not self.dry_run:
                    self.state.data["last_offset"] = offset
                    self.state.save()
            if outcomes["limit"]:
                LOGGER.warning("Create limit of %s reached; run again to continue", self.max_create)
                break
            offset += len(incidents)
        return {"outcomes": outcomes, "next_offset": offset, "stats": self.state.data["stats"]}


def load_or_discover_mapper(
    path: Path | None, source: SoarApiClient, target: SoarApiClient
) -> FieldValueMapper:
    """Reuse a reviewed mapping file when present, otherwise discover one (and save it for review)."""
    if path and path.exists():
        return FieldValueMapper.load(path)
    mapper = FieldValueMapper.discover(source, target)
    if path:
        mapper.save(path)
    return mapper
