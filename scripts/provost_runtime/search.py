"""Read-only keyword search over stored handoff decisions; no index, no model, no writes."""
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import handoff_home
from .store import (CHAINS, ID, REPO_DIR_CHARS, StoreError, Workspace, _archive_path, _read, _when, digest,
                    encoded, guard, validate_payload)

DEFAULT_LIMIT = 10
MAX_LIMIT = 50
CLIP = 160
REPO_DIR = re.compile(r"^[a-f0-9]{%d}$" % REPO_DIR_CHARS)
ARCHIVE_FILE = re.compile(r"^[a-f0-9]{32}\.json$")
# ref: <r|s>:<repo dir>:<session digest[:12]>:<record or snapshot id[:12]> or a:<repo dir>:<archive file stem>
REF = re.compile(r"^(?:([rs]):([a-f0-9]{16}):([a-f0-9]{12}):([a-f0-9]{12})|(a):([a-f0-9]{16}):([a-f0-9]{32}))$")
SECRET = re.compile(r"gh[pousr]_[A-Za-z0-9]{10,}|github_pat_[A-Za-z0-9_]{10,}|(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{16,}"
                    r"|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----")
SOURCE_RANK = {"record": 0, "snapshot": 1, "archive": 2}
SESSION_KINDS = (("record", "record", "r"), ("snapshot", "snapshot", "s"))
BAD_FILE = (StoreError, OSError, ValueError, TypeError, KeyError, AttributeError)


@dataclass(frozen=True)
class Entry:
    data: dict
    source: str
    ref: str
    when: datetime


def redact(value):
    if isinstance(value, str):
        return SECRET.sub("[REDACTED]", value)
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    return value


def _repos_dir():
    return handoff_home() / "v2" / "repos"


def _foreign_ws(repo, raw):
    """A Workspace carrying the repo_key a stored file claims, which must belong to repo dir `repo`."""
    key = raw.get("workspace_id") if isinstance(raw, dict) else None
    if not isinstance(key, str) or not ID.fullmatch(key) or key[:REPO_DIR_CHARS] != repo:
        raise StoreError("RECORD_IDENTITY_MISMATCH")
    return Workspace(Path("."), Path("."), key)


def _session_file(home, repo, folder, path, kind):
    """Verified record or snapshot at path (same checks the store applies on read)."""
    guard(home, path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    session = raw.get("session_id") if isinstance(raw, dict) else None
    if not isinstance(session, str) or digest(session) != folder.name:
        raise StoreError("RECORD_IDENTITY_MISMATCH")
    return _read(_foreign_ws(repo, raw), path, session, kind)


def _archive_file(home, repo, path):
    """Verified archive record: a sealed record named after sha256(work_id)[:32]."""
    guard(home, path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    _foreign_ws(repo, raw)
    body = {k: v for k, v in raw.items() if k != "checksum"}
    if (raw.get("schema_version") != 1 or raw.get("kind") != "record" or not isinstance(raw.get("session_id"), str)
            or raw.get("checksum") != hashlib.sha256(encoded(body)).hexdigest()):
        raise StoreError("CORRUPT_RECORD")
    validate_payload(raw.get("payload"))
    if _archive_path(path.parent, raw["payload"]["work_id"]).name != path.name:
        raise StoreError("RECORD_IDENTITY_MISMATCH")
    return raw


def _session_entries(home, repo, folder, skipped):
    """The session's newest valid record plus all of its valid snapshots."""
    newest, entries = None, []
    for kind, source, letter in SESSION_KINDS:
        collection = folder / CHAINS[kind]
        try:
            guard(home, collection)
            if not collection.is_dir():
                continue
            paths = sorted(collection.glob("*.json"))
        except BAD_FILE:
            skipped[0] += 1
            continue
        for path in paths:
            if not ID.fullmatch(path.stem):
                continue
            try:
                data = _session_file(home, repo, folder, path, kind)
                when = _when(data.get("recorded_at"))
            except BAD_FILE:
                skipped[0] += 1
                continue
            entry = Entry(data, source, "%s:%s:%s:%s" % (letter, repo, folder.name[:12], path.stem[:12]), when)
            if kind == "snapshot":
                entries.append(entry)
            elif newest is None or (when, path.stem) > (newest[0].when, newest[1]):
                newest = (entry, path.stem)
    return ([newest[0]] if newest else []) + entries


def _repo_entries(home, repo_dir, skipped):
    repo = repo_dir.name
    entries = []
    sessions = repo_dir / "sessions"
    try:
        guard(home, sessions)
        folders = sorted(sessions.iterdir()) if sessions.is_dir() else []
    except BAD_FILE:
        skipped[0] += 1
        folders = []
    for folder in folders:
        if not ID.fullmatch(folder.name):
            continue
        try:
            guard(home, folder)
            if not folder.is_dir():
                continue
        except BAD_FILE:
            skipped[0] += 1
            continue
        entries += _session_entries(home, repo, folder, skipped)
    archive = repo_dir / "archive"
    try:
        guard(home, archive)
        paths = sorted(archive.glob("*.json")) if archive.is_dir() else []
    except BAD_FILE:
        skipped[0] += 1
        paths = []
    for path in paths:
        try:
            if not ARCHIVE_FILE.fullmatch(path.name):
                raise StoreError("RECORD_IDENTITY_MISMATCH")
            data = _archive_file(home, repo, path)
            entries.append(Entry(data, "archive", "a:%s:%s" % (repo, path.stem), _when(data.get("recorded_at"))))
        except BAD_FILE:
            skipped[0] += 1
    return entries


def _repo_dirs(home, ws, all_repos, skipped):
    repos = _repos_dir()
    if not all_repos:
        candidates = [repos / ws.repo_key[:REPO_DIR_CHARS]]
    else:
        try:
            guard(home, repos)
            candidates = sorted(repos.iterdir()) if repos.is_dir() else []
        except BAD_FILE:
            skipped[0] += 1
            candidates = []
    found = []
    for path in candidates:
        if not REPO_DIR.fullmatch(path.name):
            continue
        try:
            guard(home, path)
            if path.is_dir():
                found.append(path)
        except BAD_FILE:
            skipped[0] += 1
    return found


def _items(payload):
    for text in payload["decisions"]:
        yield "decision", text
    for text in payload["blockers"]:
        yield "blocker", text
    yield "next", payload["next"]
    for item in payload["evidence"]:
        yield "evidence", item["claim"]


def _since(text):
    try:
        return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError) as exc:
        raise StoreError("INVALID_SINCE") from exc


def _limit(text):
    if text is None:
        return DEFAULT_LIMIT
    try:
        value = int(text)
    except (TypeError, ValueError) as exc:
        raise StoreError("INVALID_LIMIT") from exc
    if not 1 <= value <= MAX_LIMIT:
        raise StoreError("INVALID_LIMIT")
    return value


def find(ws, query, *, all_repos=False, since=None, limit=None):
    """Matching decisions, blockers, next steps and evidence claims across the scanned repos."""
    terms = list(dict.fromkeys((query or "").lower().split()))
    if not terms:
        raise StoreError("QUERY_REQUIRED")
    floor = _since(since) if since is not None else None
    cap = _limit(limit)
    home, skipped = handoff_home(), [0]
    repos = _repo_dirs(home, ws, all_repos, skipped)
    hits = []
    for repo_dir in repos:
        for entry in _repo_entries(home, repo_dir, skipped):
            if floor is not None and entry.when < floor:
                continue
            payload = entry.data["payload"]
            context = (payload["work_id"] + " " + payload["phase"]).lower()
            for field, text in _items(payload):
                lowered = text.lower()
                if all(term in lowered or term in context for term in terms):
                    hits.append((entry, field, text, sum(lowered.count(term) for term in terms)))
    # Newest first (a record beats a snapshot or archive copy of the same moment), then keep
    # the first of each identical (work_id, field, text); the stable sort by score keeps recency on ties.
    hits.sort(key=lambda h: (-h[0].when.timestamp(), SOURCE_RANK[h[0].source], h[0].ref, h[1], h[2]))
    unique, seen = [], set()
    for hit in hits:
        key = (hit[0].data["payload"]["work_id"], hit[1], hit[2])
        if key not in seen:
            seen.add(key)
            unique.append(hit)
    unique.sort(key=lambda h: -h[3])
    results = [{"recorded_at": entry.data["recorded_at"], "worktree_path": entry.data.get("worktree_path"),
                "work_id": entry.data["payload"]["work_id"], "phase": entry.data["payload"]["phase"],
                "field": field, "text": redact(text)[:CLIP], "source": entry.source, "ref": entry.ref}
               for entry, field, text, _ in unique[:cap]]
    return redact({"query": query, "repos_scanned": len(repos), "results": results,
                   "truncated": max(0, len(unique) - cap), "skipped": skipped[0]})


def show(ws, ref, *, all_repos=False):
    """The full payload of the record a search result's ref names."""
    match = REF.fullmatch(ref) if isinstance(ref, str) else None
    if not match:
        raise StoreError("INVALID_REF")
    home = handoff_home()
    letter, repo, session_prefix, stem_prefix = match.group(1, 2, 3, 4)
    if match.group(5):
        letter, repo, stem = match.group(5, 6, 7)
    if not all_repos and repo != ws.repo_key[:REPO_DIR_CHARS]:
        raise StoreError("REF_NOT_FOUND")
    repo_dir = _repos_dir() / repo
    guard(home, repo_dir)
    if letter == "a":
        path = repo_dir / "archive" / (stem + ".json")
        guard(home, path)
        if not path.is_file():
            raise StoreError("REF_NOT_FOUND")
        found, data, source = path, None, "archive"
    else:
        kind, source = ("record", "record") if letter == "r" else ("snapshot", "snapshot")
        sessions = repo_dir / "sessions"
        guard(home, sessions)
        matches = []
        if sessions.is_dir():
            for folder in sessions.iterdir():
                if ID.fullmatch(folder.name) and folder.name.startswith(session_prefix):
                    collection = folder / CHAINS[kind]
                    guard(home, collection)
                    if collection.is_dir():
                        matches += [(folder, p) for p in collection.glob("*.json")
                                    if ID.fullmatch(p.stem) and p.stem.startswith(stem_prefix)]
        if len(matches) > 1:
            raise StoreError("REF_AMBIGUOUS")
        if not matches:
            raise StoreError("REF_NOT_FOUND")
        folder, found = matches[0]
        data = None
    try:
        data = (_archive_file(home, repo, found) if letter == "a"
                else _session_file(home, repo, folder, found, kind))
        _when(data.get("recorded_at"))
    except BAD_FILE as exc:
        raise StoreError(str(exc) if isinstance(exc, StoreError) else "CORRUPT_RECORD") from exc
    payload = data["payload"]
    return redact({"ref": ref, "source": source, "recorded_at": data["recorded_at"],
                   "worktree_path": data.get("worktree_path"), "work_id": payload["work_id"],
                   "phase": payload["phase"], "payload": payload})
