"""Small local checkpoint store; no model, network, or task scheduler."""
import copy
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import workspace_key

MAX_PAYLOAD = 32768
ID = re.compile(r"^[a-f0-9]{64}$")


class StoreError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _base(root):
    return Path(root).absolute() / ".provost" / "handoffs" / "v1"


def session_dir(root, session):
    if not isinstance(session, str) or not session or len(session) > 256:
        raise StoreError("INVALID_SESSION")
    return _base(root) / "sessions" / digest(session)


def guard(root, path):
    root, path = Path(root).absolute(), Path(path).absolute()
    try:
        parts = path.relative_to(root).parts
    except ValueError as exc:
        raise StoreError("OUTSIDE_WORKSPACE") from exc
    node = root
    for part in parts:
        node = node / part
        if node.is_symlink():
            raise StoreError("LINKED_STORAGE")
        try:
            info = node.lstat()
        except FileNotFoundError:
            continue
        if getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise StoreError("LINKED_STORAGE")


def storage_ready(root):
    guard(root, _base(root) / "sessions")
    try:
        probe = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                               capture_output=True, text=True, timeout=5)
        if probe.returncode:
            # An existing .git with an unreadable repository must not become non-Git.
            if (Path(root) / ".git").exists() or "dubious ownership" in probe.stderr:
                raise StoreError("GIT_UNREADABLE")
            return
        if workspace_key(Path(probe.stdout.strip())) != workspace_key(Path(root)):
            raise StoreError("NOT_WORKSPACE_ROOT")
        tracked = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--", ".provost/handoffs"],
                                 capture_output=True, timeout=5)
        if tracked.returncode:
            raise StoreError("GIT_UNREADABLE")
        if tracked.stdout:
            raise StoreError("TRACKED_HANDOFF")
        ignored = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", "--", ".provost/handoffs/probe"],
                                 capture_output=True, timeout=5)
        if ignored.returncode:
            raise StoreError("HANDOFF_NOT_IGNORED")
    except FileNotFoundError as exc:
        if (Path(root) / ".git").exists():
            raise StoreError("GIT_UNAVAILABLE") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StoreError("GIT_UNREADABLE") from exc


def validate_payload(payload):
    required = {"work_id", "phase", "sources", "assignments", "decisions", "blockers",
                "evidence", "next", "suggested_skills"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise StoreError("INVALID_PAYLOAD")
    for key in ("work_id", "phase", "next"):
        if not isinstance(payload[key], str) or not payload[key]:
            raise StoreError("INVALID_PAYLOAD")
    for key in ("sources", "assignments", "decisions", "blockers", "evidence", "suggested_skills"):
        if not isinstance(payload[key], list):
            raise StoreError("INVALID_PAYLOAD")
    for key in ("decisions", "blockers", "suggested_skills"):
        if any(not isinstance(item, str) for item in payload[key]):
            raise StoreError("INVALID_PAYLOAD")
    for source in payload["sources"]:
        if (not isinstance(source, dict) or set(source) - {"kind", "ref", "item"}
                or source.get("kind") not in ("file", "url")
                or not isinstance(source.get("ref"), str) or not source["ref"]
                or ("item" in source and not isinstance(source["item"], str))):
            raise StoreError("INVALID_SOURCE")
    for assignment in payload["assignments"]:
        if (not isinstance(assignment, dict)
                or set(assignment) != {"task", "agent_id", "observed_state", "write_scope"}
                or any(not isinstance(assignment[k], str) for k in ("task", "agent_id", "observed_state"))
                or not isinstance(assignment["write_scope"], list)
                or any(not isinstance(p, str) for p in assignment["write_scope"])):
            raise StoreError("INVALID_ASSIGNMENT")
    for item in payload["evidence"]:
        if (not isinstance(item, dict) or set(item) != {"claim", "ref", "state"}
                or any(not isinstance(v, str) for v in item.values())
                or item["state"] not in ("pass-observed", "fail", "unverified", "stale")):
            raise StoreError("INVALID_EVIDENCE")
    try:
        if len(encoded(payload)) > MAX_PAYLOAD:
            raise StoreError("PAYLOAD_TOO_LARGE")
    except (TypeError, ValueError) as exc:
        if isinstance(exc, StoreError):
            raise
        raise StoreError("INVALID_PAYLOAD") from exc


def atomic_write(root, path, data):
    guard(root, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    guard(root, path)
    temp = None
    try:
        fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
        temp = Path(name)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded(data))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    except OSError as exc:
        raise StoreError("WRITE_FAILED") from exc
    finally:
        if temp is not None and temp.exists():
            temp.unlink()


@contextmanager
def _lock(root, directory):
    storage_ready(root)
    guard(root, directory / ".lock")
    directory.mkdir(parents=True, exist_ok=True)
    try:
        with (directory / ".lock").open("a+b") as stream:
            stream.seek(0, 2)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise StoreError("STORE_BUSY") from exc
            else:
                import fcntl
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise StoreError("STORE_BUSY") from exc
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        raise StoreError("WRITE_FAILED") from exc


def _read(root, path, session, kind):
    guard(root, path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StoreError("CORRUPT_RECORD") from exc
    if (not isinstance(data, dict) or data.get("schema_version") != 1
            or data.get("workspace_id") != workspace_key(root)
            or data.get("session_id") != session or data.get("kind") != kind):
        raise StoreError("RECORD_IDENTITY_MISMATCH")
    ident = "revision" if kind == "record" else "snapshot_id"
    if data.get(ident) != path.stem:
        raise StoreError("RECORD_IDENTITY_MISMATCH")
    checksum = data.get("checksum")
    body = {k: v for k, v in data.items() if k != "checksum"}
    if checksum != hashlib.sha256(encoded(body)).hexdigest():
        raise StoreError("CORRUPT_RECORD")
    validate_payload(data.get("payload"))
    return data


def _seal(data):
    return {**data, "checksum": hashlib.sha256(encoded(data)).hexdigest()}


def _head(root, session, kind):
    directory = session_dir(root, session)
    index = directory / ("current.json" if kind == "record" else "latest.json")
    collection = directory / ("records" if kind == "record" else "snapshots")
    guard(root, collection)
    guard(root, index)
    if not collection.exists():
        return None
    id_key = "revision" if kind == "record" else "snapshot_id"
    parent_key = "parent_record" if kind == "record" else "parent_snapshot"
    # Index files are hints, never authority: a completed object can outlive
    # an interrupted index replacement. Reconcile the complete chain on reads.
    # Only complete canonical objects participate; leftover temporary files do not.
    objects = {}
    for path in collection.glob("*.json"):
        if not ID.fullmatch(path.stem):
            continue
        objects[path.stem] = _read(root, path, session, kind)
    if not objects:
        return None
    parents = {obj.get(parent_key) for obj in objects.values() if obj.get(parent_key)}
    if parents - objects.keys():
        raise StoreError("INCOMPLETE_CHAIN")
    heads = set(objects) - parents
    if len(heads) != 1:
        raise StoreError("AMBIGUOUS_CHAIN")
    head = objects[heads.pop()]
    visited = set()
    node = head
    while node:
        ident = node[id_key]
        if ident in visited:
            raise StoreError("AMBIGUOUS_CHAIN")
        visited.add(ident)
        node = objects.get(node.get(parent_key))
    if len(visited) != len(objects):
        raise StoreError("AMBIGUOUS_CHAIN")
    return head


def current(root, session):
    return _head(root, session, "record")


def latest(root, session):
    return _head(root, session, "snapshot")


def record(root, session, payload, expected, request_id, *, cursor=None, adopted_from=None):
    validate_payload(payload)
    if not isinstance(request_id, str) or not request_id or len(request_id) > 512:
        raise StoreError("INVALID_REQUEST_ID")
    directory = session_dir(root, session)
    revision = digest(session + "\0record\0" + request_id)
    path = directory / "records" / (revision + ".json")
    with _lock(root, directory):
        if path.exists():
            prior = _read(root, path, session, "record")
            if prior["payload"] != payload or prior.get("adopted_from") != adopted_from:
                raise StoreError("REQUEST_ID_REUSED")
            return prior
        previous = current(root, session)
        if expected != (previous["revision"] if previous else None):
            raise StoreError("REVISION_CONFLICT")
        data = _seal({"schema_version": 1, "kind": "record", "workspace_id": workspace_key(root),
                      "session_id": session, "revision": revision, "parent_record": expected,
                      "recorded_at": datetime.now(timezone.utc).isoformat(),
                      "transcript_cursor": cursor, "adopted_from": adopted_from, "payload": copy.deepcopy(payload)})
        atomic_write(root, path, data)
        atomic_write(root, directory / "current.json", {"revision": revision})
        return data


def snapshot(root, session, event_key):
    if not isinstance(event_key, str) or not event_key or len(event_key) > 1024:
        raise StoreError("INVALID_EVENT_KEY")
    directory = session_dir(root, session)
    ident = digest(session + "\0snapshot\0" + event_key)
    path = directory / "snapshots" / (ident + ".json")
    with _lock(root, directory):
        if path.exists():
            return _read(root, path, session, "snapshot")
        source = current(root, session)
        if source is None:
            raise StoreError("NO_CHECKPOINT")
        previous = latest(root, session)
        data = {k: v for k, v in source.items() if k != "checksum"}
        data.update(kind="snapshot", snapshot_id=ident, captured_at=datetime.now(timezone.utc).isoformat(),
                    parent_snapshot=previous["snapshot_id"] if previous else None)
        data = _seal(data)
        atomic_write(root, path, data)
        atomic_write(root, directory / "latest.json", {"snapshot_id": ident})
        return data


def list_snapshots(root, snapshot_id=None):
    base = _base(root) / "sessions"
    guard(root, base)
    if not base.exists():
        return []
    result = []
    for folder in sorted(base.iterdir()):
        guard(root, folder)
        if not folder.is_dir() or not ID.fullmatch(folder.name):
            continue
        collection = folder / "snapshots"
        guard(root, collection)
        for path in collection.glob(snapshot_id + ".json" if snapshot_id else "*.json"):
            guard(root, path)
            if not ID.fullmatch(path.stem):
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                session = raw.get("session_id")
                if not isinstance(session, str) or digest(session) != folder.name:
                    raise StoreError("RECORD_IDENTITY_MISMATCH")
                data = _read(root, path, session, "snapshot")
            except (StoreError, OSError, UnicodeError, json.JSONDecodeError, AttributeError) as exc:
                code = str(exc) if isinstance(exc, StoreError) else "CORRUPT_RECORD"
                result.append({"snapshot_id": path.stem, "error": code})
                continue
            result.append({k: data[k] for k in ("snapshot_id", "session_id", "captured_at", "parent_snapshot")}
                          | {"work_id": data["payload"]["work_id"], "phase": data["payload"]["phase"]})
    return result


def resume(root, session, snapshot_id):
    if not isinstance(snapshot_id, str) or not ID.fullmatch(snapshot_id):
        raise StoreError("INVALID_SNAPSHOT_ID")
    matches = [item for item in list_snapshots(root, snapshot_id) if item["snapshot_id"] == snapshot_id]
    if len(matches) != 1:
        raise StoreError("SNAPSHOT_NOT_FOUND")
    if "error" in matches[0]:
        raise StoreError(matches[0]["error"])
    original = matches[0]["session_id"]
    if original == session:
        raise StoreError("SAME_SESSION_ADOPTION")
    source = _read(root, session_dir(root, original) / "snapshots" / (snapshot_id + ".json"), original, "snapshot")
    payload = copy.deepcopy(source["payload"])
    for assignment in payload["assignments"]:
        assignment["observed_state"] = "needs-verification"
    for evidence in payload["evidence"]:
        evidence["state"] = "unverified"
    return record(root, session, payload, None, "adopt:" + snapshot_id, adopted_from=snapshot_id)


def _only_binding(root, directory):
    """(path, lstat) when directory is a real dir holding exactly one regular binding.json."""
    guard(root, directory / "binding.json")
    if not directory.is_dir() or [child.name for child in directory.iterdir()] != ["binding.json"]:
        return None
    binding = directory / "binding.json"
    info = binding.lstat()
    return (binding, info) if stat.S_ISREG(info.st_mode) else None


def _stale_binding(root, entry, cutoff):
    """(binding.json, lstat) when entry is a session dir holding only a binding older than cutoff."""
    if not ID.fullmatch(entry.name):
        return None
    found = _only_binding(root, entry)
    return found if found and found[1].st_mtime < cutoff else None


def _remove_stale(root, entry, staging, info):
    """Move the dir aside, re-verify it is unchanged, then delete it.

    A concurrent SessionStart that rewrote binding.json before the move is caught
    by the re-check and the dir is moved back; one after the move recreates the
    original dir, which prune never touches again. Returns True when removed.
    """
    entry.rename(staging)
    again = _only_binding(root, staging)
    if again is None or again[1].st_mtime_ns != info.st_mtime_ns:
        staging.rename(entry)
        return False
    try:
        again[0].unlink()
    except OSError:
        staging.rename(entry)
        raise
    staging.rmdir()
    return True


def prune(root, *, older_than_days=14, keep_session=None, apply=False, now=None):
    """List (or with apply, remove) session dirs that hold only an old binding.json.

    Such a dir records only that a session started; it has no records, snapshots
    or locks. Linked or foreign entries are skipped, never followed or removed.
    """
    if not isinstance(older_than_days, int) or isinstance(older_than_days, bool) or older_than_days < 1:
        raise StoreError("INVALID_ARGUMENTS")
    sessions = _base(root) / "sessions"
    guard(root, sessions)
    result = {"older_than_days": older_than_days, "applied": bool(apply), "candidates": [],
              "removed": 0, "errors": []}
    if not sessions.is_dir():
        return result
    keep = digest(keep_session) if keep_session else None
    cutoff = (now if now is not None else datetime.now(timezone.utc).timestamp()) - older_than_days * 86400
    for entry in sorted(sessions.iterdir()):
        if entry.name == keep:
            continue
        staging = sessions / (".pruning-" + entry.name)
        try:
            found = _stale_binding(root, entry, cutoff)
            if found is None:
                continue
            binding, info = found
            try:
                session_id = json.loads(binding.read_text(encoding="utf-8")).get("session_id")
            except (OSError, ValueError, AttributeError):
                session_id = None
            result["candidates"].append({
                "session_dir": entry.name, "session_id": session_id if isinstance(session_id, str) else None,
                "bound_at": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat()})
            if apply and _remove_stale(root, entry, staging, info):
                result["removed"] += 1
        except StoreError:
            continue
        except OSError as exc:
            result["errors"].append({"session_dir": entry.name, "error": type(exc).__name__,
                                     "left_at": staging.name if staging.exists() else None})
    return result
