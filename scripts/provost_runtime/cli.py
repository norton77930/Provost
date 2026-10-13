"""JSON-only CLI. No personal settings or Git excludes are modified."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

from . import facts, redact, search
from .config import ConfigError, config_path, handoff_home, load_config, resolve_mode
from .hooks import (binding, cursor, handle_event, last_binding_at, recovery_context,
                    resolve_transcript, workspace_root)
from .facts import SPOOL_SESSION
from .store import (FACTS_FLAGS, StoreError, cards, guard, list_snapshots, newest_entries, prune, read_log, record,
                    resume, snapshot, storage_ready)


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
REMINDER_FLAGS = FACTS_FLAGS  # touched by hooks/native-facts
STATUS_SOURCES = 10  # recent repo files prefilled into template.sources
STATUS_LOG = 10  # newest decision log entries of the shown work in status


def clear_reminder_flags(cfg_path, session):
    """Best effort: a missing dir or file, or any I/O error, leaves the record untouched."""
    if not isinstance(session, str) or not SPOOL_SESSION.fullmatch(session):
        return
    cache = Path(cfg_path).parent / "cache"
    for name in REMINDER_FLAGS:
        flag = cache / "facts" / f"{session}.{name}"
        try:
            guard(cache, flag)
            flag.unlink(missing_ok=True)
        except (OSError, StoreError):
            continue


def runtime_hash(root=RUNTIME_ROOT):
    """Line-ending-insensitive digest of the runtime files; a missing file changes it."""
    root = Path(root)
    rels = {p.relative_to(root).as_posix() for p in (root / "scripts" / "provost_runtime").glob("*.py")}
    rels |= {"hooks/runtime", "hooks/native-event", "hooks/native-facts", "hooks/_python.sh"}
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
    if args == ["native-hook"]:
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
        parser.add_argument("command", choices=["doctor", "record", "snapshot", "list", "resume", "status", "changes", "prune", "search", "redact"])
        parser.add_argument("--workspace", required=True)
        parser.add_argument("--session")
        parser.add_argument("--expected")
        parser.add_argument("--request-id")
        parser.add_argument("--from", dest="source")
        parser.add_argument("--input")
        # status: the card of this work_id instead of the session's most recently written one.
        parser.add_argument("--work")
        # Prune ages: binding-only dirs (default 14 days) and expired sessions (default 30).
        parser.add_argument("--older-than-days", type=int, default=None)
        parser.add_argument("--expired-older-than-days", type=int, default=None)
        parser.add_argument("--apply", action="store_true")
        # search: keywords, optional date floor/limit/ref (validated in search.py), and scope.
        parser.add_argument("--query")
        parser.add_argument("--since")
        parser.add_argument("--limit")
        parser.add_argument("--show")
        parser.add_argument("--all-repos", action="store_true")
        # redact (maintainer only, see docs): the entry of the --work log and an optional reason.
        parser.add_argument("--entry")
        parser.add_argument("--reason")
        options = parser.parse_args(args)
        root = workspace_root(options.workspace)
        cfg = load_config(config_path())
        mode = resolve_mode(cfg, root.root, root.main)
        if options.command == "doctor":
            error = None
            try:
                storage_ready(root)
            except StoreError as exc:
                error = str(exc)
            bound_at = last_binding_at(root)
            try:
                listing = prune(root)  # dry run: doctor never deletes
                stale, expired = len(listing["candidates"]), len(listing["expired"])
            except (StoreError, OSError):
                stale = expired = None
            result = {"mode": mode, "python_supported": sys.version_info >= (3, 11),
                      "handoff": mode == "adaptive" and cfg.get("handoff", False),
                      "storage_ready": error is None, "storage_error": error,
                      "host_verified": bound_at is not None, "last_binding_at": bound_at,
                      "stale_sessions": stale, "expired_sessions": expired,
                      "store_home": str(handoff_home()), "repo_key": root.repo_key,
                      "runtime_root": str(RUNTIME_ROOT), "runtime_hash": runtime_hash(RUNTIME_ROOT)}
        else:
            if mode != "adaptive" or not cfg.get("handoff"):
                raise StoreError("HANDOFF_DISABLED")
            if options.command == "list":
                result = list_snapshots(root)
            elif options.command == "search":
                if options.show is not None:
                    result = search.show(root, options.show, all_repos=options.all_repos)
                else:
                    result = search.find(root, options.query, all_repos=options.all_repos,
                                         since=options.since, limit=options.limit)
            elif options.command == "redact":
                if not options.work or options.entry is None:
                    raise StoreError("INVALID_ARGUMENTS")
                result = redact.redact(root, options.work, options.entry, options.reason)
            elif options.command == "prune":
                result = prune(root, older_than_days=options.older_than_days,
                               expired_older_than_days=options.expired_older_than_days,
                               keep_session=options.session, apply=options.apply,
                               facts_cache=config_path().parent / "cache" / "facts")
            else:
                if not options.session:
                    raise StoreError("SESSION_REQUIRED")
                source = binding(root, options.session)
                if not source:
                    raise StoreError("SESSION_NOT_BOUND")
                if options.command == "status":
                    if options.work == "":
                        raise StoreError("INVALID_ARGUMENTS")
                    heads = cards(root, options.session)
                    work = options.work if options.work is not None else heads[0]["payload"]["work_id"] if heads else None
                    checkpoint = next((head for head in heads if head["payload"]["work_id"] == work), None)
                    result = {"workspace": str(root.root), "session_id": options.session,
                              "expected": checkpoint["revision"] if checkpoint else None,
                              "checkpoint": checkpoint,
                              "facts": None,
                              "template": {"work_id": "", "phase": "", "sources": [],
                                           "assignments": [], "blockers": [], "next": "", "suggested_skills": [],
                                           "new_decisions": [], "new_evidence": []}}
                    others = [{"work_id": head["payload"]["work_id"], "phase": head["payload"]["phase"],
                               "revision": head["revision"], "recorded_at": head["recorded_at"]}
                              for head in heads if head["payload"]["work_id"] != work]
                    if others:  # only for a session with several works: single-work output is unchanged
                        result["other_works"] = others
                    try:
                        entries = read_log(root, work) if work is not None else []
                        result["log"] = {"total": len(entries), "newest": newest_entries(entries, STATUS_LOG)}
                    except StoreError as exc:
                        result["log"] = {"error": str(exc)}
                    try:
                        stored, stored_path = facts.stored(root, options.session)
                    except (StoreError, OSError):
                        stored = None  # facts are a convenience; status stays readable without them
                    if stored:
                        result["facts"] = facts.summary(stored, stored_path)
                        result["template"]["sources"] = [{"kind": "file", "ref": path}
                                                         for path in facts.recent_files(stored, STATUS_SOURCES)]
                elif options.command == "changes":
                    result = facts.changes(config_path(), root, options.session)
                elif options.command == "record":
                    if options.expected is None or not options.request_id:
                        raise StoreError("EXPECTED_REVISION_AND_REQUEST_REQUIRED")
                    if options.input is None:
                        # UTF-8 regardless of the console/locale encoding; size checked before decoding
                        # so a cut multi-byte character still reports PAYLOAD_TOO_LARGE.
                        data = sys.stdin.buffer.read(32769)
                        if len(data) > 32768:
                            raise StoreError("PAYLOAD_TOO_LARGE")
                        raw = data.decode("utf-8")
                    else:
                        with open(options.input, encoding="utf-8", newline="") as handle:
                            raw = handle.read(32769)
                    if len(raw.encode("utf-8")) > 32768:
                        raise StoreError("PAYLOAD_TOO_LARGE")
                    payload = json.loads(raw)
                    result = record(root, options.session, payload,
                                    None if options.expected == "null" else options.expected,
                                    options.request_id, cursor=cursor(resolve_transcript(source.get("transcript_path"))))
                    clear_reminder_flags(config_path(), options.session)
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
        detail = getattr(exc, "detail", None)
        print(json.dumps({"error": str(exc), **({"detail": detail} if detail else {})}))
        return 1
    except (OSError, UnicodeError, json.JSONDecodeError):
        print(json.dumps({"error": "INPUT_OR_IO_ERROR"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
