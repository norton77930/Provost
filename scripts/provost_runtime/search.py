"""Read-only keyword search over stored handoff decisions; no index, no model, no writes."""
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import handoff_home
from .store import (CHAINS, ID, REPO_DIR_CHARS, StoreError, Workspace, _archive_path, _parse_log,
                    _read, _when, digest, encoded, guard, newest_entries, validate_payload)

DEFAULT_LIMIT = 10
MAX_LIMIT = 50
CLIP = 160
REPO_DIR = re.compile(r"^[a-f0-9]{%d}$" % REPO_DIR_CHARS)
ARCHIVE_FILE = re.compile(r"^[a-f0-9]{32}\.json$")
# ref: <r|s>:<repo dir>:<session digest[:12]>:<record or snapshot id[:12]> or a:<repo dir>:<archive file stem>
REF = re.compile(r"^(?:([rs]):([a-f0-9]{16}):([a-f0-9]{12}):([a-f0-9]{12})|(a):([a-f0-9]{16}):([a-f0-9]{32}))$")
SECRET = re.compile(r"gh[pousr]_[A-Za-z0-9]{10,}|github_pat_[A-Za-z0-9_]{10,}|(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{16,}"
                    r"|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----")
SOURCE_RANK = {"log": -1, "record": 0, "snapshot": 1, "archive": 2}
LOG_FILE = re.compile(r"^[a-f0-9]{64}\.jsonl$")
# log ref: [@<repo dir>:]<work_id>:<entry id>; the repo prefix is only given for repos other than the workspace's.
LOG_REF = re.compile(r"^(?:@([a-f0-9]{%d}):)?(.+):([DE][1-9][0-9]*)$" % REPO_DIR_CHARS, re.DOTALL)
KINDS = ("decision", "evidence")
NEIGHBOURS = 2  # log entries shown on each side of a --show entry
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
    if (raw.get("schema_version") not in (1, 2) or isinstance(raw.get("schema_version"), bool)
            or raw.get("kind") != "record" or not isinstance(raw.get("session_id"), str)
            or raw.get("checksum") != hashlib.sha256(encoded(body)).hexdigest()):
        raise StoreError("CORRUPT_RECORD")
    validate_payload(raw.get("payload"), raw["schema_version"])
    if _archive_path(path.parent, raw["payload"]["work_id"]).name != path.name:
        raise StoreError("RECORD_IDENTITY_MISMATCH")
    return raw


def _session_entries(home, repo, folder, skipped):
    """The session's newest valid record per work_id plus all of its valid snapshots."""
    newest, entries = {}, []
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
                continue
            work, stamp = data["payload"]["work_id"], (when, path.stem)
            if work not in newest or stamp > newest[work][0]:
                newest[work] = (stamp, entry)
    return [entry for _, entry in sorted(newest.values(), key=lambda item: item[0])] + entries


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


class DecisionLog:
    """A work's verified log entries and the links between them (read-only views).

    A link is an entry's `supersedes` (decisions) or `updates` (evidence) target. Logs written
    before the record checks may hold dangling or cross-kind targets: those are tolerated, a
    dangling one is listed, a cross-kind one is followed like any other.
    """

    def __init__(self, repo, work_id, entries):
        self.repo, self.work_id = repo, work_id
        self.by_id = {entry["id"]: entry for entry in entries}
        self.order = newest_entries(entries, len(entries))  # oldest first: by time, then log order
        self.position = {entry["id"]: i for i, entry in enumerate(self.order)}
        self.superseded_by, self.updated_by, self.links, self.claims = {}, {}, {}, {}
        for entry in entries:  # log order: with several pointers at one target the last one wins
            if entry["kind"] == "evidence":
                self.claims.setdefault(entry["claim"], []).append(entry["id"])
            target = entry.get("supersedes") if entry["kind"] == "decision" else entry.get("updates")
            if target is None:
                continue
            self.links[entry["id"]] = target
            (self.superseded_by if entry["kind"] == "decision" else self.updated_by)[target] = entry["id"]

    def latest(self, ident):
        """The current entry of evidence `ident`: the newest in time order of the end of its update chain
        and every evidence entry of the work with the same claim text (a migrated chain and a live one
        for one claim are never linked, so the claim text joins them)."""
        seen = {ident}
        while ident in self.updated_by and self.updated_by[ident] not in seen:
            ident = self.updated_by[ident]
            seen.add(ident)
        same = self.claims.get(self.by_id[ident].get("claim"), ())
        return self.by_id[max([ident, *same], key=self.position.__getitem__)]

    def chain(self, ident):
        """(entries connected to `ident` through supersede/update links in either direction, in time
        order; the link targets of those entries that are not in the log)."""
        adjacent = {}
        for source, target in self.links.items():
            if target in self.by_id:
                adjacent.setdefault(source, set()).add(target)
                adjacent.setdefault(target, set()).add(source)
        seen, todo = {ident}, [ident]
        while todo:
            for other in adjacent.get(todo.pop(), ()):
                if other not in seen:
                    seen.add(other)
                    todo.append(other)
        dangling = sorted({self.links[i] for i in seen if i in self.links and self.links[i] not in self.by_id})
        return [self.by_id[i] for i in sorted(seen, key=self.position.__getitem__)], dangling

    def neighbours(self, ident):
        """(up to NEIGHBOURS entries before, up to NEIGHBOURS after) `ident` in time order."""
        at = self.position[ident]
        return self.order[max(0, at - NEIGHBOURS):at], self.order[at + 1:at + 1 + NEIGHBOURS]

    def state(self, entry):
        """Where an entry stands in its chain: its replacement and, for evidence, the chain's latest state."""
        found = {"superseded_by": self.superseded_by.get(entry["id"])}
        if entry["kind"] == "evidence":
            latest = self.latest(entry["id"])
            found.update(state=latest["state"], latest=latest["id"])
        return found


def _read_log(home, repo, own_key, path):
    """(work_id, entries) of the log file at path: header first (repo, work_id whose digest names the
    file), then the entries as the store parses them. A torn tail is ignored; anything else bad raises."""
    guard(home, path)
    raw = path.read_bytes()
    end = raw.find(b"\n")
    if end < 0:
        return None, []  # nothing committed yet
    try:
        header = json.loads(raw[:end].decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise StoreError("CORRUPT_LOG") from exc
    key, work = (header.get("workspace_id"), header.get("work_id")) if isinstance(header, dict) else (None, None)
    if not isinstance(key, str) or not ID.fullmatch(key) or not isinstance(work, str) or not work:
        raise StoreError("CORRUPT_LOG")
    if key[:REPO_DIR_CHARS] != repo or (repo == own_key[:REPO_DIR_CHARS] and key != own_key):
        raise StoreError("FOREIGN_REPO")
    if digest(work) + ".jsonl" != path.name:
        raise StoreError("CORRUPT_LOG")
    return work, _parse_log(Workspace(Path("."), Path("."), key), work, raw)[0]


def _repo_logs(home, ws, repo_dir, skipped, work=None):
    """The verified decision logs of a repo dir (only the one of `work` when given); unreadable,
    foreign or mismatched ones are counted in skipped."""
    logs = repo_dir / "logs"
    try:
        guard(home, logs)
        if work is not None:
            named = logs / (digest(work) + ".jsonl")
            guard(home, named)
            paths = [named] if named.is_file() else []
        else:
            paths = sorted(logs.glob("*.jsonl")) if logs.is_dir() else []
    except BAD_FILE:
        skipped[0] += 1
        return []
    found = []
    for path in paths:
        try:
            if not LOG_FILE.fullmatch(path.name):
                raise StoreError("CORRUPT_LOG")
            work, entries = _read_log(home, repo_dir.name, ws.repo_key, path)
        except BAD_FILE:
            skipped[0] += 1
            continue
        if entries:
            found.append(DecisionLog(repo_dir.name, work, entries))
    return found


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
    """Searchable items of a card; schema 2 cards hold no decisions or evidence (those are in the log)."""
    for text in payload.get("decisions", ()):
        yield "decision", text
    for text in payload["blockers"]:
        yield "blocker", text
    yield "next", payload["next"]
    for item in payload.get("evidence", ()):
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


def _log_ref(ws, log, ident):
    """Ref of a log entry: <work_id>:<entry id>, with an @<repo dir>: prefix outside the workspace's repo."""
    outside = log.repo != ws.repo_key[:REPO_DIR_CHARS] or log.work_id.startswith("@")  # "@..." must not be read as a prefix
    return ("@%s:" % log.repo if outside else "") + log.work_id + ":" + ident


def find(ws, query, *, all_repos=False, since=None, limit=None, work=None, kind=None):
    """Matching log entries, decisions, blockers, next steps and evidence claims across the scanned repos.

    work keeps one work_id; kind (decision or evidence) keeps decision/evidence items only.
    """
    terms = list(dict.fromkeys((query or "").lower().split()))
    if not terms:
        raise StoreError("QUERY_REQUIRED")
    if work == "":
        raise StoreError("INVALID_ARGUMENTS")
    if kind is not None and kind not in KINDS:
        raise StoreError("INVALID_KIND")
    floor = _since(since) if since is not None else None
    cap = _limit(limit)
    home, skipped = handoff_home(), [0]
    repos = _repo_dirs(home, ws, all_repos, skipped)

    def score(text, context):
        lowered = text.lower()
        if all(term in lowered or term in context for term in terms):
            return sum(lowered.count(term) for term in terms)
        return None

    # Candidates: (when, source rank, ref, field, text, score, dedupe key or None, result without text/ref).
    hits, logged = [], set()
    for repo_dir in repos:
        for log in _repo_logs(home, ws, repo_dir, skipped, work):
            for item in log.order:
                when = _when(item["at"])
                text = item["text"] if item["kind"] == "decision" else item["claim"]
                points = score(text, log.work_id.lower())
                if (kind not in (None, item["kind"]) or (floor is not None and when < floor)
                        or points is None):
                    continue
                logged.add((log.work_id, item["kind"], text))
                result = {"recorded_at": item["at"], "work_id": log.work_id, "id": item["id"], "kind": item["kind"],
                          "date": when.astimezone(timezone.utc).date().isoformat(), "field": item["kind"], "source": "log",
                          **log.state(item)}
                hits.append((when, SOURCE_RANK["log"], _log_ref(ws, log, item["id"]), item["kind"], text, points,
                             None, result))
        for entry in _repo_entries(home, repo_dir, skipped):
            payload = entry.data["payload"]
            if (floor is not None and entry.when < floor) or (work is not None and payload["work_id"] != work):
                continue
            context = (payload["work_id"] + " " + payload["phase"]).lower()
            for field, text in _items(payload):
                points = score(text, context)
                if points is not None and kind in (None, field):
                    result = {"recorded_at": entry.data["recorded_at"],
                              "worktree_path": entry.data.get("worktree_path"), "work_id": payload["work_id"],
                              "phase": payload["phase"], "field": field, "source": entry.source}
                    hits.append((entry.when, SOURCE_RANK[entry.source], entry.ref, field, text, points,
                                 (payload["work_id"], field, text), result))
    # Newest first (a log entry or record beats a snapshot or archive copy of the same moment), then keep
    # the first of each identical (work_id, field, text) of the older stores: a log entry already holds
    # what a migrated record item said. The stable sort by score keeps recency on ties.
    hits.sort(key=lambda h: (-h[0].timestamp(), h[1], h[2], h[3], h[4]))
    unique, seen = [], set()
    for hit in hits:
        key = hit[6]
        if key is not None and (key in seen or key in logged):
            continue
        seen.add(key)
        unique.append(hit)
    unique.sort(key=lambda h: -h[5])
    results = [{**result, "text": redact(text)[:CLIP], "ref": ref}
               for _, _, ref, _, text, _, _, result in unique[:cap]]
    return redact({"query": query, "repos_scanned": len(repos), "results": results,
                   "truncated": max(0, len(unique) - cap), "skipped": skipped[0]})


def _show_log(ws, ref, match, *, all_repos):
    """A log entry with its supersede/update chain and its time-order neighbours."""
    repo = match.group(1) or ws.repo_key[:REPO_DIR_CHARS]
    work, ident = match.group(2, 3)
    if repo != ws.repo_key[:REPO_DIR_CHARS] and not all_repos:
        raise StoreError("REF_NOT_FOUND")
    home = handoff_home()
    path = _repos_dir() / repo / "logs" / (digest(work) + ".jsonl")
    guard(home, path)
    if not path.is_file():
        raise StoreError("REF_NOT_FOUND")
    try:
        _, entries = _read_log(home, repo, ws.repo_key, path)
    except BAD_FILE as exc:
        raise StoreError(str(exc) if isinstance(exc, StoreError) else "CORRUPT_LOG") from exc
    log = DecisionLog(repo, work, entries)
    if ident not in log.by_id:
        raise StoreError("REF_NOT_FOUND")
    entry = log.by_id[ident]
    chain, dangling = log.chain(ident)
    before, after = log.neighbours(ident)
    return redact({"ref": ref, "source": "log", "work_id": work, "recorded_at": entry["at"], "entry": entry,
                   **log.state(entry), "chain": chain, "dangling": dangling, "before": before, "after": after})


def show(ws, ref, *, all_repos=False):
    """The full payload of the record a search result's ref names, or for a log entry ref the entry,
    its chain and its neighbours."""
    match = REF.fullmatch(ref) if isinstance(ref, str) else None
    if not match:
        logged = LOG_REF.fullmatch(ref) if isinstance(ref, str) else None
        if logged:
            return _show_log(ws, ref, logged, all_repos=all_repos)
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
