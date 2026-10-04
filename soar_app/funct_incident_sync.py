"""SOAR app function ``incident_sync``.

Function component for an app generated with ``resilient-sdk codegen``; one
function serves every synchronization step so each playbook only differs in
its inputs:

- ``sync_action``: create, severity, close, reopen, note, report or analyst
- ``sync_direction``: mssp-to-customer (default) or customer-to-mssp
- ``incident_id``: incident the playbook runs on
- ``note_id``: optional, for the ``note`` action (note that triggered the playbook)

Both SOAR connections are read from the environment of the resilient-circuits
service (see ``.env.example``).
"""

from resilient_circuits import AppFunctionComponent, FunctionResult, app_function
from resilient_lib import IntegrationError, validate_fields

from soar_incident_synchronization.client import SoarApiClient
from soar_incident_synchronization.config import AppConfig, SyncDirection
from soar_incident_synchronization.sync import IncidentSynchronizer

PACKAGE_NAME = "fn_incident_sync"
FN_NAME = "incident_sync"


def _select_value(value):
    """Select inputs arrive as {"name": ...} objects, text inputs as plain strings."""
    return getattr(value, "name", None) or (value.get("name") if isinstance(value, dict) else value)


class FunctionComponent(AppFunctionComponent):
    def __init__(self, opts):
        super().__init__(opts, PACKAGE_NAME)
        config = AppConfig.from_env()
        # Kept for the lifetime of the service so field metadata is read only once.
        self.synchronizer = IncidentSynchronizer(
            config, SoarApiClient(config.mssp), SoarApiClient(config.customer)
        )

    @app_function(FN_NAME)
    def _app_function(self, fn_inputs):
        validate_fields(["sync_action", "incident_id"], fn_inputs)
        action = _select_value(fn_inputs.sync_action)
        direction = SyncDirection(
            _select_value(getattr(fn_inputs, "sync_direction", None)) or "mssp-to-customer"
        )
        note_id = getattr(fn_inputs, "note_id", None)

        yield self.status_message(
            f"Running {action} ({direction.value}) for incident {fn_inputs.incident_id}"
        )
        try:
            result = self.synchronizer.run(
                action,
                int(fn_inputs.incident_id),
                direction,
                note_id=int(note_id) if note_id else None,
            )
        except Exception as exc:
            # The synchronizer already logged the error and sent the alert mail.
            raise IntegrationError(f"{action} failed: {exc}") from exc
        yield FunctionResult(result.to_dict())
