"""Command line entry point: python -m soar_incident_synchronization.cli <command> ..."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .client import SoarApiClient
from .config import AppConfig, SyncDirection
from .migration import IncidentMigrator, MigrationState, load_or_discover_mapper
from .resolution_backfill import CustomerResolutionBackfill
from .sync import IncidentSynchronizer

SYNC_ACTIONS = ("create", "severity", "close", "reopen", "note", "report", "analyst")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="IBM SOAR incident synchronization")
    subparsers = parser.add_subparsers(dest="command", required=True)
    directions = [item.value for item in SyncDirection]

    sync = subparsers.add_parser("sync", help="apply one change to the linked incident")
    sync.add_argument("action", choices=SYNC_ACTIONS)
    sync.add_argument("--incident-id", type=int, required=True)
    sync.add_argument(
        "--direction", choices=directions, default=SyncDirection.MSSP_TO_CUSTOMER.value
    )
    sync.add_argument(
        "--note-id", type=int, help="note to copy (action 'note'); default: newest note"
    )

    migrate = subparsers.add_parser("migrate", help="copy incidents to the other instance")
    migrate.add_argument(
        "--direction", choices=directions, default=SyncDirection.MSSP_TO_CUSTOMER.value
    )
    migrate.add_argument("--state-file", type=Path, default=Path("state/migration_state.json"))
    migrate.add_argument(
        "--field-map",
        type=Path,
        default=Path("state/field_map.json"),
        help="value mapping; discovered and saved on the first run, edit it to correct mappings",
    )
    migrate.add_argument(
        "--start", type=int, help="query offset (default: resume from the state file)"
    )
    migrate.add_argument("--limit", type=int, help="number of source incidents to process")
    migrate.add_argument("--max-create", type=int, help="stop after creating this many incidents")
    migrate.add_argument("--batch-size", type=int, default=100)
    migrate.add_argument(
        "--dry-run", action="store_true", help="build payloads only, write nothing"
    )
    migrate.add_argument(
        "--payload-dir", type=Path, help="with --dry-run: save each planned payload as JSON"
    )

    backfill = subparsers.add_parser(
        "backfill-resolution", help="derive resolution_id from the customer close reason"
    )
    backfill.add_argument("--state-file", type=Path, default=Path("state/resolution_backfill.json"))
    backfill.add_argument("--start", type=int, default=0)
    backfill.add_argument("--limit", type=int)
    backfill.add_argument("--batch-size", type=int, default=100)
    backfill.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    config = AppConfig.from_env()
    mssp, customer = SoarApiClient(config.mssp), SoarApiClient(config.customer)

    if args.command == "sync":
        synchronizer = IncidentSynchronizer(config, mssp, customer)
        result = synchronizer.run(
            args.action, args.incident_id, SyncDirection(args.direction), note_id=args.note_id
        )
        output = result.to_dict()
    elif args.command == "migrate":
        source, target = (
            (mssp, customer)
            if args.direction == SyncDirection.MSSP_TO_CUSTOMER.value
            else (customer, mssp)
        )
        state = MigrationState(args.state_file)
        migrator = IncidentMigrator(
            source,
            target,
            state,
            load_or_discover_mapper(args.field_map, source, target),
            config.fields,
            dry_run=args.dry_run,
            payload_dir=args.payload_dir,
            max_create=args.max_create,
        )
        start = args.start if args.start is not None else int(state.data.get("last_offset", 0))
        output = migrator.migrate_range(start=start, limit=args.limit, batch_size=args.batch_size)
    else:
        backfill = CustomerResolutionBackfill(
            customer,
            args.state_file,
            resolution_field=config.fields.customer_resolution_field,
            test_marker=config.fields.test_incident_marker,
        )
        output = backfill.run(
            start=args.start, limit=args.limit, batch_size=args.batch_size, dry_run=args.dry_run
        )

    json.dump(output, sys.stdout, indent=2, default=str)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
