import json
from types import SimpleNamespace

import pytest

from soar_incident_synchronization import sync as sync_module
from soar_incident_synchronization.cli import build_parser
from soar_incident_synchronization.client import SoarApiClient
from soar_incident_synchronization.config import (
    AppConfig,
    LoggingConfig,
    SoarInstanceConfig,
    SyncDirection,
    SyncFieldConfig,
    _cafile_option,
)
from soar_incident_synchronization.mappings import FieldCatalog, MappingError
from soar_incident_synchronization.migration import (
    IncidentMigrator,
    MigrationState,
    load_or_discover_mapper,
)
from soar_incident_synchronization.resolution_backfill import CustomerResolutionBackfill
from soar_incident_synchronization.sync import IncidentSynchronizer
from tests.fakes import CUSTOMER_FIELDS, MSSP_FIELDS, FakeResilient

TO_CUSTOMER = SyncDirection.MSSP_TO_CUSTOMER
TO_MSSP = SyncDirection.CUSTOMER_TO_MSSP


def make_env(tmp_path, monkeypatch, **field_options):
    mails = []
    monkeypatch.setattr(
        sync_module, "send_error_mail", lambda *args, **kwargs: mails.append(kwargs)
    )
    fields = SyncFieldConfig(
        customer_create_properties={"record_type": "security"}, **field_options
    )
    config = AppConfig(
        mssp=SoarInstanceConfig("mssp", "", "", "", ""),
        customer=SoarInstanceConfig("customer", "", "", "", ""),
        fields=fields,
        logging=LoggingConfig(directory=tmp_path / "logs"),
    )
    mssp_raw, customer_raw = FakeResilient(MSSP_FIELDS, 1000), FakeResilient(CUSTOMER_FIELDS, 5000)
    mssp, customer = (
        SoarApiClient(config.mssp, mssp_raw),
        SoarApiClient(config.customer, customer_raw),
    )
    return SimpleNamespace(
        sync=IncidentSynchronizer(config, mssp, customer),
        config=config,
        mssp=mssp,
        customer=customer,
        mssp_raw=mssp_raw,
        customer_raw=customer_raw,
        mails=mails,
    )


def link(env, mssp_id, customer_id, mssp_values=None, customer_values=None):
    mssp_values, customer_values = dict(mssp_values or {}), dict(customer_values or {})
    mssp_values.setdefault("properties", {})["customer_incident_id"] = str(customer_id)
    customer_values.setdefault("properties", {})["mssp_incident_id"] = str(mssp_id)
    env.mssp_raw.add_incident(mssp_id, **mssp_values)
    env.customer_raw.add_incident(customer_id, **customer_values)


@pytest.fixture
def env(tmp_path, monkeypatch):
    return make_env(tmp_path, monkeypatch)


def test_create_counterpart_copies_incident_and_links_both_sides(env):
    env.mssp_raw.add_incident(
        1001,
        name="Brute force on VPN",
        description="<p>many failed logins</p>",
        severity_code=19357,
        properties={"external_alert_id": "A-77", "analyst": "jdoe"},
    )
    env.mssp_raw.comments[1001] = [{"id": 1, "text": "<p>first look</p>", "create_date": 1}]
    env.mssp_raw.tasks[1001] = [
        {
            "id": 11,
            "name": "Describe the offense.",
            "required": True,
            "notes": [{"text": "summary"}],
        }
    ]

    result = env.sync.create_counterpart(1001, TO_CUSTOMER)

    created = env.customer_raw.incidents[result.target_incident_id]
    assert created["severity_code"] == 6  # High -> High, matched by label
    assert created["properties"] == {
        "record_type": "security",
        "external_alert_id": "A-77",
        "mssp_incident_id": "1001",
    }
    assert env.mssp_raw.incidents[1001]["properties"]["customer_incident_id"] == str(
        result.target_incident_id
    )
    assert env.customer_raw.comments[result.target_incident_id][0]["text"] == "<p>first look</p>"
    assert result.details == {
        "comments": 1,
        "tasks": 1,
        "task_notes": 1,
        "artifacts": 0,
        "failed_items": 0,
    }
    assert env.sync.create_counterpart(1001, TO_CUSTOMER).status == "skipped"  # never twice


def test_create_closed_incident_carries_resolution(env):
    env.mssp_raw.add_incident(
        1002, name="Closed", severity_code=19355, plan_status="C", resolution_id=19432
    )
    result = env.sync.create_counterpart(1002, TO_CUSTOMER, copy_comments=False, copy_tasks=False)
    created = env.customer_raw.incidents[result.target_incident_id]
    assert (created["plan_status"], created["resolution_id"], created["severity_code"]) == (
        "C",
        1121,
        4,
    )


def test_close_mssp_to_customer_sets_both_resolution_fields(env):
    link(
        env,
        1003,
        5003,
        {"plan_status": "C", "resolution_id": 19545, "resolution_summary": "Host isolated"},
    )
    env.sync.sync_close(1003, TO_CUSTOMER)
    target = env.customer_raw.incidents[5003]
    assert target["plan_status"] == "C" and target["resolution_id"] == 782
    assert target["properties"]["customer_resolution"] == 890
    assert target["resolution_summary"] == "Host isolated"
    # custom fields are patched under their plain name (the fake rejects "properties.x")
    assert env.customer_raw.patches[-1][1]["customer_resolution"] == 890


def test_close_customer_to_mssp_and_unmapped_resolution(tmp_path, monkeypatch, env):
    link(
        env,
        1004,
        5004,
        customer_values={"plan_status": "C", "properties": {"customer_resolution": 890}},
    )
    env.sync.sync_close(5004, TO_MSSP)
    assert env.mssp_raw.incidents[1004]["resolution_id"] == 19545

    link(
        env,
        1005,
        5005,
        customer_values={"plan_status": "C", "properties": {"customer_resolution": 897}},
    )
    with pytest.raises(MappingError):
        env.sync.sync_close(5005, TO_MSSP)  # "False Positive" has no label match on the MSSP side
    assert env.mails and env.mails[0]["function_name"] == "close"

    overridden = make_env(
        tmp_path,
        monkeypatch,
        override_maps={"customer-to-mssp": {"customer_resolution->resolution_id": {"897": 19433}}},
    )
    link(
        overridden,
        1005,
        5005,
        customer_values={"plan_status": "C", "properties": {"customer_resolution": 897}},
    )
    overridden.sync.sync_close(5005, TO_MSSP)
    assert overridden.mssp_raw.incidents[1005]["resolution_id"] == 19433


def test_close_is_skipped_while_source_is_open(env):
    link(env, 1006, 5006)
    assert env.sync.sync_close(1006, TO_CUSTOMER).status == "skipped"
    assert env.customer_raw.patches == []


def test_severity_uses_the_mapping_of_its_direction(env):
    link(env, 1007, 5007, customer_values={"severity_code": 1116})
    env.sync.sync_severity(5007, TO_MSSP)
    assert env.mssp_raw.incidents[1007]["severity_code"] == 19420


def test_reopen_only_when_source_is_active(env):
    link(env, 1008, 5008, {"plan_status": "C"}, {"plan_status": "C"})
    assert env.sync.sync_reopen(1008, TO_CUSTOMER).status == "skipped"
    env.mssp_raw.incidents[1008]["plan_status"] = "A"
    env.sync.sync_reopen(1008, TO_CUSTOMER)
    assert env.customer_raw.incidents[5008]["plan_status"] == "A"


def test_note_given_or_newest(env):
    link(env, 1009, 5009)
    env.mssp_raw.comments[1009] = [
        {"id": 1, "text": "<p>older</p>", "create_date": 10},
        {"id": 2, "text": "<p>newer</p>", "create_date": 20},
    ]
    env.sync.sync_note(1009, TO_CUSTOMER, note_id=1)
    env.sync.sync_note(1009, TO_CUSTOMER)
    assert [c["text"] for c in env.customer_raw.comments[5009]] == ["<p>older</p>", "<p>newer</p>"]
    assert env.customer_raw.posts[-1][1] == {"text": {"format": "html", "content": "<p>newer</p>"}}

    env.mssp_raw.add_incident(1010)
    assert env.sync.sync_note(1010, TO_CUSTOMER).status == "skipped"  # not linked


def test_report_from_mssp_tasks(env):
    link(env, 1011, 5011, {"properties": {"date_reported": 1767225600000}})
    env.mssp_raw.tasks[1011] = [
        {
            "id": 1,
            "name": "Describe the offense.",
            "notes": [{"text": "Password spraying from one IP"}],
        },
        {"id": 2, "name": "Provide suggestions.", "notes": [{"text": "Block the IP"}]},
    ]
    env.sync.sync_report(1011, TO_CUSTOMER)
    properties = env.customer_raw.incidents[5011]["properties"]
    assert properties["describe_the_incident"] == "Password spraying from one IP"
    assert properties["provide_recommendation"] == "Block the IP"
    assert properties["date_reported"] == 1767225600000


def test_analyst_assignment(env):
    link(env, 1012, 5012)
    assert env.sync.sync_analyst_assignment(1012).status == "skipped"
    env.mssp_raw.incidents[1012]["properties"]["analyst"] = "jdoe"
    env.sync.run("analyst", 1012, TO_CUSTOMER)
    assert env.customer_raw.incidents[5012]["properties"]["analyst_assigned"] is True
    with pytest.raises(ValueError):
        env.sync.run("delete", 1012, TO_CUSTOMER)


def test_missing_link_raises_and_alerts(env):
    env.mssp_raw.add_incident(1013, severity_code=19355)
    with pytest.raises(LookupError):
        env.sync.sync_severity(1013, TO_CUSTOMER)
    assert env.mails[0]["error_details"]["Incident ID"] == 1013


def make_migrator(env, tmp_path, **options):
    mapper = load_or_discover_mapper(tmp_path / "field_map.json", env.mssp, env.customer)
    state = MigrationState(tmp_path / "state.json")
    return IncidentMigrator(env.mssp, env.customer, state, mapper, env.config.fields, **options)


def test_migration_dry_run_writes_nothing(env, tmp_path):
    env.mssp_raw.add_incident(1, name="Old incident", severity_code=19356)
    result = make_migrator(
        env, tmp_path, dry_run=True, payload_dir=tmp_path / "payloads"
    ).migrate_range()
    assert result["outcomes"]["planned"] == 1
    assert env.customer_raw.incidents == {}
    assert not (tmp_path / "state.json").exists()
    planned = json.loads((tmp_path / "payloads" / "incident_1.json").read_text())
    assert planned["incident"]["severity_code"] == 5
    assert (tmp_path / "field_map.json").exists()  # saved for review


def test_migration_copies_and_never_duplicates(env, tmp_path):
    env.mssp_raw.add_incident(
        1,
        name="Closed phishing",
        severity_code=19357,
        plan_status="C",
        resolution_id=19545,
        resolution_summary="Mail removed",
        properties={"external_alert_id": "A-1", "customer_incident_id": "77", "analyst": "jdoe"},
    )
    env.mssp_raw.comments[1] = [{"id": 1, "text": "<p>note</p>"}]
    env.mssp_raw.artifacts[1] = [{"id": 9, "type": 1, "value": "203.0.113.5"}]
    env.customer_raw.fail_on = {"/artifacts"}

    result = make_migrator(env, tmp_path).migrate_range()
    assert result["outcomes"]["created"] == 1
    _target_id, created = next(iter(env.customer_raw.incidents.items()))
    assert (created["severity_code"], created["resolution_id"]) == (6, 782)
    # old links and unknown fields are dropped, the tracking field is added
    assert created["properties"] == {"external_alert_id": "A-1", "source_incident_id": 1}
    assert result["stats"]["comments"] == 1 and result["stats"]["failed_items"] == 1

    # state lost: the tracking field still prevents a second copy
    (tmp_path / "state.json").unlink()
    again = make_migrator(env, tmp_path).migrate_range()
    assert again["outcomes"] == {"created": 0, "existing": 1, "planned": 0, "limit": 0, "failed": 0}
    assert len(env.customer_raw.incidents) == 1


def test_migration_fallback_payload_default_resolution_and_limit(tmp_path, monkeypatch):
    env = make_env(tmp_path, monkeypatch, migration_default_resolution="1117")
    env.mssp_raw.add_incident(1, name="No resolution", plan_status="C", severity_code=19355)
    env.mssp_raw.add_incident(2, name="Second", severity_code=19355)
    env.customer_raw.reject_full_incident = True  # full payload rejected, core payload accepted

    result = make_migrator(env, tmp_path, max_create=1).migrate_range()
    assert result["outcomes"]["created"] == 1 and result["outcomes"]["limit"] == 1
    created = next(iter(env.customer_raw.incidents.values()))
    assert created["resolution_id"] == 1117 and "severity_code" not in created
    assert created["properties"] == {"source_incident_id": 1}


def test_resolution_backfill(env, tmp_path):
    raw = env.customer_raw
    raw.add_incident(
        5101,
        name="Incident generated - phishing simulation",
        plan_status="C",
        resolution_id=782,
        properties={"customer_resolution": 890},
    )
    raw.add_incident(
        5102,
        name="Real case",
        plan_status="C",
        resolution_id=782,
        properties={"customer_resolution": 889},
    )
    raw.add_incident(
        5103,
        name="Already right",
        plan_status="C",
        resolution_id=1117,
        properties={"customer_resolution": 890},
    )
    raw.add_incident(5104, name="Open", plan_status="A", properties={"customer_resolution": 889})

    backfill = CustomerResolutionBackfill(env.customer, tmp_path / "backfill.json")
    assert backfill.run(dry_run=True) == {"patched": 2, "skipped": 1, "errors": 0}
    assert raw.patches == []

    assert backfill.run() == {"patched": 2, "skipped": 1, "errors": 0}
    assert raw.incidents[5101]["resolution_id"] == 1120  # test incident -> Known Offense (Test)
    assert raw.incidents[5102]["resolution_id"] == 1121  # Not an Issue -> Non-Issue
    assert raw.incidents[5104].get("resolution_id") is None
    assert "Known Offense (Test)" in raw.comments[5101][0]["text"]
    assert CustomerResolutionBackfill(env.customer, tmp_path / "backfill.json").run() == {
        "patched": 0,
        "skipped": 0,
        "errors": 0,
    }


def test_config_and_cli():
    assert _cafile_option("false") is False and _cafile_option("TRUE") is True
    assert _cafile_option("/etc/pki/soar.pem") == "/etc/pki/soar.pem"
    with pytest.raises(ValueError):
        SoarInstanceConfig("customer", "", "", "", "").to_auth()
    catalog = FieldCatalog(CUSTOMER_FIELDS)
    assert (
        "customer_resolution" in catalog.properties
        and catalog.value_for("resolution_id", "non-issue") == 1121
    )
    args = build_parser().parse_args(["sync", "note", "--incident-id", "5", "--note-id", "7"])
    assert (args.action, args.incident_id, args.note_id, args.direction) == (
        "note",
        5,
        7,
        "mssp-to-customer",
    )
