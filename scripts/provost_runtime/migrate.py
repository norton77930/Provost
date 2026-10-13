"""One-time import of decisions and evidence from schema 1 cards into the decision logs.

Sources are the repo's valid schema 1 records and snapshots (every session dir, including
`.pruning-` leftovers) and its valid archive files. They are only read, never modified.
A dry run writes nothing; apply backs up `logs/` first, then appends under each work's lock.

Per work_id, sources are read in time order (a record's or archive file's `recorded_at`, a
snapshot's `captured_at`; ties by kind, then path):
- a decision is imported once per exact text, at its first occurrence (its time, session
  and card revision, or snapshot id, are kept);
- evidence with the same claim forms one chain: the first state is an entry, each later
  different state is a new entry whose `updates` names the previous one, and a state equal
  to the previous one adds nothing.

Idempotency: an imported decision is identified by its text, an imported evidence entry by
(record, claim, state), each among the work's entries carrying `migrated: true`; candidates
already present are not imported again, and later chain links point at the logged ids.
"""
import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import handoff_home
from .store import (CHAINS, ID, LOG_ENTRY_ID, LOG_KINDS, LOG_SCHEMA, STAGING, StoreError, _archive_path, _base,
                    _log_lock, _log_paths, _parse_log, _read, _valid_decision, _valid_evidence, _when, as_workspace,
                    digest, encoded, guard, read_log, storage_ready, validate_payload)

SOURCE_RANK = {"record": 0, "snapshot": 1, "archive": 2}
BAD_SOURCE = (StoreError, OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError)


@dataclass(frozen=True)
class Source:
    when: datetime
    at: str        # the stored time string, kept as the entries' `at`
    rank: int
    file: str      # relative to the repo dir
    session: str
    record: str    # card revision, or snapshot id for a snapshot
    payload: dict


def _reason(exc):
    return str(exc) if isinstance(exc, StoreError) else "CORRUPT_RECORD"


def _session_source(ws, home, path, name, kind, rel):
    """The schema 1 source of a record or snapshot file (None for schema 2); StoreError when invalid."""
    guard(home, path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    session = raw.get("session_id") if isinstance(raw, dict) else None
    if not isinstance(session, str) or digest(session) != name:
        raise StoreError("RECORD_IDENTITY_MISMATCH")
    data = _read(ws, path, session, kind)
    if data["schema_version"] != 1:
        return None
    stamp = data.get("recorded_at" if kind == "record" else "captured_at")
    return Source(_when(stamp), stamp, SOURCE_RANK[kind], rel, session,
                  data["revision"] if kind == "record" else data["snapshot_id"], data["payload"])


def _archive_source(ws, home, path, rel):
    """The schema 1 source of an archive file (a sealed record named after its work_id)."""
    guard(home, path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise StoreError("CORRUPT_RECORD")
    body = {k: v for k, v in raw.items() if k != "checksum"}
    version = raw.get("schema_version")
    if (version not in (1, 2) or isinstance(version, bool) or raw.get("kind") != "record"
            or not isinstance(raw.get("session_id"), str) or not raw["session_id"]
            or not isinstance(raw.get("revision"), str) or not ID.fullmatch(raw["revision"])
            or raw.get("checksum") != hashlib.sha256(encoded(body)).hexdigest()):
        raise StoreError("CORRUPT_RECORD")
    if raw.get("workspace_id") != ws.repo_key:
        raise StoreError("FOREIGN_REPO")
    validate_payload(raw.get("payload"), version)
    if _archive_path(path.parent, raw["payload"]["work_id"]).name != path.name:
        raise StoreError("RECORD_IDENTITY_MISMATCH")
    if version != 1:
        return None
    return Source(_when(raw.get("recorded_at")), raw["recorded_at"], SOURCE_RANK["archive"], rel,
                  raw["session_id"], raw["revision"], raw["payload"])


def sources(ws):
    """(schema 1 sources in time order, skipped [{file, reason}]) of the repo. Read-only."""
    home, base = handoff_home(), _base(ws)
    found, skipped = [], []

    def listing(rel, directory):
        try:
            guard(home, directory)
            return sorted(directory.glob("*.json")) if directory.is_dir() else []
        except BAD_SOURCE as exc:
            skipped.append({"file": rel, "reason": _reason(exc)})
            return []

    sessions = base / "sessions"
    try:
        guard(home, sessions)
        folders = sorted(sessions.iterdir()) if sessions.is_dir() else []
    except BAD_SOURCE as exc:
        skipped.append({"file": "sessions", "reason": _reason(exc)})
        folders = []
    for folder in folders:
        name = folder.name[len(".pruning-"):] if STAGING.fullmatch(folder.name) else folder.name
        if not ID.fullmatch(name):
            continue
        for kind, collection in CHAINS.items():
            rel_dir = "sessions/%s/%s" % (folder.name, collection)
            for path in listing(rel_dir, folder / collection):
                if not ID.fullmatch(path.stem):
                    continue
                rel = rel_dir + "/" + path.name
                try:
                    source = _session_source(ws, home, path, name, kind, rel)
                except BAD_SOURCE as exc:
                    skipped.append({"file": rel, "reason": _reason(exc)})
                    continue
                if source is not None:
                    found.append(source)
    for path in listing("archive", base / "archive"):
        rel = "archive/" + path.name
        try:
            source = _archive_source(ws, home, path, rel)
        except BAD_SOURCE as exc:
            skipped.append({"file": rel, "reason": _reason(exc)})
            continue
        if source is not None:
            found.append(source)
    found.sort(key=lambda s: (s.when, s.rank, s.file))
    return found, skipped


def plan(found, skipped):
    """{work_id: {"sources": n, "items": [candidate]}} after dedup and chaining.

    A candidate is {kind, key, prev, entry}: entry holds every stored field except id and
    updates; prev is the key of the candidate an evidence entry updates (None for the first).
    Items the log cannot hold (empty text or claim) are added to skipped.
    """
    works = {}
    for source in found:
        payload = source.payload
        if not payload["decisions"] and not payload["evidence"]:
            continue
        work = works.setdefault(payload["work_id"], {"sources": 0, "items": [], "texts": set(), "claims": {}})
        work["sources"] += 1
        base = {"at": source.at, "session": source.session, "record": source.record, "migrated": True}
        for text in payload["decisions"]:
            if not _valid_decision({"text": text}):
                skipped.append({"file": source.file, "reason": "INVALID_DECISION"})
                continue
            if text in work["texts"]:
                continue
            work["texts"].add(text)
            work["items"].append({"kind": "decision", "key": ("decision", text), "prev": None,
                                  "entry": dict(base, kind="decision", text=text)})
        for item in payload["evidence"]:
            fields = {"claim": item["claim"], "state": item["state"], "ref": item["ref"]}
            if not _valid_evidence(fields):
                skipped.append({"file": source.file, "reason": "INVALID_EVIDENCE"})
                continue
            prev = work["claims"].get(item["claim"])
            if prev is not None and prev[3] == item["state"]:
                continue
            key = ("evidence", source.record, item["claim"], item["state"])
            work["claims"][item["claim"]] = key
            work["items"].append({"kind": "evidence", "key": key, "prev": prev,
                                  "entry": dict(base, kind="evidence", **fields)})
    return {work: {"sources": data["sources"], "items": data["items"]} for work, data in works.items()}


def _migrated_keys(logged):
    """{candidate key: id} of the log's migrated entries."""
    keys = {}
    for entry in logged:
        if entry.get("migrated") is not True:
            continue
        key = (("decision", entry["text"]) if entry["kind"] == "decision"
               else ("evidence", entry.get("record"), entry["claim"], entry["state"]))
        keys.setdefault(key, entry["id"])
    return keys


def new_entries(logged, items):
    """The entries of items not yet in the log, with ids continuing after the log's highest ones."""
    known = _migrated_keys(logged)
    last = {"D": 0, "E": 0}
    for entry in logged:
        match = LOG_ENTRY_ID.fullmatch(entry["id"])
        last[match[1]] = int(match[2])
    new = []
    for item in items:
        if item["key"] in known:
            continue
        letter = LOG_KINDS[item["kind"]]
        last[letter] += 1
        entry = {"id": letter + str(last[letter]), **item["entry"]}
        if item["prev"] is not None:
            entry["updates"] = known[item["prev"]]
        known[item["key"]] = entry["id"]
        new.append(entry)
    return new


def _count(item, new):
    item.update(decisions=sum(e["kind"] == "decision" for e in new),
                evidence=sum(e["kind"] == "evidence" for e in new))


def _backup(ws, home):
    """Copy the repo's logs/*.jsonl into a new <repo dir>/backups/logs-<UTC time>/; returns its path."""
    logs = _base(ws) / "logs"
    target = _base(ws) / "backups" / ("logs-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    guard(home, logs)
    guard(home, target)
    try:
        target.mkdir(parents=True)
        for path in sorted(logs.glob("*.jsonl")) if logs.is_dir() else []:
            guard(home, path)
            guard(home, target / path.name)
            shutil.copyfile(path, target / path.name)
    except OSError as exc:
        raise StoreError("WRITE_FAILED") from exc
    return target


def _append(ws, home, work_id, items):
    """Under the work's log lock, append the entries of items not yet logged; returns them."""
    path, _ = _log_paths(ws, work_id)
    with _log_lock(ws, work_id):
        guard(home, path)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            raw = b""
        except OSError as exc:
            raise StoreError("WRITE_FAILED") from exc
        logged, end = _parse_log(ws, work_id, raw)
        new = new_entries(logged, items)
        if not new:
            return new
        lines = [] if end else [encoded({"kind": "log", "schema_version": LOG_SCHEMA,
                                          "workspace_id": ws.repo_key, "work_id": work_id})]
        data = b"".join(line + b"\n" for line in lines + [encoded(entry) for entry in new])
        _parse_log(ws, work_id, raw[:end] + data)  # never write what the log reader would refuse
        try:
            with path.open("r+b" if raw else "wb") as stream:
                stream.truncate(end)  # drop a torn append; committed lines are never touched
                stream.seek(end)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise StoreError("WRITE_FAILED") from exc
        return new


def migrate(root, *, apply=False):
    """Per-work import counts (dry run unless apply); see the module docstring for the rules."""
    ws = as_workspace(root)
    home = handoff_home()
    found, skipped = sources(ws)
    planned = plan(found, skipped)
    works = []
    for work_id in sorted(planned):
        item = {"work_id": work_id, "sources": planned[work_id]["sources"]}
        try:
            new = new_entries(read_log(ws, work_id), planned[work_id]["items"])
        except StoreError as exc:
            item["error"] = str(exc)
        else:
            _count(item, new)
            item["already_migrated"] = len(planned[work_id]["items"]) - len(new)
        works.append(item)
    result = {"applied": bool(apply), "backup": None, "works": works, "skipped": skipped}
    pending = [item for item in works if item.get("decisions") or item.get("evidence")]
    if not apply or not pending:
        return result
    storage_ready(ws)
    result["backup"] = str(_backup(ws, home))
    for item in pending:
        items = planned[item["work_id"]]["items"]
        try:
            new = _append(ws, home, item["work_id"], items)
        except StoreError as exc:
            for key in ("decisions", "evidence", "already_migrated"):
                item.pop(key)
            item["error"] = str(exc)
            continue
        _count(item, new)
        item["already_migrated"] = len(items) - len(new)
    return result
