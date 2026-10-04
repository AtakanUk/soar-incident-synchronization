"""Field metadata and select-value mapping between two SOAR instances.

Select fields (severity, resolution, custom drop-downs) store numeric IDs that
differ per instance. Values are translated by matching option labels, which
can be overridden per direction in configuration.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class MappingError(ValueError):
    """A select value has no counterpart on the other instance."""


def _option_list(definition: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("values", "choice_values", "select_values"):
        if isinstance(definition.get(key), list):
            return [item for item in definition[key] if isinstance(item, dict)]
    return []


def _field_name(definition: dict[str, Any]) -> tuple[str | None, bool]:
    """Return (name, is_custom). Custom fields carry prefix 'properties'."""
    for key in ("name", "api_name", "field_name"):
        value = definition.get(key)
        if isinstance(value, str) and value.strip():
            name = value.strip()
            if name.startswith("properties."):
                return name.split(".", 1)[1], True
            return name, definition.get("prefix") == "properties"
    return None, False


class FieldCatalog:
    """Incident field names and select options of one SOAR instance."""

    def __init__(self, definitions: Iterable[dict[str, Any]] = ()):
        self.top_level: set[str] = set()
        self.properties: set[str] = set()
        self._labels: dict[str, dict[str, str]] = {}
        for definition in definitions:
            name, is_custom = _field_name(definition)
            if not name:
                continue
            (self.properties if is_custom else self.top_level).add(name)
            labels = {}
            for option in _option_list(definition):
                label = option.get("label") or option.get("name") or option.get("text")
                value = option.get("value", option.get("id"))
                if label is not None and value is not None:
                    labels[str(value)] = str(label).strip()
            if labels:
                self._labels[name] = labels

    @classmethod
    def from_client(cls, client: Any) -> FieldCatalog:
        return cls(client.get_field_definitions())

    def __bool__(self) -> bool:
        return bool(self.top_level or self.properties)

    def options(self, field_name: str) -> dict[str, str]:
        """{str(value): label} of a select field."""
        return dict(self._labels.get(field_name, {}))

    def label_for(self, field_name: str, value: Any) -> str | None:
        return self._labels.get(field_name, {}).get(str(value))

    def value_for(self, field_name: str, label: str) -> Any | None:
        wanted = label.strip().casefold()
        for value, option_label in self._labels.get(field_name, {}).items():
            if option_label.casefold() == wanted:
                return int(value) if value.isdigit() else value
        return None


@dataclass
class FieldValueMapper:
    """Maps select values from a source instance to a target instance."""

    value_maps: dict[str, dict[str, Any]] = field(default_factory=dict)
    target_top_level: set[str] = field(default_factory=set)
    target_properties: set[str] = field(default_factory=set)

    @classmethod
    def build(
        cls,
        source: FieldCatalog,
        target: FieldCatalog,
        extra_pairs: Iterable[tuple[str, str]] = (),
        overrides: dict[str, dict[str, Any]] | None = None,
    ) -> FieldValueMapper:
        source_fields = source.top_level | source.properties
        target_fields = target.top_level | target.properties
        pairs = [(name, name) for name in sorted(source_fields & target_fields)] + list(extra_pairs)

        value_maps: dict[str, dict[str, Any]] = {}
        for source_field, target_field in pairs:
            target_by_label = {
                label.casefold(): value for value, label in target.options(target_field).items()
            }
            mapping = {}
            for value, label in source.options(source_field).items():
                if label.casefold() in target_by_label:
                    target_value = target_by_label[label.casefold()]
                    mapping[value] = int(target_value) if target_value.isdigit() else target_value
            if mapping:
                value_maps[f"{source_field}->{target_field}"] = mapping

        for key, mapping in (overrides or {}).items():
            value_maps.setdefault(key, {}).update({str(k): v for k, v in mapping.items()})
        return cls(value_maps, set(target.top_level), set(target.properties))

    @classmethod
    def discover(
        cls,
        source_client: Any,
        target_client: Any,
        extra_pairs: Iterable[tuple[str, str]] = (),
        overrides: dict[str, dict[str, Any]] | None = None,
    ) -> FieldValueMapper:
        return cls.build(
            FieldCatalog.from_client(source_client),
            FieldCatalog.from_client(target_client),
            extra_pairs,
            overrides,
        )

    def map_value(
        self, source_field: str, target_field: str, value: Any, *, strict: bool = False
    ) -> Any:
        """Translate a select value. Unknown values pass through unless ``strict`` is set."""
        mapping = self.value_maps.get(f"{source_field}->{target_field}", {})
        if isinstance(value, list):
            return [
                self.map_value(source_field, target_field, item, strict=strict) for item in value
            ]
        if value is None:
            if strict:
                raise MappingError(f"{source_field} is empty, nothing to map to {target_field}")
            return None
        if str(value) in mapping:
            return mapping[str(value)]
        if strict:
            raise MappingError(
                f"No {target_field} value for {source_field}={value!r}; add it to the override maps"
            )
        return value

    def to_dict(self) -> dict[str, Any]:
        return {
            "value_maps": self.value_maps,
            "target_top_level": sorted(self.target_top_level),
            "target_properties": sorted(self.target_properties),
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> FieldValueMapper:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            data.get("value_maps", {}),
            set(data.get("target_top_level", [])),
            set(data.get("target_properties", [])),
        )


def determine_customer_resolution_label(
    resolution_label: str | None,
    incident_name: str | None = None,
    test_marker: str = "Incident generated",
) -> str | None:
    """Standard resolution label for a customer close reason (None when no rule applies)."""
    if not resolution_label:
        return None
    if "Not an Issue" in resolution_label or "Non-Issue" in resolution_label:
        return "Non-Issue"
    if "Known Activity" in resolution_label:
        return "Known Offense (Test)"
    if "Action Taken" in resolution_label:
        # Test incidents are closed with "Action Taken" as well but are not real positives.
        if incident_name and test_marker and test_marker in incident_name:
            return "Known Offense (Test)"
        return "True-Positive Resolved"
    if "Related Offense(s)" in resolution_label:
        return "Related Offense(s)"
    if "False-Positive" in resolution_label or "False Positive" in resolution_label:
        return "False-Positive"
    return None
