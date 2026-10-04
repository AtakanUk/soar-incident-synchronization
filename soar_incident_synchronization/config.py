"""Configuration models loaded from environment variables."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


def _to_bool(value: str | bool | None, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _cafile_option(value: str | bool | None) -> bool | str:
    """``false`` disables TLS verification, ``true`` uses the system CAs, anything else is a CA bundle path."""
    if isinstance(value, bool):
        return value
    text = (value or "").strip()
    if text.lower() in {"", "0", "false", "no", "off"}:
        return False
    if text.lower() in {"1", "true", "yes", "on"}:
        return True
    return text


def _load_json_env(name: str, default: Any) -> Any:
    raw_value = os.getenv(name)
    if not raw_value:
        return default
    try:
        return json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} is not valid JSON: {exc}") from exc


def _split_csv(raw_value: str, default: list[str] | None = None) -> list[str]:
    if not raw_value:
        return list(default or [])
    return [item.strip() for item in raw_value.split(",") if item.strip()]


class SyncDirection(StrEnum):
    MSSP_TO_CUSTOMER = "mssp-to-customer"
    CUSTOMER_TO_MSSP = "customer-to-mssp"


@dataclass(frozen=True)
class SoarInstanceConfig:
    """Connection settings for one IBM SOAR instance."""

    name: str
    host: str
    org: str
    api_key_id: str
    api_key_secret: str
    port: int = 443
    cafile: bool | str = False

    @classmethod
    def from_env(cls, prefix: str, display_name: str) -> SoarInstanceConfig:
        return cls(
            name=display_name,
            host=os.getenv(f"{prefix}_SOAR_HOST", ""),
            org=os.getenv(f"{prefix}_SOAR_ORG", ""),
            api_key_id=os.getenv(f"{prefix}_SOAR_API_KEY_ID", ""),
            api_key_secret=os.getenv(f"{prefix}_SOAR_API_KEY_SECRET", ""),
            port=int(os.getenv(f"{prefix}_SOAR_PORT", "443")),
            cafile=_cafile_option(os.getenv(f"{prefix}_SOAR_CAFILE", "true")),
        )

    def to_auth(self) -> dict[str, Any]:
        missing = [
            name
            for name in ("host", "org", "api_key_id", "api_key_secret")
            if not getattr(self, name)
        ]
        if missing:
            raise ValueError(f"{self.name} SOAR settings are incomplete: {', '.join(missing)}")
        return {
            "host": self.host,
            "port": self.port,
            "org": self.org,
            "api_key_id": self.api_key_id,
            "api_key_secret": self.api_key_secret,
            "cafile": _cafile_option(self.cafile),
        }


@dataclass(frozen=True)
class NotificationConfig:
    """Optional OAuth2 SMTP notification settings."""

    enabled: bool = False
    email_to: str = ""
    email_from: str = ""
    smtp_user: str = ""
    smtp_server: str = "smtp.office365.com"
    smtp_port: int = 587
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    oauth_tenant_id: str = ""
    oauth_refresh_token: str = ""
    oauth_scope: str = "offline_access https://outlook.office365.com/SMTP.Send"

    @classmethod
    def from_env(cls) -> NotificationConfig:
        return cls(
            enabled=_to_bool(os.getenv("SOAR_SYNC_ERROR_MAIL_ENABLED"), False),
            email_to=os.getenv("SOAR_SYNC_ERROR_MAIL_TO", ""),
            email_from=os.getenv("SOAR_SYNC_ERROR_MAIL_FROM", ""),
            smtp_user=os.getenv("SOAR_SYNC_ERROR_MAIL_SMTP_USER", ""),
            smtp_server=os.getenv("SOAR_SYNC_ERROR_MAIL_SMTP_SERVER", "smtp.office365.com"),
            smtp_port=int(os.getenv("SOAR_SYNC_ERROR_MAIL_SMTP_PORT", "587")),
            oauth_client_id=os.getenv("SOAR_SYNC_ERROR_MAIL_OAUTH_CLIENT_ID", ""),
            oauth_client_secret=os.getenv("SOAR_SYNC_ERROR_MAIL_OAUTH_CLIENT_SECRET", ""),
            oauth_tenant_id=os.getenv("SOAR_SYNC_ERROR_MAIL_OAUTH_TENANT_ID", ""),
            oauth_refresh_token=os.getenv("SOAR_SYNC_ERROR_MAIL_OAUTH_REFRESH_TOKEN", ""),
            oauth_scope=os.getenv(
                "SOAR_SYNC_ERROR_MAIL_OAUTH_SCOPE",
                "offline_access https://outlook.office365.com/SMTP.Send",
            ),
        )


@dataclass(frozen=True)
class SyncFieldConfig:
    """Custom field names and per-direction payload defaults."""

    # On customer incidents: the MSSP incident ID. On MSSP incidents: the customer incident ID.
    mssp_reference_field: str = "mssp_incident_id"
    customer_reference_field: str = "customer_incident_id"
    # Customer-side select field holding the customer's own close reason.
    customer_resolution_field: str = "customer_resolution"
    report_description_field: str = "describe_the_incident"
    report_recommendation_field: str = "provide_recommendation"
    report_date_field: str = "date_reported"
    analyst_field: str = "analyst"
    analyst_assigned_field: str = "analyst_assigned"
    copy_property_fields: list[str] = field(
        default_factory=lambda: ["external_alert_id", "date_reported"]
    )
    mssp_description_task_name: str = "Describe the offense."
    mssp_recommendation_task_name: str = "Provide suggestions."
    customer_create_properties: dict[str, Any] = field(default_factory=dict)
    mssp_create_properties: dict[str, Any] = field(default_factory=dict)
    # {"mssp-to-customer": {"severity_code->severity_code": {"<source value>": <target value>}}, ...}
    override_maps: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    migration_tracking_field: str = "source_incident_id"
    migration_default_resolution: str = ""
    migration_default_resolution_summary: str = "Migrated from the previous SOAR instance."
    test_incident_marker: str = "Incident generated"

    @classmethod
    def from_env(cls) -> SyncFieldConfig:
        defaults = cls()
        env = os.getenv
        return cls(
            mssp_reference_field=env(
                "SOAR_SYNC_MSSP_REFERENCE_FIELD", defaults.mssp_reference_field
            ),
            customer_reference_field=env(
                "SOAR_SYNC_CUSTOMER_REFERENCE_FIELD", defaults.customer_reference_field
            ),
            customer_resolution_field=env(
                "SOAR_SYNC_CUSTOMER_RESOLUTION_FIELD", defaults.customer_resolution_field
            ),
            report_description_field=env(
                "SOAR_SYNC_REPORT_DESCRIPTION_FIELD", defaults.report_description_field
            ),
            report_recommendation_field=env(
                "SOAR_SYNC_REPORT_RECOMMENDATION_FIELD", defaults.report_recommendation_field
            ),
            report_date_field=env("SOAR_SYNC_REPORT_DATE_FIELD", defaults.report_date_field),
            analyst_field=env("SOAR_SYNC_ANALYST_FIELD", defaults.analyst_field),
            analyst_assigned_field=env(
                "SOAR_SYNC_ANALYST_ASSIGNED_FIELD", defaults.analyst_assigned_field
            ),
            copy_property_fields=_split_csv(
                env("SOAR_SYNC_COPY_PROPERTY_FIELDS", ""), default=defaults.copy_property_fields
            ),
            mssp_description_task_name=env(
                "SOAR_SYNC_MSSP_DESCRIPTION_TASK", defaults.mssp_description_task_name
            ),
            mssp_recommendation_task_name=env(
                "SOAR_SYNC_MSSP_RECOMMENDATION_TASK", defaults.mssp_recommendation_task_name
            ),
            customer_create_properties=_load_json_env(
                "SOAR_SYNC_CUSTOMER_CREATE_PROPERTIES_JSON", {}
            ),
            mssp_create_properties=_load_json_env("SOAR_SYNC_MSSP_CREATE_PROPERTIES_JSON", {}),
            override_maps=_load_json_env("SOAR_SYNC_OVERRIDE_MAPS_JSON", {}),
            migration_tracking_field=env(
                "SOAR_SYNC_MIGRATION_TRACKING_FIELD", defaults.migration_tracking_field
            ),
            migration_default_resolution=env(
                "SOAR_SYNC_MIGRATION_DEFAULT_RESOLUTION", defaults.migration_default_resolution
            ),
            migration_default_resolution_summary=env(
                "SOAR_SYNC_MIGRATION_DEFAULT_RESOLUTION_SUMMARY",
                defaults.migration_default_resolution_summary,
            ),
            test_incident_marker=env(
                "SOAR_SYNC_TEST_INCIDENT_MARKER", defaults.test_incident_marker
            ),
        )


@dataclass(frozen=True)
class LoggingConfig:
    """Logging configuration."""

    directory: Path = Path("logs")
    level: str = "INFO"

    @classmethod
    def from_env(cls) -> LoggingConfig:
        return cls(
            directory=Path(os.getenv("SOAR_SYNC_LOG_DIR", "logs")),
            level=os.getenv("SOAR_SYNC_LOG_LEVEL", "INFO").upper(),
        )


@dataclass(frozen=True)
class AppConfig:
    """Application-wide configuration."""

    mssp: SoarInstanceConfig
    customer: SoarInstanceConfig
    fields: SyncFieldConfig = field(default_factory=SyncFieldConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    @classmethod
    def from_env(cls) -> AppConfig:
        return cls(
            mssp=SoarInstanceConfig.from_env("MSSP", "mssp"),
            customer=SoarInstanceConfig.from_env("CUSTOMER", "customer"),
            fields=SyncFieldConfig.from_env(),
            notifications=NotificationConfig.from_env(),
            logging=LoggingConfig.from_env(),
        )
