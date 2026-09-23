"""JSON-only CLI. No personal settings or Git excludes are modified."""
import argparse
import json
import sys
from pathlib import Path

from .config import ConfigError, config_path, load_config, resolve_mode
from .hooks import binding, cursor, handle_event, recovery_context, workspace_root
from .store import StoreError, current, list_snapshots, record, resume, snapshot, storage_ready


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise StoreError("INVALID_ARGUMENTS")


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["hook"]:
        try:
            raw = sys.stdin.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError()
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise ValueError()
            result = handle_event(event, config_path())
        except (ValueError, OSError):
            result = {"systemMessage": "Provost: invalid hook input; no handoff saved."}
        print(json.dumps(result, ensure_ascii=True))
        return 0
    try:
        parser = Parser()
        parser.add_argument("command", choices=["doctor", "record", "snapshot", "list", "resume"])
        parser.add_argument("--workspace", required=True)
        parser.add_argument("--session")
        parser.add_argument("--expected")
        parser.add_argument("--request-id")
        parser.add_argument("--from", dest="source")
        options = parser.parse_args(args)
        root = workspace_root(options.workspace)
        cfg = load_config(config_path())
        mode = resolve_mode(cfg, root)
        if options.command == "doctor":
            error = None
            try:
                storage_ready(root)
            except StoreError as exc:
                error = str(exc)
            result = {"mode": mode, "python_supported": sys.version_info >= (3, 11),
                      "handoff": mode == "adaptive" and cfg.get("handoff", False),
                      "storage_ready": error is None, "storage_error": error,
                      "host_verified": False}
        else:
            if mode != "adaptive" or not cfg.get("handoff"):
                raise StoreError("HANDOFF_DISABLED")
            if options.command == "list":
                result = list_snapshots(root)
            else:
                if not options.session:
                    raise StoreError("SESSION_REQUIRED")
                source = binding(root, options.session)
                if not source:
                    raise StoreError("SESSION_NOT_BOUND")
                if options.command == "record":
                    if options.expected is None or not options.request_id:
                        raise StoreError("EXPECTED_REVISION_AND_REQUEST_REQUIRED")
                    raw = sys.stdin.read(32769)
                    if len(raw.encode("utf-8")) > 32768:
                        raise StoreError("PAYLOAD_TOO_LARGE")
                    payload = json.loads(raw)
                    result = record(root, options.session, payload,
                                    None if options.expected == "null" else options.expected,
                                    options.request_id, cursor=cursor(source.get("transcript_path")))
                elif options.command == "snapshot":
                    if not options.request_id:
                        raise StoreError("REQUEST_ID_REQUIRED")
                    result = snapshot(root, options.session, options.request_id)
                else:
                    if not options.source:
                        raise StoreError("SNAPSHOT_ID_REQUIRED")
                    result = resume(root, options.session, options.source)
                    result = {**result, "reconciliation": recovery_context(root, options.session)}
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except (ConfigError, StoreError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    except (OSError, UnicodeError, json.JSONDecodeError):
        print(json.dumps({"error": "INPUT_OR_IO_ERROR"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
