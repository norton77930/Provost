"""One-time import of decisions and evidence from schema 1 cards into the decision logs.

Sources are the repo's valid schema 1 records and snapshots (every session dir, including
`.pruning-` leftovers) and its valid archive files. They are only read, never modified.
A dry run writes nothing; apply appends under each work's log lock, after backing up
that work's log under the same lock.

A source stands for the card it holds: a snapshot or archive file carries its card's
`recorded_at` and `revision` (a snapshot is one source per card it holds), so copies of
one card share time and identity. Per work_id,
sources are read in time order (`recorded_at`; equal times: by parent_record descent,
then records before snapshots before archive files, then path):
- a decision is imported once per exact text, at its first occurrence (its time, session
  and card revision are kept);
- evidence with the same claim forms one chain: the first state is an entry, each later
  different state is a new entry whose `updates` names the previous one, and a state equal
  to the previous one adds nothing.

Idempotency: a work's candidates go out in one write, so a candidate whose card revision
already appears as `record` among the work's `migrated: true` entries was handled by an
earlier run and is not imported again. As a secondary guard, a decision whose text a
migrated decision already has is not imported again either. A chain link to an earlier
import resolves by position among that card's migrated entries, else by (record, claim,
state); MIGRATE_CHAIN_UNRESOLVED when neither finds it.
"""
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import handoff_home
from .store import (CHAINS, ID, LOG_ENTRY_ID, LOG_KINDS, LOG_SCHEMA, STAGING, StoreError, _archive_path, _base,
                    _log_lock, _log_paths, _parse_log, _read, _valid_decision, _valid_evidence, _when, as_workspace,
                    digest, encoded, guard, read_log, snapshot_cards, storage_ready, validate_payload)

SOURCE_RANK = {"record": 0, "snapshot": 1, "archive": 2}
BAD_SOURCE = (StoreError, OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError)


@dataclass(frozen=True)
class Source:
    when: datetime
    at: str         # the card's stored recorded_at, kept as the entries' `at`
    rank: int
    file: str       # relative to the repo dir
    session: str
    record: str     # the card's revision (a snapshot or archive file keeps its card's)
    parent: object  # the card's parent_record
    payload: dict


def _reason(exc):
    return str(exc) if isinstance(exc, StoreError) else "CORRUPT_RECORD"


def _card_source(data, kind, rel, session):
    """The Source of a validated card (or of a snapshot's or archive file's copy of one)."""
    if not isinstance(data.get("revision"), str) or not ID.fullmatch(data["revision"]):
        raise StoreError("CORRUPT_RECORD")
    return Source(_when(data.get("recorded_at")), data["recorded_at"], SOURCE_RANK[kind], rel, session,
                  data["revision"], data.get("parent_record"), data["payload"])


def _session_sources(ws, home, path, name, kind, rel):
    """The schema 1 sources of a record file or of every card a snapshot holds; StoreError when invalid."""
    guard(home, path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    session = raw.get("session_id") if isinstance(raw, dict) else None
    if not isinstance(session, str) or digest(session) != name:
        raise StoreError("RECORD_IDENTITY_MISMATCH")
    data = _read(ws, path, session, kind)
    held = snapshot_cards(data) if kind == "snapshot" else [data]
    return [_card_source(card, kind, rel, session) for card in held if card["schema_version"] == 1]


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
            or raw.get("checksum") != hashlib.sha256(encoded(body)).hexdigest()):
        raise StoreError("CORRUPT_RECORD")
    if raw.get("workspace_id") != ws.repo_key:
        raise StoreError("FOREIGN_REPO")
    validate_payload(raw.get("payload"), version)
    if _archive_path(path.parent, raw["payload"]["work_id"]).name != path.name:
        raise StoreError("RECORD_IDENTITY_MISMATCH")
    return _card_source(raw, "archive", rel, raw["session_id"]) if version == 1 else None


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
                    found += _session_sources(ws, home, path, name, kind, rel)
                except BAD_SOURCE as exc:
                    skipped.append({"file": rel, "reason": _reason(exc)})
    for path in listing("archive", base / "archive"):
        rel = "archive/" + path.name
        try:
            source = _archive_source(ws, home, path, rel)
        except BAD_SOURCE as exc:
            skipped.append({"file": rel, "reason": _reason(exc)})
            continue
        if source is not None:
            found.append(source)
    parents = {source.record: source.parent for source in found}

    def depth(revision):
        """How many of the card's ancestors (by parent_record) are among the sources."""
        seen = {revision}
        while parents.get(revision) in parents and parents[revision] not in seen:
            revision = parents[revision]
            seen.add(revision)
        return len(seen) - 1
    found.sort(key=lambda s: (s.when, depth(s.record), s.rank, s.file))
    return found, skipped


def plan(found, skipped):
    """{work_id: {"sources": n, "items": [candidate]}} after dedup and chaining.

    A candidate is {kind, prev, entry}: entry holds every stored field except id and
    updates; prev is the index (in the work's items) of the candidate an evidence entry
    updates, None for the first of its claim. Items the log cannot hold (empty text or
    claim) are added to skipped.
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
            work["items"].append({"kind": "decision", "prev": None, "entry": dict(base, kind="decision", text=text)})
        for item in payload["evidence"]:
            fields = {"claim": item["claim"], "state": item["state"], "ref": item["ref"]}
            if not _valid_evidence(fields):
                skipped.append({"file": source.file, "reason": "INVALID_EVIDENCE"})
                continue
            prev = work["claims"].get(item["claim"])
            if prev is not None and work["items"][prev]["entry"]["state"] == item["state"]:
                continue
            work["claims"][item["claim"]] = len(work["items"])
            work["items"].append({"kind": "evidence", "prev": prev, "entry": dict(base, kind="evidence", **fields)})
    return {work: {"sources": data["sources"], "items": data["items"]} for work, data in works.items()}


def _evidence_key(entry):
    return entry.get("record"), entry["claim"], entry["state"]


def new_entries(logged, items):
    """(entries of items not yet imported, count already imported); ids continue after the log's highest."""
    migrated = [entry for entry in logged if entry.get("migrated") is True]
    done, keys = {}, {}
    for entry in migrated:
        done.setdefault(entry.get("record"), []).append(entry)
        if entry["kind"] == "evidence":
            keys.setdefault(_evidence_key(entry), entry["id"])
    texts = {entry["text"] for entry in migrated if entry["kind"] == "decision"}
    ids, groups = {}, {}  # ids: candidate index -> logged id
    for index, item in enumerate(items):
        groups.setdefault(item["entry"]["record"], []).append(index)
    for record, indexes in groups.items():
        imported = done.get(record, [])
        if [entry["kind"] for entry in imported] == [items[i]["kind"] for i in indexes]:
            ids.update((i, entry["id"]) for i, entry in zip(indexes, imported))
    last = {"D": 0, "E": 0}
    for entry in logged:
        match = LOG_ENTRY_ID.fullmatch(entry["id"])
        last[match[1]] = int(match[2])
    new, already = [], 0
    for index, item in enumerate(items):
        entry = item["entry"]
        if entry["record"] in done or (item["kind"] == "decision" and entry["text"] in texts):
            already += 1
            continue
        letter = LOG_KINDS[item["kind"]]
        last[letter] += 1
        created = {"id": letter + str(last[letter]), **entry}
        if item["prev"] is not None:
            target = ids.get(item["prev"]) or keys.get(_evidence_key(items[item["prev"]]["entry"]))
            if target is None:
                raise StoreError("MIGRATE_CHAIN_UNRESOLVED")
            created["updates"] = target
        ids[index] = created["id"]
        new.append(created)
    return new, already


def _backup(home, target, path, data):
    """Write data (a log's committed bytes) to target/<log name>, fsynced; StoreError WRITE_FAILED."""
    copy = target / path.name
    try:
        guard(home, copy)
        target.mkdir(parents=True, exist_ok=True)
        guard(home, copy)
        with copy.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise StoreError("WRITE_FAILED") from exc


def _append(ws, home, work_id, items, backup):
    """Under the work's log lock, copy the log into dir backup, then append the entries of items
    not yet imported; returns (new entries, count already imported)."""
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
        new, already = new_entries(logged, items)
        if not new:
            return new, already
        lines = [] if end else [encoded({"kind": "log", "schema_version": LOG_SCHEMA,
                                          "workspace_id": ws.repo_key, "work_id": work_id})]
        data = b"".join(line + b"\n" for line in lines + [encoded(entry) for entry in new])
        _parse_log(ws, work_id, raw[:end] + data)  # never write what the log reader would refuse
        _backup(home, backup, path, raw[:end])  # an empty copy stands for "no log before migrate"
        try:
            with path.open("r+b" if raw else "wb") as stream:
                stream.truncate(end)  # drop a torn append; committed lines are never touched
                stream.seek(end)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise StoreError("WRITE_FAILED") from exc
        return new, already


def _count(item, new, already):
    item.update(decisions=sum(e["kind"] == "decision" for e in new),
                evidence=sum(e["kind"] == "evidence" for e in new), already_migrated=already)


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
            _count(item, *new_entries(read_log(ws, work_id), planned[work_id]["items"]))
        except StoreError as exc:
            item["error"] = str(exc)
        works.append(item)
    result = {"applied": bool(apply), "backup": None, "works": works, "skipped": skipped}
    pending = [item for item in works if item.get("decisions") or item.get("evidence")]
    if not apply or not pending:
        return result
    storage_ready(ws)
    backup = _base(ws) / "backups" / ("logs-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    try:
        for item in pending:
            try:
                new, already = _append(ws, home, item["work_id"], planned[item["work_id"]]["items"], backup)
            except StoreError as exc:
                for key in ("decisions", "evidence", "already_migrated"):
                    item.pop(key)
                item["error"] = str(exc)
                continue
            _count(item, new, already)
    finally:
        try:
            if backup.is_dir() and not any(backup.iterdir()):
                backup.rmdir()  # nothing was backed up: leave no empty backup dir behind
        except OSError:
            pass
    result["backup"] = str(backup) if backup.is_dir() else None
    return result
