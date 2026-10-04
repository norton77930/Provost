"""Temporary read-only bridge to the pre-relocation handoff stores.

Before the store moved under the handoff home, checkpoints lived in
`<workspace>/.provost/handoffs/v1`, one store per git top-level (a linked worktree
and its main checkout each had their own). This module reads those stores so a
session that began before the move can still be recovered. It never writes: no
directories, locks, index files or temporary files are created under a legacy path.

REMOVAL: delete this module after the observation window (about 2 weeks after
release), plus its call sites: `status` / `list --legacy` / `resume` / `doctor` in
cli.py and `recovery_context` in hooks.py. Nothing else depends on it.
"""
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .config import workspace_key
from .store import (ID, StoreError, adopt, as_workspace, digest, encoded, guard, resume as store_resume,
                    validate_payload)


@dataclass(frozen=True)
class Location:
    workspace: Path   # dir that holds `.provost`; its old path-based key is the stored workspace_id
    key: str

    @property
    def base(self):
        return self.workspace / ".provost" / "handoffs" / "v1"


def locations(root):
    """Legacy stores of a workspace: its own top-level first, then its main checkout (deduped)."""
    ws = as_workspace(root)
    found, seen = [], set()
    for directory in (ws.root, ws.main):
        key = workspace_key(directory)
        if key not in seen:
            seen.add(key)
            found.append(Location(Path(directory), key))
    return found


def present(root):
    return any(loc.base.is_dir() for loc in locations(root))


def _session_dir(loc, session):
    if not isinstance(session, str) or not session or len(session) > 256:
        raise StoreError("INVALID_SESSION")
    return loc.base / "sessions" / digest(session)


def _read(loc, path, session, kind):
    guard(loc.workspace, path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StoreError("CORRUPT_RECORD") from exc
    if (not isinstance(data, dict) or data.get("schema_version") != 1
            or data.get("workspace_id") != loc.key
            or data.get("session_id") != session or data.get("kind") != kind):
        raise StoreError("RECORD_IDENTITY_MISMATCH")
    ident = "revision" if kind == "record" else "snapshot_id"
    if data.get(ident) != path.stem:
        raise StoreError("RECORD_IDENTITY_MISMATCH")
    body = {k: v for k, v in data.items() if k != "checksum"}
    if data.get("checksum") != hashlib.sha256(encoded(body)).hexdigest():
        raise StoreError("CORRUPT_RECORD")
    validate_payload(data.get("payload"))
    return data


def _head(loc, session, kind):
    directory = _session_dir(loc, session)
    collection = directory / ("records" if kind == "record" else "snapshots")
    guard(loc.workspace, collection)
    if not collection.exists():
        return None
    id_key = "revision" if kind == "record" else "snapshot_id"
    parent_key = "parent_record" if kind == "record" else "parent_snapshot"
    objects = {}
    for path in collection.glob("*.json"):
        if ID.fullmatch(path.stem):
            objects[path.stem] = _read(loc, path, session, kind)
    if not objects:
        return None
    parents = {obj.get(parent_key) for obj in objects.values() if obj.get(parent_key)}
    if parents - objects.keys():
        raise StoreError("INCOMPLETE_CHAIN")
    heads = set(objects) - parents
    if len(heads) != 1:
        raise StoreError("AMBIGUOUS_CHAIN")
    head = objects[heads.pop()]
    visited, node = set(), head
    while node:
        if node[id_key] in visited:
            raise StoreError("AMBIGUOUS_CHAIN")
        visited.add(node[id_key])
        node = objects.get(node.get(parent_key))
    if len(visited) != len(objects):
        raise StoreError("AMBIGUOUS_CHAIN")
    return head


def checkpoint(root, session):
    """(checkpoint, path) of the session's newest legacy record, else newest snapshot, else None.

    The worktree's own store is consulted before the main checkout's. A legacy store
    that fails validation raises StoreError; it is never skipped in favour of the next.
    """
    for loc in locations(root):
        data = _head(loc, session, "record") or _head(loc, session, "snapshot")
        if data is not None:
            kind, ident = ("records", data["revision"]) if data["kind"] == "record" else ("snapshots", data["snapshot_id"])
            return data, _session_dir(loc, session) / kind / (ident + ".json")
    return None


def _snapshots(loc, snapshot_id=None):
    """Yield (path, session, data, error_code) for every snapshot file of one legacy store."""
    base = loc.base / "sessions"
    guard(loc.workspace, base)
    if not base.exists():
        return
    for folder in sorted(base.iterdir()):
        guard(loc.workspace, folder)
        if not folder.is_dir() or not ID.fullmatch(folder.name):
            continue
        collection = folder / "snapshots"
        guard(loc.workspace, collection)
        for path in collection.glob(snapshot_id + ".json" if snapshot_id else "*.json"):
            guard(loc.workspace, path)
            if not ID.fullmatch(path.stem):
                continue
            session = None
            try:
                session = json.loads(path.read_text(encoding="utf-8")).get("session_id")
                if not isinstance(session, str) or digest(session) != folder.name:
                    raise StoreError("RECORD_IDENTITY_MISMATCH")
                yield path, session, _read(loc, path, session, "snapshot"), None
            except (StoreError, OSError, UnicodeError, json.JSONDecodeError, AttributeError) as exc:
                yield path, session, None, str(exc) if isinstance(exc, StoreError) else "CORRUPT_RECORD"


def list_snapshots(root):
    """Same entries as store.list_snapshots, from the legacy stores, each marked legacy."""
    result = []
    for loc in locations(root):
        for path, _session, data, error in _snapshots(loc):
            if error:
                result.append({"snapshot_id": path.stem, "error": error, "legacy": True})
                continue
            result.append({k: data[k] for k in ("snapshot_id", "session_id", "captured_at", "parent_snapshot")}
                          | {"work_id": data["payload"]["work_id"], "phase": data["payload"]["phase"],
                             "legacy": True})
    return result


def resume(root, session, snapshot_id):
    """store.resume, falling back to a legacy snapshot that the new store does not have."""
    root = as_workspace(root)
    try:
        return store_resume(root, session, snapshot_id)
    except StoreError as exc:
        if str(exc) != "SNAPSHOT_NOT_FOUND":
            raise
    for loc in locations(root):
        for _path, original, data, error in _snapshots(loc, snapshot_id):
            if error:
                raise StoreError(error)
            if original == session:
                raise StoreError("SAME_SESSION_ADOPTION")
            return adopt(root, session, data, snapshot_id)
    raise StoreError("SNAPSHOT_NOT_FOUND")
