"""Fill the standard resolution of closed customer incidents from the customer's own close reason.

Customer analysts close incidents with a custom select field. Reports and the
MSSP side use the standard ``resolution_id``, so this job derives it (see
``determine_customer_resolution_label``) for incidents closed before the
synchronization was in place.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from .client import SoarApiClient
from .mappings import FieldCatalog, determine_customer_resolution_label

LOGGER = logging.getLogger(__name__)


class CustomerResolutionBackfill:
    def __init__(
        self,
        client: SoarApiClient,
        state_file: Path,
        *,
        resolution_field: str = "customer_resolution",
        test_marker: str = "Incident generated",
        catalog: FieldCatalog | None = None,
    ) -> None:
        self.client = client
        self.state_file = state_file
        self.resolution_field = resolution_field
        self.test_marker = test_marker
        self.catalog = catalog if catalog is not None else FieldCatalog.from_client(client)
        self.state = self._load_state()

    def _load_state(self) -> dict[str, Any]:
        if not self.state_file.exists():
            return {"completed_ids": [], "patched": 0, "skipped": 0}
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def _save_state(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_name(self.state_file.name + ".tmp")
        tmp.write_text(json.dumps(self.state, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.state_file)

    def target_resolution(self, incident: dict[str, Any]) -> Any | None:
        """resolution_id value the incident should have, or None when no rule applies."""
        raw = (incident.get("properties") or {}).get(self.resolution_field)
        if raw in (None, ""):
            return None
        # Select fields come back as option IDs; the rules work on labels.
        label = self.catalog.label_for(self.resolution_field, raw) or str(raw)
        wanted = determine_customer_resolution_label(label, incident.get("name"), self.test_marker)
        if wanted is None:
            LOGGER.info("Incident %s: close reason %r matches no rule", incident.get("id"), label)
            return None
        value = self.catalog.value_for("resolution_id", wanted)
        if value is None:
            raise LookupError(f"Resolution {wanted!r} does not exist on this SOAR instance")
        return value

    def process_incident(self, incident: dict[str, Any], dry_run: bool = False) -> bool:
        incident_id = int(incident["id"])
        new_value = self.target_resolution(incident)
        current = incident.get("resolution_id")
        if new_value is None or str(current) == str(new_value):
            return False
        old_label = self.catalog.label_for("resolution_id", current) or current
        new_label = self.catalog.label_for("resolution_id", new_value) or new_value
        LOGGER.info(
            "Incident %s: resolution %s -> %s%s",
            incident_id,
            old_label,
            new_label,
            " (dry run)" if dry_run else "",
        )
        if not dry_run:
            self.client.patch_fields(incident_id, {"resolution_id": new_value})
            self.client.add_comment(
                incident_id,
                f"Resolution set to {new_label} from the close reason (was: {old_label}).",
            )
        return True

    def run(
        self,
        *,
        start: int = 0,
        limit: int | None = None,
        batch_size: int = 100,
        dry_run: bool = False,
    ) -> dict[str, int]:
        completed = set(self.state.get("completed_ids", []))
        counts = {"patched": 0, "skipped": 0, "errors": 0}
        filters: list[dict[str, Any]] = [
            {"conditions": [{"field_name": "plan_status", "method": "equals", "value": "C"}]}
        ]
        offset = start
        processed = 0
        while limit is None or processed < limit:
            incidents = self.client.query_incidents(
                start=offset, length=batch_size, filters=filters
            )
            if not incidents:
                break
            for incident in incidents:
                if limit is not None and processed >= limit:
                    break
                incident_id = int(incident["id"])
                if incident_id in completed:
                    continue
                processed += 1
                try:
                    changed = self.process_incident(
                        self.client.get_incident(incident_id), dry_run=dry_run
                    )
                except Exception as exc:
                    # Not marked completed, so the next run retries it.
                    counts["errors"] += 1
                    LOGGER.error("Incident %s: %s", incident_id, exc)
                    continue
                counts["patched" if changed else "skipped"] += 1
                if not dry_run:
                    completed.add(incident_id)
                    self.state["completed_ids"] = sorted(completed)
                    self.state["patched"] = self.state.get("patched", 0) + int(changed)
                    self.state["skipped"] = self.state.get("skipped", 0) + int(not changed)
                    self._save_state()
            offset += len(incidents)
        return counts
