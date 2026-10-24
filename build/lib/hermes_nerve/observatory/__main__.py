"""Command line entry point: python -m hermes_nerve.observatory {serve,snapshot,record}.

serve     Start the read-only observatory. Loopback by default. A non-loopback
          bind requires HERMES_NERVE_OBSERVATORY_TOKEN in the environment.
snapshot  Print one live snapshot as JSON (no server).
record    Append one sanitized, derived history row to the bounded segmented
          store (--history-dir). The legacy unsegmented --history file is not
          migrated, read, or overwritten, and does not meet the segmented contract.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import collector, history_store, recorder, server, fleet_monitor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hermes_nerve.observatory")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="serve the read-only observatory")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--store", type=Path, default=None, help="supervision store path")
    serve.add_argument("--shadow", type=Path, default=None, help="D1/shadow telemetry JSONL path")
    serve.add_argument(
        "--history-dir",
        type=Path,
        default=None,
        help="bounded segmented history directory (default: report-home observatory/history)",
    )
    serve.add_argument(
        "--history-interval",
        type=float,
        default=None,
        help="opt-in: seconds between history rows while serving (default: disabled)",
    )

    serve.add_argument(
        "--fleet-interval", type=float, default=None,
        help="opt-in interval in seconds for durable j2/win4060 Hermes process census (3-3600)",
    )

    snap = sub.add_parser("snapshot", help="print one live snapshot as JSON")
    snap.add_argument("--store", type=Path, default=None)
    snap.add_argument("--shadow", type=Path, default=None)

    rec = sub.add_parser(
        "record",
        help="append one sanitized history row to the bounded segmented store",
        description=(
            "Append one sanitized history row to the bounded segmented store "
            "(--history-dir). The legacy unsegmented --history file is kept only for "
            "explicit compatibility; it is never read, migrated, or overwritten."
        ),
    )
    rec.add_argument("--store", type=Path, default=None)
    rec.add_argument("--shadow", type=Path, default=None)
    rec.add_argument(
        "--history-dir",
        type=Path,
        default=None,
        help="segmented history directory (default: report-home observatory/history)",
    )
    rec.add_argument(
        "--history",
        type=Path,
        default=None,
        help="DEPRECATED legacy unsegmented JSONL file. Does not satisfy the segmented contract; not migrated.",
    )

    args = parser.parse_args(argv)
    if args.command == "serve":
        # Validate the opt-in interval before any socket or thread exists.
        interval = None
        if args.history_interval is not None:
            try:
                interval = recorder.validate_interval(args.history_interval)
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
        if args.fleet_interval is not None and not 3.0 <= args.fleet_interval <= 3600.0:
            print("error: --fleet-interval must be between 3 and 3600 seconds", file=sys.stderr)
            return 2
        try:
            srv = server.make_server(
                args.host,
                args.port,
                store_path=args.store,
                shadow_path=args.shadow,
                history_dir=args.history_dir,
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        host, port = srv.server_address[0], srv.server_address[1]
        auth = "bearer token required on every /api request"
        history = None
        if interval is not None:
            history = recorder.HistoryRecorder(
                args.history_dir,
                store_path=args.store,
                shadow_path=args.shadow,
                interval=interval,
            )
            srv.history_recorder = history
            history.start()
            print(f"history: capture enabled every {interval:g}s to the bounded segmented store", flush=True)
        fleet = None
        if args.fleet_interval is not None:
            db_dir = args.history_dir or (collector.report_home() / "observatory" / "history")
            fleet = fleet_monitor.FleetMonitor(db_dir / "hermes-fleet.sqlite3",
                                                interval=args.fleet_interval)
            srv.fleet_monitor = fleet
            fleet.start()
            print("fleet: bounded process census enabled for j2 and win4060; "
                  "session details require independent evidence", flush=True)
        print(f"Nerve observatory on http://{host}:{port}/ ({auth}). Ctrl-C to stop.", flush=True)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            srv.server_close()
            if history is not None:
                history.stop()  # Orderly: join the loop, then write the closing row.
            if fleet is not None:
                fleet.stop()
        return 0
    if args.command == "snapshot":
        print(json.dumps(collector.build_snapshot(store_path=args.store, shadow_path=args.shadow), indent=2, sort_keys=True))
        return 0
    if args.command == "record":
        # Refuse ambiguous targets before any snapshot is built or any file is touched.
        if args.history is not None and args.history_dir is not None:
            print("error: --history and --history-dir are mutually exclusive; nothing was written", file=sys.stderr)
            return 2
        snapshot = collector.build_snapshot(store_path=args.store, shadow_path=args.shadow)
        if args.history is not None:
            # Explicit legacy path only. Writes the old unsegmented file; it does NOT
            # satisfy the bounded segmented contract and is never migrated.
            path = collector.append_history(snapshot, args.history)
            print(f"appended LEGACY unsegmented history row to {path} (not the bounded store)")
            return 0
        directory = args.history_dir or (collector.report_home() / "observatory" / "history")
        try:
            result = history_store.append_row(directory, collector.history_row(snapshot))
        except history_store.HistoryStorageFull:
            print("error: history storage is full; no row written and no segment was deleted", file=sys.stderr)
            return 3
        print(f"appended history row to segment {result.path.name}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
