"""JSON-only CLI. No personal settings or Git excludes are modified."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

from . import legacy
from .config import ConfigError, config_path, handoff_home, load_config, resolve_mode
from .hooks import (binding, cursor, handle_event, last_binding_at, recovery_context,
                    resolve_transcript, workspace_root)
from .store import StoreError, current, latest, list_snapshots, prune, record, snapshot, storage_ready


RUNTIME_ROOT = Path(__file__).resolve().parents[2]


def runtime_hash(root=RUNTIME_ROOT):
    """Line-ending-insensitive digest of the runtime files; a missing file changes it."""
    root = Path(root)
    rels = {p.relative_to(root).as_posix() for p in (root / "scripts" / "provost_runtime").glob("*.py")}
    rels |= {"hooks/runtime", "hooks/native-event", "hooks/native-reminder", "hooks/_python.sh"}
    digest = hashlib.sha256()
    for rel in sorted(rels):
        try:
            body = (root / rel).read_bytes().replace(b"\r\n", b"\n")
        except OSError:
            body = b"<missing>"
        digest.update(rel.encode("utf-8") + b"\0" + body + b"\0")
    return digest.hexdigest()


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise StoreError("INVALID_ARGUMENTS")


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args in (["hook"], ["native-hook"]):
        try:
            raw = sys.stdin.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError()
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise ValueError()
            result = handle_event(event, config_path(), native=args == ["native-hook"])
        except (ValueError, OSError):
            result = {"systemMessage": "Provost: invalid hook input; no handoff saved."}
        print(json.dumps(result, ensure_ascii=True))
        return 0
    try:
        parser = Parser()
        parser.add_argument("command", choices=["doctor", "record", "snapshot", "list", "resume", "status", "prune"])
        parser.add_argument("--workspace", required=True)
        parser.add_argument("--session")
        parser.add_argument("--expected")
        parser.add_argument("--request-id")
        parser.add_argument("--from", dest="source")
        parser.add_argument("--input")
        parser.add_argument("--older-than-days", type=int, default=14)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--legacy", action="store_true")
        options = parser.parse_args(args)
        root = workspace_root(options.workspace)
        cfg = load_config(config_path())
        mode = resolve_mode(cfg, root.root)
        if options.command == "doctor":
            error = None
            try:
                storage_ready(root)
            except StoreError as exc:
                error = str(exc)
            bound_at = last_binding_at(root)
            try:
                stale = len(prune(root)["candidates"])
            except (StoreError, OSError):
                stale = None
            result = {"mode": mode, "python_supported": sys.version_info >= (3, 11),
                      "handoff": mode == "adaptive" and cfg.get("handoff", False),
                      "storage_ready": error is None, "storage_error": error,
                      "host_verified": bound_at is not None, "last_binding_at": bound_at,
                      "stale_sessions": stale, "legacy_store_present": legacy.present(root),
                      "store_home": str(handoff_home()), "repo_key": root.repo_key,
                      "runtime_root": str(RUNTIME_ROOT), "runtime_hash": runtime_hash(RUNTIME_ROOT)}
        else:
            if mode != "adaptive" or not cfg.get("handoff"):
                raise StoreError("HANDOFF_DISABLED")
            if options.command == "list":
                result = legacy.list_snapshots(root) if options.legacy else list_snapshots(root)
            elif options.command == "prune":
                result = prune(root, older_than_days=options.older_than_days,
                               keep_session=options.session, apply=options.apply)
            else:
                if not options.session:
                    raise StoreError("SESSION_REQUIRED")
                source = binding(root, options.session)
                if not source:
                    raise StoreError("SESSION_NOT_BOUND")
                if options.command == "status":
                    checkpoint = current(root, options.session)
                    old = None
                    if checkpoint is None and latest(root, options.session) is None:
                        old = legacy.checkpoint(root, options.session)
                    result = {"workspace": str(root.root), "session_id": options.session,
                              "expected": checkpoint["revision"] if checkpoint else None,
                              "checkpoint": old[0] if old else checkpoint,
                              **({"legacy": True, "legacy_path": str(old[1])} if old else {}),
                              "template": {"work_id": "", "phase": "", "sources": [],
                                           "assignments": [], "decisions": [], "blockers": [],
                                           "evidence": [], "next": "", "suggested_skills": []}}
                elif options.command == "record":
                    if options.expected is None or not options.request_id:
                        raise StoreError("EXPECTED_REVISION_AND_REQUEST_REQUIRED")
                    if options.input is None:
                        raw = sys.stdin.read(32769)
                    else:
                        with open(options.input, encoding="utf-8", newline="") as handle:
                            raw = handle.read(32769)
                    if len(raw.encode("utf-8")) > 32768:
                        raise StoreError("PAYLOAD_TOO_LARGE")
                    payload = json.loads(raw)
                    result = record(root, options.session, payload,
                                    None if options.expected == "null" else options.expected,
                                    options.request_id, cursor=cursor(resolve_transcript(source.get("transcript_path"))))
                elif options.command == "snapshot":
                    if not options.request_id:
                        raise StoreError("REQUEST_ID_REQUIRED")
                    result = snapshot(root, options.session, options.request_id)
                else:
                    if not options.source:
                        raise StoreError("SNAPSHOT_ID_REQUIRED")
                    result = legacy.resume(root, options.session, options.source)
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
