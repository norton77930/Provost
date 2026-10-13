"""Small local checkpoint store; no model, network, or task scheduler."""
import copy
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import handoff_home, workspace_key

MAX_PAYLOAD = 32768
ID = re.compile(r"^[a-f0-9]{64}$")


class StoreError(ValueError):
    """str() is the error code; detail, when given, is guidance for the caller."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.detail = detail


@dataclass(frozen=True)
class Workspace:
    """A resolved workspace: its own top-level dir, the main checkout it belongs to
    (itself when not a linked worktree or not Git) and the repo-level store identity."""
    root: Path
    main: Path
    repo_key: str


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def locate(cwd):
    """Resolve cwd with one Git spawn. A main checkout and its worktrees share a repo_key:
    the hash of the main checkout (parent of the common dir). A non-Git dir keys on itself."""
    root = Path(cwd).resolve()
    if not root.is_dir():
        raise StoreError("WORKSPACE_MISSING")
    try:
        result = subprocess.run(["git", "-C", str(root), "rev-parse", "--path-format=absolute",
                                 "--show-toplevel", "--git-common-dir"],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
        # git < 2.31 lacks --path-format: rev-parse echoes the unknown flag or fails with
        # usage. Never fall back to non-Git keying for a Git checkout that cannot be keyed.
        lines = result.stdout.splitlines()
        if ((lines and lines[0] == "--path-format=absolute") or result.returncode == 129
                or "unknown option" in result.stderr or "usage:" in result.stderr):
            raise StoreError("GIT_UNSUPPORTED")
        if result.returncode == 0:
            if len(lines) != 2 or not all(lines):
                raise StoreError("REPO_KEY_UNRESOLVED")
            top, common = Path(lines[0]).resolve(), Path(lines[1]).resolve()
            # Bare-backed worktrees, submodules and separate git dirs have no main checkout to key on.
            if common.name != ".git" or not common.parent.is_dir():
                raise StoreError("REPO_KEY_UNRESOLVED")
            return Workspace(top, common.parent, workspace_key(common.parent))
        if "must be run in a work tree" in result.stderr:
            raise StoreError("REPO_KEY_UNRESOLVED")
        # An existing .git with an unreadable repository must not become non-Git.
        if (root / ".git").exists() or "dubious ownership" in result.stderr:
            raise StoreError("GIT_UNREADABLE")
    except FileNotFoundError as exc:
        if (root / ".git").exists():
            raise StoreError("GIT_UNAVAILABLE") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StoreError("GIT_UNREADABLE") from exc
    return Workspace(root, root, workspace_key(root))


def as_workspace(root):
    """A Workspace as given, or one resolved from a path (one Git spawn)."""
    return root if isinstance(root, Workspace) else locate(root)


# Windows MAX_PATH (260) cannot hold a 64-char repo dir next to the 64-char session
# and object names under ~/.claude/provost/handoffs. The dir uses a key prefix; the
# full repo_key is stored in every file and compared on read, so a prefix collision
# fails as an identity mismatch instead of mixing repositories.
REPO_DIR_CHARS = 16


def _base(ws):
    return handoff_home() / "v2" / "repos" / ws.repo_key[:REPO_DIR_CHARS]


def session_dir(root, session):
    if not isinstance(session, str) or not session or len(session) > 256:
        raise StoreError("INVALID_SESSION")
    return _base(as_workspace(root)) / "sessions" / digest(session)


def guard(root, path):
    root, path = Path(root).absolute(), Path(path).absolute()
    try:
        parts = path.relative_to(root).parts
    except ValueError as exc:
        raise StoreError("OUTSIDE_WORKSPACE") from exc
    if ".." in parts:  # relative_to is lexical: root/../x would pass
        raise StoreError("OUTSIDE_WORKSPACE")
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


_HOME_CHECKED = {}


def _home_outside_repo(home):
    """Error code when handoffs under the store home could be committed, else None.

    A home outside every Git work tree, or inside one that ignores it (a dotfiles
    repo such as ~/.claude), is accepted. One `git check-ignore` of the kind of path
    the store writes, <home>/v2/repos/<repo dir> (a dot-name probe would pass a repo
    ignoring only `.*`), run from the nearest existing ancestor so nothing is created
    before the verdict; cached per process.
    """
    key = os.path.normcase(str(home))
    if key not in _HOME_CHECKED:
        cwd = Path(home)
        while not cwd.is_dir() and cwd.parent != cwd:
            cwd = cwd.parent
        child = (Path(home) / "v2" / "repos" / ("0" * REPO_DIR_CHARS)).relative_to(cwd)
        try:
            result = subprocess.run(["git", "-C", str(cwd), "check-ignore", "-q", "--", child.as_posix()],
                                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
        except FileNotFoundError:
            result = None  # no Git, nothing can commit the store
        except (OSError, subprocess.TimeoutExpired):
            return "GIT_UNREADABLE"
        if result is None or result.returncode == 0:
            _HOME_CHECKED[key] = None
        elif result.returncode == 1:
            _HOME_CHECKED[key] = "STORE_INSIDE_REPO"
        elif "not a git repository" in result.stderr:
            _HOME_CHECKED[key] = None
        else:
            _HOME_CHECKED[key] = "GIT_UNREADABLE"
    return _HOME_CHECKED[key]


def storage_ready(root):
    """Writable-store check: no links under the store home, and the home outside every repo."""
    home = handoff_home()
    guard(home, _base(as_workspace(root)) / "sessions")
    error = _home_outside_repo(home)
    if error:
        raise StoreError(error)


RECORD_SCHEMA = 2  # 1: the card also carried decisions and evidence; 2: those live in the decision log
CARD_FIELDS = frozenset({"work_id", "phase", "sources", "assignments", "blockers", "next", "suggested_skills"})
V1_FIELDS = CARD_FIELDS | {"decisions", "evidence"}
LOG_FIELDS = ("new_decisions", "new_evidence")
EVIDENCE_STATES = ("pass-observed", "fail", "unverified", "stale")
LEGACY_GUIDANCE = ("record payloads no longer carry decisions or evidence: put only what is new since this "
                   "work's last record in new_decisions ([{text, refs?, supersedes?}]) and new_evidence "
                   "([{claim, state, ref, updates?}]); earlier entries stay in the work's decision log")


def validate_payload(payload, version=RECORD_SCHEMA):
    """A stored state card payload of record schema `version` (1 or 2)."""
    if version not in (1, 2) or not isinstance(payload, dict) or set(payload) != (V1_FIELDS if version == 1
                                                                                 else CARD_FIELDS):
        raise StoreError("INVALID_PAYLOAD")
    for key in ("work_id", "phase", "next"):
        if not isinstance(payload[key], str) or not payload[key]:
            raise StoreError("INVALID_PAYLOAD")
    lists = ("sources", "assignments", "blockers", "suggested_skills") + (("decisions", "evidence") if version == 1 else ())
    for key in lists:
        if not isinstance(payload[key], list):
            raise StoreError("INVALID_PAYLOAD")
    for key in ("blockers", "suggested_skills") + (("decisions",) if version == 1 else ()):
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
    for item in payload.get("evidence", ()):
        if (not isinstance(item, dict) or set(item) != {"claim", "ref", "state"}
                or any(not isinstance(v, str) for v in item.values())
                or item["state"] not in EVIDENCE_STATES):
            raise StoreError("INVALID_EVIDENCE")
    _check_size(payload)


def _check_size(payload):
    try:
        if len(encoded(payload)) > MAX_PAYLOAD:
            raise StoreError("PAYLOAD_TOO_LARGE")
    except (TypeError, ValueError) as exc:
        if isinstance(exc, StoreError):
            raise
        raise StoreError("INVALID_PAYLOAD") from exc


def _valid_decision(item):
    """Model-supplied decision fields (types only; target checks come later)."""
    return (isinstance(item, dict) and "text" in item and set(item) <= {"text", "refs", "supersedes"}
            and isinstance(item["text"], str) and item["text"] != ""
            and ("refs" not in item or (isinstance(item["refs"], list)
                                        and all(isinstance(ref, str) for ref in item["refs"])))
            and ("supersedes" not in item or (isinstance(item["supersedes"], str) and item["supersedes"] != "")))


def _valid_evidence(item):
    """Model-supplied evidence fields (types only; target checks come later)."""
    return (isinstance(item, dict) and {"claim", "state", "ref"} <= set(item) <= {"claim", "state", "ref", "updates"}
            and isinstance(item["claim"], str) and item["claim"] != "" and isinstance(item["ref"], str)
            and item["state"] in EVIDENCE_STATES
            and ("updates" not in item or (isinstance(item["updates"], str) and item["updates"] != "")))


def split_request(payload):
    """(state card payload, new log entries) of a record request; StoreError on any contract violation.

    A request is a schema 2 card plus optional new_decisions and new_evidence lists.
    The old decisions/evidence fields are refused with guidance (LEGACY_FIELDS). Entries
    are (kind, fields) pairs, decisions first, each list in the given order.
    """
    if isinstance(payload, dict) and {"decisions", "evidence"} & set(payload):
        raise StoreError("LEGACY_FIELDS", LEGACY_GUIDANCE)
    if not isinstance(payload, dict):
        raise StoreError("INVALID_PAYLOAD")
    card = {k: v for k, v in payload.items() if k not in LOG_FIELDS}
    validate_payload(card)
    decisions, evidence = payload.get("new_decisions", []), payload.get("new_evidence", [])
    if not isinstance(decisions, list) or not all(_valid_decision(item) for item in decisions):
        raise StoreError("INVALID_DECISION")
    if not isinstance(evidence, list) or not all(_valid_evidence(item) for item in evidence):
        raise StoreError("INVALID_EVIDENCE")
    _check_size(payload)
    return card, ([("decision", item) for item in decisions] + [("evidence", item) for item in evidence])


def atomic_write(base, path, data):
    """Replace path atomically; base is the guard root (the store home for handoff files)."""
    guard(base, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    guard(base, path)
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
        if temp is not None:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass  # best effort: never mask the write result or its error


@contextmanager
def _locked(path):
    """Hold the lock file at path (created if missing) without waiting; yields its stream.

    StoreError STORE_BUSY while another holder has it. Closing the stream inside the
    block releases the lock early (Windows: an open handle blocks moving its dir).
    """
    with path.open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise StoreError("STORE_BUSY") from exc
        try:
            yield stream
        finally:
            if not stream.closed:
                _unlock(stream)


def _unlock(stream):
    stream.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _names(stream, path):
    """True when path still names the open file: prune can move a session's lock file
    away (with its dir) between a writer's open and its lock (POSIX only). A deleted file
    never does, even when a new one at path reused its inode number."""
    try:
        here = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return False
    there = os.fstat(stream.fileno())
    return there.st_nlink > 0 and (here.st_dev, here.st_ino) == (there.st_dev, there.st_ino)


@contextmanager
def _lock(root, directory):
    storage_ready(root)
    guard(handoff_home(), directory / ".lock")
    directory.mkdir(parents=True, exist_ok=True)
    try:
        with _locked(directory / ".lock") as stream:
            # A lock on a moved-away file excludes no writer that opens the path now.
            if os.name != "nt" and not _names(stream, directory / ".lock"):
                raise StoreError("STORE_BUSY")
            yield
    except OSError as exc:
        raise StoreError("WRITE_FAILED") from exc


def _read(root, path, session, kind):
    ws = as_workspace(root)
    guard(handoff_home(), path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StoreError("CORRUPT_RECORD") from exc
    if (not isinstance(data, dict) or data.get("schema_version") not in (1, 2)
            or isinstance(data.get("schema_version"), bool)
            or data.get("workspace_id") != ws.repo_key
            or data.get("session_id") != session or data.get("kind") != kind):
        raise StoreError("RECORD_IDENTITY_MISMATCH")
    ident = "revision" if kind == "record" else "snapshot_id"
    if data.get(ident) != path.stem:
        raise StoreError("RECORD_IDENTITY_MISMATCH")
    checksum = data.get("checksum")
    body = {k: v for k, v in data.items() if k != "checksum"}
    if checksum != hashlib.sha256(encoded(body)).hexdigest():
        raise StoreError("CORRUPT_RECORD")
    validate_payload(data.get("payload"), data["schema_version"])
    return data


def _seal(data):
    return {**data, "checksum": hashlib.sha256(encoded(data)).hexdigest()}


KEEP_RECORDS = 5
KEEP_SNAPSHOTS = 3
CHAINS = {"record": "records", "snapshot": "snapshots"}


def _horizon(directory):
    """Horizon entry of each collection from horizon.json; both None without the file.

    An entry is None, the newest trimmed id of the collection's one chain, or (records
    of a session with several trimmed work chains) a list of such ids, one per chain.
    """
    path = directory / "horizon.json"
    guard(handoff_home(), path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return dict.fromkeys(CHAINS.values())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StoreError("CORRUPT_RECORD") from exc
    if not isinstance(data, dict) or set(data) != set(CHAINS.values()):
        raise StoreError("CORRUPT_RECORD")
    for value in data.values():
        ids = value if isinstance(value, list) else [] if value is None else [value]
        if (any(not (isinstance(v, str) and ID.fullmatch(v)) for v in ids)
                or (isinstance(value, list) and (not value or len(set(value)) != len(value)))):
            raise StoreError("CORRUPT_RECORD")
    return data


def _horizon_ids(value):
    """The horizon ids of one horizon.json entry."""
    return [] if value is None else list(value) if isinstance(value, list) else [value]


def _horizon_entry(ids):
    """The horizon.json entry for ids: None, one id (the single-chain form), or a sorted list."""
    return None if not ids else ids[0] if len(ids) == 1 else sorted(ids)


def _recency(directory):
    """Sort key of a record chain head: newest recorded_at first; the current.json hint breaks ties."""
    index = directory / "current.json"
    guard(handoff_home(), index)
    hint = (_load(index) or {}).get("revision")

    def key(head):
        try:
            when = _when(head.get("recorded_at"))
        except StoreError:
            when = datetime.min.replace(tzinfo=timezone.utc)
        return when, head["revision"] == hint, head["revision"]
    return key


def _chains(root, session, kind):
    """(chains, trimmed) of one session collection.

    chains: lists of objects newest first, ordered by recency of their head. A
    snapshot collection has at most one chain; a record collection has one per
    work_id, named by its head's payload.work_id (a chain written before per-work
    cards may hold older records of other works). trimmed: {horizon: [horizon and
    its ancestors still on disk]} (left by a trim interrupted between the horizon
    write and the deletes); they take no part in head selection. Only the oldest
    live object of a chain may miss its parent, and only when that parent is a horizon.
    """
    root = as_workspace(root)
    home = handoff_home()
    directory = session_dir(root, session)
    index = directory / ("current.json" if kind == "record" else "latest.json")
    collection = directory / CHAINS[kind]
    guard(home, collection)
    guard(home, index)
    horizons = _horizon_ids(_horizon(directory)[CHAINS[kind]])
    id_key = "revision" if kind == "record" else "snapshot_id"
    parent_key = "parent_record" if kind == "record" else "parent_snapshot"
    # Index files are hints, never authority: a completed object can outlive
    # an interrupted index replacement. Reconcile the complete chain on reads.
    # Only complete canonical objects participate; leftover temporary files do not.
    objects = {}
    if collection.exists():
        for path in collection.glob("*.json"):
            if not ID.fullmatch(path.stem):
                continue
            objects[path.stem] = _read(root, path, session, kind)
    trimmed = {}
    for horizon in horizons:
        trimmed[horizon] = [horizon]
        node = objects.get(horizon)
        while node is not None:
            del objects[node[id_key]]
            node = objects.get(node.get(parent_key))
            if node is not None:
                trimmed[horizon].append(node[id_key])
    # A trim always keeps newer objects, the oldest of which names the horizon.
    for horizon in horizons:
        if not any(obj.get(parent_key) == horizon for obj in objects.values()):
            raise StoreError("CORRUPT_RECORD")
    if not objects:
        return [], trimmed
    parents = {obj.get(parent_key) for obj in objects.values() if obj.get(parent_key)}
    if parents - objects.keys() - set(horizons):
        raise StoreError("INCOMPLETE_CHAIN")
    heads = [objects[ident] for ident in set(objects) - parents]
    names = [head["payload"]["work_id"] for head in heads] if kind == "record" else [None] * len(heads)
    if not heads or len(set(names)) != len(names):
        raise StoreError("AMBIGUOUS_CHAIN")
    chains, visited = [], set()
    for node in heads:
        chain = []
        while node:
            ident = node[id_key]
            if ident in visited:  # a fork or a cycle
                raise StoreError("AMBIGUOUS_CHAIN")
            visited.add(ident)
            chain.append(node)
            node = objects.get(node.get(parent_key))
        chains.append(chain)
    if len(visited) != len(objects):
        raise StoreError("AMBIGUOUS_CHAIN")
    if len(chains) > 1:
        recency = _recency(directory)
        chains.sort(key=lambda chain: recency(chain[0]), reverse=True)
    return chains, trimmed


def _head(root, session, kind):
    chains, _ = _chains(root, session, kind)
    return chains[0][0] if chains else None


def _expired(root, session, kind, ident):
    """Refuse an id that a trim already removed from any chain: rewriting it would
    resurrect it below a horizon (or close a cycle with the oldest live object)."""
    if any(ident in ids for ids in _chains(root, session, kind)[1].values()):
        raise StoreError("REQUEST_ID_EXPIRED")


def _trim(root, session, kind, keep, work_id=None):
    """Under the session lock: drop live objects beyond `keep` and trimmed leftovers
    of one chain (records: the chain of work_id; snapshots: the only chain).

    The chain's new horizon replaces its old one in horizon.json, written atomically
    before any delete, and deletes run oldest first, so an interruption leaves the
    newest removed object (the horizon) last; reads skip whatever remains and the
    chain's next trim removes it. Other chains and their horizons are untouched.
    Best effort: the new object is already durable, so a failed trim is left for
    the next write rather than failing this one.
    """
    directory = session_dir(root, session)
    collection = directory / CHAINS[kind]
    id_key = "revision" if kind == "record" else "snapshot_id"
    parent_key = "parent_record" if kind == "record" else "parent_snapshot"
    try:
        chains, trimmed = _chains(root, session, kind)
        chain = next((c for c in chains if kind != "record" or c[0]["payload"]["work_id"] == work_id), [])
        old = chain[-1].get(parent_key) if chain and chain[-1].get(parent_key) in trimmed else None
        surplus = [obj[id_key] for obj in chain[keep:]]
        if surplus:
            horizon = _horizon(directory)
            ids = [ident for ident in _horizon_ids(horizon[CHAINS[kind]]) if ident != old]
            horizon[CHAINS[kind]] = _horizon_entry(ids + [surplus[0]])
            atomic_write(handoff_home(), directory / "horizon.json", horizon)
        for ident in list(reversed(trimmed.get(old, []))) + list(reversed(surplus)):
            path = collection / (ident + ".json")
            guard(handoff_home(), path)
            path.unlink(missing_ok=True)
    except (StoreError, OSError):
        pass


def cards(root, session):
    """The head card of each of the session's work chains, most recently written first."""
    return [chain[0] for chain in _chains(root, session, "record")[0]]


def current(root, session, work_id=None):
    """The session's most recently written card, or with work_id the head card of that work (None if absent)."""
    heads = cards(root, session)
    if work_id is None:
        return heads[0] if heads else None
    return next((head for head in heads if head["payload"]["work_id"] == work_id), None)


def latest(root, session):
    return _head(root, session, "snapshot")


# Decision log: one append-only JSON Lines file per work_id at the repo level of the
# store, <repo dir>/logs/<sha256(work_id)>.jsonl, shared by every session of the repo
# (a checkout and its worktrees). Line 1 is a header naming the repo and the work;
# every later line is one entry. A line exists only once its newline is written: bytes
# after the last newline are a torn append (a crash mid-write), ignored by readers and
# cut by the next append under the per-work lock. A newline-terminated line that is not
# a valid entry fails closed (CORRUPT_LOG): it is never skipped or rewritten.
LOG_SCHEMA = 1
LOG_ENTRY_ID = re.compile(r"^([DE])([1-9][0-9]*)$")
LOG_KINDS = {"decision": "D", "evidence": "E"}
LOG_RUNTIME_FIELDS = ("id", "kind", "at", "session", "record")
LOG_LOCK_WAIT = 10.0  # seconds a record waits for another session's append to the same work


def _log_paths(ws, work_id):
    """(log, lock) paths of a work's decision log."""
    logs = _base(ws) / "logs"
    name = digest(work_id)
    return logs / (name + ".jsonl"), logs / (name + ".lock")


def _log_entry_fields(entry):
    """(kind, model-supplied fields) of a stored entry."""
    return entry["kind"], {k: v for k, v in entry.items() if k not in LOG_RUNTIME_FIELDS}


def _parse_log(ws, work_id, raw):
    """(entries, committed byte length) of a log file's bytes; StoreError on a bad header or entry."""
    end = raw.rfind(b"\n") + 1  # bytes past the last newline are a torn append
    lines = raw[:end].split(b"\n")[:-1]
    if not lines:
        return [], end
    try:
        header = json.loads(lines[0].decode("utf-8"))
        entries = [json.loads(line.decode("utf-8")) for line in lines[1:]]
    except (UnicodeError, ValueError) as exc:
        raise StoreError("CORRUPT_LOG") from exc
    if not isinstance(header, dict) or header.get("kind") != "log" or header.get("schema_version") != LOG_SCHEMA:
        raise StoreError("CORRUPT_LOG")
    if header.get("workspace_id") != ws.repo_key:
        raise StoreError("FOREIGN_REPO")
    if header.get("work_id") != work_id:
        raise StoreError("CORRUPT_LOG")
    last = {"D": 0, "E": 0}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("kind") not in LOG_KINDS:
            raise StoreError("CORRUPT_LOG")
        match = LOG_ENTRY_ID.fullmatch(entry.get("id")) if isinstance(entry.get("id"), str) else None
        kind, fields = _log_entry_fields(entry)
        if (match is None or match[1] != LOG_KINDS[kind] or int(match[2]) <= last[match[1]]
                or not all(isinstance(entry.get(k), str) and entry[k] for k in ("at", "session"))
                or not isinstance(entry.get("record", ""), str)
                or not (_valid_decision if kind == "decision" else _valid_evidence)(fields)):
            raise StoreError("CORRUPT_LOG")
        last[match[1]] = int(match[2])
    return entries, end


def read_log(root, work_id):
    """All committed entries of a work's decision log, oldest first ([] without a log). Read-only, no lock."""
    ws = as_workspace(root)
    path, _ = _log_paths(ws, work_id)
    guard(handoff_home(), path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise StoreError("CORRUPT_LOG") from exc
    return _parse_log(ws, work_id, raw)[0]


@contextmanager
def _log_lock(ws, work_id):
    """Hold the per-work log lock, waiting up to LOG_LOCK_WAIT for another writer (STORE_BUSY after)."""
    storage_ready(ws)
    _, lock = _log_paths(ws, work_id)
    guard(handoff_home(), lock)
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + LOG_LOCK_WAIT
    try:
        with ExitStack() as held:
            while True:
                try:
                    held.enter_context(_locked(lock))
                    break
                except StoreError as exc:
                    if str(exc) != "STORE_BUSY" or time.monotonic() >= deadline:
                        raise
                time.sleep(0.02)
            yield
    except OSError as exc:
        raise StoreError("WRITE_FAILED") from exc


def _append_log(ws, session, work_id, revision, entries):
    """Append entries for the card `revision` under the work's lock; returns the ids of all its entries.

    Entries already logged for this revision (an earlier attempt that appended but never
    wrote its card) must be the start of `entries`; only the rest is appended, so a replay
    never appends twice. Anything else is REQUEST_ID_REUSED. Ids continue the work's
    highest D/E number. All new lines go out in one write, then fsync.
    """
    home = handoff_home()
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
        mine = [entry for entry in logged if entry.get("record") == revision]
        if len(mine) > len(entries) or [_log_entry_fields(e) for e in mine] != entries[:len(mine)]:
            raise StoreError("REQUEST_ID_REUSED")
        last = {"D": 0, "E": 0}
        for entry in logged:
            match = LOG_ENTRY_ID.fullmatch(entry["id"])
            last[match[1]] = int(match[2])
        lines, ids, now = [], [entry["id"] for entry in mine], datetime.now(timezone.utc).isoformat()
        if end == 0:
            lines.append(encoded({"kind": "log", "schema_version": LOG_SCHEMA,
                                  "workspace_id": ws.repo_key, "work_id": work_id}))
        for kind, fields in entries[len(mine):]:
            letter = LOG_KINDS[kind]
            last[letter] += 1
            ids.append(letter + str(last[letter]))
            lines.append(encoded({"id": ids[-1], "kind": kind, "at": now, "session": session, "record": revision,
                                  **copy.deepcopy(fields)}))
        if len(ids) == len(mine) and end == len(raw):
            return ids
        try:
            with path.open("r+b" if raw else "wb") as stream:
                stream.truncate(end)  # drop a torn append; committed lines are never touched
                stream.seek(end)
                stream.write(b"".join(line + b"\n" for line in lines))
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise StoreError("WRITE_FAILED") from exc
        return ids


def record(root, session, payload, expected, request_id, *, cursor=None):
    """Write a schema 2 state card from a record request and append its new log entries.

    The entries are appended (under the per-work log lock) before the card is written, so
    a card always follows its entries; replaying the request completes an interrupted one.
    """
    card, entries = split_request(payload)
    return _commit(root, session, card, RECORD_SCHEMA, expected, request_id, cursor=cursor, entries=entries)


def _commit(root, session, payload, version, expected, request_id, *, cursor=None, adopted_from=None, entries=()):
    """Under the session lock, write a card of schema `version` on its work chain (idempotent per request id)."""
    validate_payload(payload, version)
    entries = list(entries)
    if version == 1 and entries:
        raise StoreError("INVALID_PAYLOAD")
    if not isinstance(request_id, str) or not request_id or len(request_id) > 512:
        raise StoreError("INVALID_REQUEST_ID")
    root = as_workspace(root)
    home = handoff_home()
    directory = session_dir(root, session)
    revision = digest(session + "\0record\0" + request_id)
    path = directory / "records" / (revision + ".json")
    with _lock(root, directory):
        _expired(root, session, "record", revision)
        if path.exists():
            prior = _read(root, path, session, "record")
            logged = [_log_entry_fields(entry) for entry in read_log(root, payload["work_id"])
                      if entry.get("record") == revision] if version != 1 else []
            if (prior["payload"] != payload or prior.get("adopted_from") != adopted_from
                    or prior["schema_version"] != version or logged != entries):
                raise StoreError("REQUEST_ID_REUSED")
            return prior
        previous = current(root, session, payload["work_id"])
        if expected != (previous["revision"] if previous else None):
            raise StoreError("REVISION_CONFLICT")
        ids = _append_log(root, session, payload["work_id"], revision, entries) if entries else []
        data = _seal({"schema_version": version, "kind": "record", "workspace_id": root.repo_key,
                      "worktree_path": str(root.root),
                      "session_id": session, "revision": revision, "parent_record": expected,
                      "recorded_at": datetime.now(timezone.utc).isoformat(),
                      "transcript_cursor": cursor, "adopted_from": adopted_from, "payload": copy.deepcopy(payload),
                      **({"log_entries": ids} if version >= 2 else {})})
        atomic_write(home, path, data)
        atomic_write(home, directory / "current.json", {"revision": revision})
        _trim(root, session, "record", KEEP_RECORDS, payload["work_id"])
        return data


def snapshot(root, session, event_key):
    if not isinstance(event_key, str) or not event_key or len(event_key) > 1024:
        raise StoreError("INVALID_EVENT_KEY")
    root = as_workspace(root)
    home = handoff_home()
    directory = session_dir(root, session)
    ident = digest(session + "\0snapshot\0" + event_key)
    path = directory / "snapshots" / (ident + ".json")
    with _lock(root, directory):
        _expired(root, session, "snapshot", ident)
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
        atomic_write(home, path, data)
        atomic_write(home, directory / "latest.json", {"snapshot_id": ident})
        _trim(root, session, "snapshot", KEEP_SNAPSHOTS)
        return data


def list_snapshots(root, snapshot_id=None):
    root = as_workspace(root)
    home = handoff_home()
    base = _base(root) / "sessions"
    guard(home, base)
    if not base.exists():
        return []
    result = []
    for folder in sorted(base.iterdir()):
        guard(home, folder)
        if not folder.is_dir() or not ID.fullmatch(folder.name):
            continue
        collection = folder / "snapshots"
        guard(home, collection)
        for path in collection.glob(snapshot_id + ".json" if snapshot_id else "*.json"):
            guard(home, path)
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
    root = as_workspace(root)
    matches =[item for item in list_snapshots(root, snapshot_id) if item["snapshot_id"] == snapshot_id]
    if len(matches) != 1:
        raise StoreError("SNAPSHOT_NOT_FOUND")
    if "error" in matches[0]:
        raise StoreError(matches[0]["error"])
    original = matches[0]["session_id"]
    if original == session:
        raise StoreError("SAME_SESSION_ADOPTION")
    source = _read(root, session_dir(root, original) / "snapshots" / (snapshot_id + ".json"), original, "snapshot")
    return adopt(root, session, source, snapshot_id)


def adopt(root, session, source, snapshot_id):
    """Start `session`'s chain from a validated snapshot, with its observed state marked unverified.

    The card keeps the snapshot's schema version (a schema 1 card keeps its decisions and
    evidence); nothing is written to the decision log.
    """
    payload = copy.deepcopy(source["payload"])
    for assignment in payload["assignments"]:
        assignment["observed_state"] = "needs-verification"
    for evidence in payload.get("evidence", ()):
        evidence["state"] = "unverified"
    return _commit(root, session, payload, source["schema_version"], None, "adopt:" + snapshot_id,
                   adopted_from=snapshot_id)


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
    try:
        again = _only_binding(root, staging)
    except StoreError:
        staging.rename(entry)
        raise
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


STAGING = re.compile(r"^\.pruning-[a-f0-9]{64}$")


def _leftover_state(root, directory, cutoff):
    """'reclaimable' for an empty staging dir or one holding only an old binding, else 'kept'."""
    guard(root, directory / "binding.json")
    if not directory.is_dir():
        return "kept"
    if not any(directory.iterdir()):
        return "reclaimable"
    found = _only_binding(root, directory)
    return "reclaimable" if found and found[1].st_mtime < cutoff else "kept"


def _reclaim(root, directory, cutoff, expiry_cutoff=None, repo_key=None):
    """Delete a leftover staging dir, re-verifying at deletion time. True when removed.

    A binding that is no longer old (another run moved a just-rebound session
    onto this name) is kept; rmdir fails if anything else appeared. A leftover
    holding an expired session is deleted only if it is still expired and unlocked.
    """
    found = _only_binding(root, directory)
    if found is not None:
        if found[1].st_mtime >= cutoff:
            return False
        found[0].unlink()
    elif expiry_cutoff is not None and any(directory.iterdir()):
        if not _expired_leftover(root, directory, expiry_cutoff, repo_key):
            return False
        shutil.rmtree(directory)
        return True
    directory.rmdir()
    return True


BINDING_ONLY_DAYS = 14
EXPIRY_DAYS = 30
SESSION_FILES = {"binding.json", "horizon.json", "current.json", "latest.json", "compaction.json", "facts.json", ".lock"}
# Per-session files of the native-facts cache (<config dir>/cache/facts), keyed by the raw session id.
CACHE_SESSION = re.compile(r"^[A-Za-z0-9-]+$")
FACTS_FLAGS = ("pending-edit", "pending-dispatch", "reminded")  # reminder flags; record clears them
# Enablement marker: a native SessionStart with handoff enabled for the workspace creates it; capture,
# reminders and the PreCompact fold run only for sessions that have it. record never clears it.
FACTS_MARKER = "enabled"
CACHE_FLAGS = (*FACTS_FLAGS, FACTS_MARKER)
CACHE_FILE = re.compile(r"^([A-Za-z0-9-]+)\.(?:jsonl(?:\.folding-[A-Za-z0-9]+)?|%s)$" % "|".join(CACHE_FLAGS))
# Rewritten on resume (binding), compaction and every write; their mtime counts as activity.
ACTIVITY_FILES = SESSION_FILES - {".lock"}
# Session content that lets a dir without records or snapshots expire (a binding alone does not).
UNRECORDED = ("facts.json", "compaction.json")


@dataclass(frozen=True)
class Scan:
    newest: object   # newest activity (epoch seconds): record/snapshot stamps and
                     # session-file mtimes; None without any record, snapshot, facts.json or compaction.json
    records: int
    snapshots: int
    extra: bool      # anything a session dir does not normally hold
    owners: frozenset = frozenset()  # stored workspace_ids of records, snapshots and binding

    def foreign(self, repo_key):
        """True when any stored workspace_id names another repo (a REPO_DIR_CHARS prefix collision)."""
        return bool(self.owners - {repo_key})


def _load(path):
    """Parsed JSON object of a session file, else None."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _owner(data):
    """Stored workspace_id; None when the file is unreadable or has none."""
    if data is None or "workspace_id" not in data:
        return None
    owner = data["workspace_id"]
    return owner if isinstance(owner, str) else "<invalid>"


def _stamp(path, key, data):
    """Stored timestamp of a record/snapshot file; its mtime when missing or unparseable."""
    try:
        when = datetime.fromisoformat(data.get(key))
        return (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).timestamp()
    except (ValueError, TypeError, AttributeError, OverflowError):
        return path.stat().st_mtime


def _scan(home, directory):
    """Read-only inventory of a session dir; never follows links (StoreError LINKED_STORAGE)."""
    guard(home, directory)
    newest, counts, extra, touched, unrecorded = None, {"records": 0, "snapshots": 0}, False, None, False
    owners = set()
    for child in directory.iterdir():
        guard(home, child)
        if child.name in counts and child.is_dir():
            key = "recorded_at" if child.name == "records" else "captured_at"
            for item in child.iterdir():
                guard(home, item)
                if item.name.startswith(".pending-") and item.is_file():
                    continue
                if not item.is_file() or item.suffix != ".json" or not ID.fullmatch(item.stem):
                    extra = True
                    continue
                counts[child.name] += 1
                data = _load(item)
                owners.add(_owner(data))
                when = _stamp(item, key, data)
                newest = when if newest is None else max(newest, when)
        elif not (child.is_file() and (child.name in SESSION_FILES or child.name.startswith(".pending-"))):
            extra = True
        elif child.name in ACTIVITY_FILES:
            mtime = child.lstat().st_mtime
            touched = mtime if touched is None else max(touched, mtime)
            unrecorded = unrecorded or child.name in UNRECORDED
            if child.name in ("binding.json", *UNRECORDED):
                owners.add(_owner(_load(child)))
    if touched is not None and (newest is not None or unrecorded):
        # A session that compacted or folded facts but never recorded ages on its files alone.
        newest = touched if newest is None else max(newest, touched)
    return Scan(newest, counts["records"], counts["snapshots"], extra, frozenset(owners - {None}))


def _lock_held(home, directory):
    """True when the session lock is held (or cannot be probed). Never creates the lock file."""
    path = directory / ".lock"
    guard(home, path)
    try:
        stream = path.open("r+b")
    except FileNotFoundError:
        return False
    except OSError:
        return True
    with stream:
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except OSError:
            return True
    return False


def _is_expired(scan, cutoff, repo_key):
    return scan.newest is not None and scan.newest < cutoff and not scan.extra and not scan.foreign(repo_key)


def _remove_expired(home, entry, staging, cutoff, repo_key):
    """Under the session lock, re-verify an expired session, move it aside, re-verify it, delete it.

    The lock is taken without waiting (StoreError STORE_BUSY while a record or snapshot
    holds it), so no locked write runs from the check until the delete: a writer opening
    the lock path after the move gets a new dir and an empty chain, and one that opened
    the moved file is refused by _lock. On Windows an open handle blocks the move, so
    the lock is released just before it; a write completed in that gap, or an unlocked
    session-file write (binding, compaction) before the move, fails the re-check and the
    dir is moved back. If the original name was recreated after the move, nothing is
    deleted: FileExistsError leaves the dir at the staging name for a later run.
    On Windows a record completed in the release gap, with the original name then
    recreated by a third writer before the move back, leaves the session (with that
    fresh record) parked at the staging name: the failed move back is reported with
    left_at, and a leftover is never deleted while it holds fresh activity.
    Returns True when removed.
    """
    lock = entry / ".lock"
    guard(home, lock)
    with _locked(lock) as stream:
        if not _is_expired(_scan(home, entry), cutoff, repo_key):
            return False
        if os.name == "nt":
            _unlock(stream)
            stream.close()
        entry.rename(staging)
        try:
            keep = not _is_expired(_scan(home, staging), cutoff, repo_key)
        except (StoreError, OSError):
            staging.rename(entry)
            raise
        if keep:
            staging.rename(entry)
            return False
        if os.path.lexists(entry):
            raise FileExistsError(errno.EEXIST, "session dir recreated during prune", str(entry))
        shutil.rmtree(staging)
        return True


def _cache_of(home, directory, name, cache):
    """Existing facts-cache files (spool, claimed leftovers, reminder flags, marker) of the session stored
    in directory; [] without a cache dir or a session id (binding.json, facts.json, compaction.json)
    whose digest is name. Never follows links."""
    if cache is None:
        return []
    session = None
    for file in ("binding.json", *UNRECORDED):
        guard(home, directory / file)
        data = _load(directory / file)
        found = data.get("session_id") if data else None
        if isinstance(found, str) and digest(found) == name:
            session = found
            break
    if session is None or not CACHE_SESSION.fullmatch(session):
        return []
    cache = Path(cache)
    try:
        guard(cache.parent, cache)
        paths = [cache / (session + ".jsonl"), *[cache / (session + "." + flag) for flag in CACHE_FLAGS],
                 *sorted(cache.glob(session + ".jsonl.folding-*"))]
        for path in paths:
            guard(cache.parent, path)
    except StoreError:
        return []  # a linked cache is left alone
    return [path for path in paths if path.is_file()]


def _drop_cache(result, name, paths):
    """Best effort after the session itself is gone; a failure is reported, never raised."""
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            result["errors"].append({"session_dir": name, "error": type(exc).__name__, "left_at": path.name})


def _expired_leftover(home, directory, cutoff, repo_key):
    """True for a staging dir holding an expired, unlocked session of this repo (an interrupted delete)."""
    return _is_expired(_scan(home, directory), cutoff, repo_key) and not _lock_held(home, directory)


# Keys a snapshot adds to the record it copies (see snapshot()); dropping them and
# resealing with kind "record" restores that record exactly.
SNAPSHOT_KEYS = ("checksum", "snapshot_id", "captured_at", "parent_snapshot")


def _when(value):
    """Parsed timestamp (UTC when naive); StoreError CORRUPT_RECORD when unparseable."""
    try:
        when = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise StoreError("CORRUPT_RECORD") from exc
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _archive_plan(home, ws, directory, name):
    """(newest record per work_id, skipped files) of the session stored in directory under digest name.

    Candidates are its valid records and the records its valid snapshots copied (a
    snapshot can outlive its record's trim); a records/ file wins over a snapshot copy
    of the same revision. Newest is the later recorded_at; on a tie the record further
    down the parent_record chain, and only when the chain cannot decide the larger
    revision. Unreadable, checksum-invalid or foreign files are skipped and returned
    as {"file", "reason"}; linked entries raise LINKED_STORAGE.
    """
    candidates, skipped, parents = [], [], {}
    for kind in CHAINS:
        collection = directory / CHAINS[kind]
        guard(home, collection)
        if not collection.is_dir():
            continue
        for path in sorted(collection.glob("*.json")):
            guard(home, path)
            if not ID.fullmatch(path.stem):
                continue
            try:
                raw = _load(path)
                if raw is None:
                    raise StoreError("CORRUPT_RECORD")
                session = raw.get("session_id")
                if not isinstance(session, str) or digest(session) != name:
                    raise StoreError("RECORD_IDENTITY_MISMATCH")
                data = _read(ws, path, session, kind)
                if kind == "snapshot":
                    data = _seal({k: v for k, v in data.items() if k not in SNAPSHOT_KEYS} | {"kind": "record"})
                if not isinstance(data.get("revision"), str) or not ID.fullmatch(data["revision"]):
                    raise StoreError("RECORD_IDENTITY_MISMATCH")
                when = _when(data.get("recorded_at"))
            except StoreError as exc:
                skipped.append({"file": CHAINS[kind] + "/" + path.name, "reason": str(exc)})
                continue
            if data["revision"] in parents:
                continue
            parents[data["revision"]] = data.get("parent_record")
            candidates.append((when, data))

    def descends(revision, ancestor):
        visited = set()
        while revision is not None and revision not in visited:
            visited.add(revision)
            revision = parents.get(revision)
            if revision == ancestor:
                return True
        return False

    newest = {}
    for when, data in candidates:
        work = data["payload"]["work_id"]
        if work not in newest:
            newest[work] = (when, data)
            continue
        best_when, best = newest[work]
        if when != best_when:
            newer = when > best_when
        elif descends(data["revision"], best["revision"]):
            newer = True
        elif descends(best["revision"], data["revision"]):
            newer = False
        else:
            newer = data["revision"] > best["revision"]
        if newer:
            newest[work] = (when, data)
    return {work: data for work, (_, data) in newest.items()}, skipped


def _archive_path(archive, work_id):
    return archive / (hashlib.sha256(work_id.encode("utf-8")).hexdigest()[:32] + ".json")


def _archive(home, ws, archive, plan):
    """Under the archive lock, write each planned record unless the archive holds one at least as new.

    An existing archive file that is not a valid record of this work_id raises
    CORRUPT_ARCHIVE (one of another repo FOREIGN_REPO): it is never overwritten.
    """
    if not plan:
        return
    with _lock(ws, archive):
        for work, data in sorted(plan.items()):
            path = _archive_path(archive, work)
            guard(home, path)
            if path.exists():
                existing = _load(path) or {}
                body = {k: v for k, v in existing.items() if k != "checksum"}
                try:
                    stored = _when(existing.get("recorded_at"))
                except StoreError as exc:
                    raise StoreError("CORRUPT_ARCHIVE") from exc
                if (existing.get("checksum") != hashlib.sha256(encoded(body)).hexdigest()
                        or not isinstance(existing.get("payload"), dict) or existing["payload"].get("work_id") != work):
                    raise StoreError("CORRUPT_ARCHIVE")
                if existing.get("workspace_id") != ws.repo_key:
                    raise StoreError("FOREIGN_REPO")
                if stored >= _when(data["recorded_at"]):
                    continue
            atomic_write(home, path, data)


def _stored_sessions(home):
    """Session digests with a dir (or .pruning- leftover) anywhere in the current store, any repo."""
    found, repos = set(), home / "v2" / "repos"
    if not repos.is_dir():
        return found
    for repo in repos.iterdir():
        sessions = repo / "sessions"
        if sessions.is_dir():
            for entry in sessions.iterdir():
                name = entry.name[len(".pruning-"):] if STAGING.fullmatch(entry.name) else entry.name
                if ID.fullmatch(name):
                    found.add(name)
    return found


def _orphan_cache(home, cache, cutoff, keep_session, claimed, apply):
    """Facts-cache files whose session has no dir anywhere in the store, grouped by session id.

    A session's files are listed when the newest of them is older than cutoff; under apply
    they are deleted after re-checking that no session dir appeared and none was touched.
    Names without a safe session id and linked entries are skipped, never followed."""
    out = {"candidates": [], "removed": 0, "skipped": [], "errors": []}
    cache, groups = Path(cache), {}
    try:
        guard(cache.parent, cache)
        if not cache.is_dir():
            return out
        present = _stored_sessions(home)
        for path in sorted(cache.iterdir()):
            if str(path) in claimed:
                continue
            try:
                guard(cache.parent, path)
            except StoreError as exc:
                out["skipped"].append({"path": str(path), "reason": str(exc)})
                continue
            match = CACHE_FILE.fullmatch(path.name)
            reason = None if match else "UNEXPECTED_NAME"
            if match and not path.is_file():
                reason = "UNEXPECTED_CONTENT"
            if reason:
                out["skipped"].append({"path": str(path), "reason": reason})
            elif match[1] != keep_session and digest(match[1]) not in present:
                groups.setdefault(match[1], []).append(path)
    except (StoreError, OSError) as exc:
        reason = str(exc) if isinstance(exc, StoreError) else type(exc).__name__
        out["skipped"].append({"path": str(cache), "reason": reason})
        return out
    for session, paths in sorted(groups.items()):
        try:
            newest = max(path.lstat().st_mtime for path in paths)
            if newest >= cutoff:
                continue
            out["candidates"].append({"session_id": session, "files": [str(p) for p in paths],
                                      "newest": datetime.fromtimestamp(newest, timezone.utc).isoformat()})
            if not apply or digest(session) in _stored_sessions(home):
                continue
            if max(path.lstat().st_mtime for path in paths) >= cutoff:
                continue
        except OSError:
            continue  # changed since the listing; left for a later run
        for path in paths:
            try:
                path.unlink()
                out["removed"] += 1
            except FileNotFoundError:
                pass
            except OSError as exc:
                out["errors"].append({"path": str(path), "error": type(exc).__name__})
    return out


def prune(root, *, older_than_days=None, expired_older_than_days=None, keep_session=None, apply=False, now=None,
          facts_cache=None):
    """List (or with apply, remove) abandoned session dirs; dry-run unless apply.

    Two kinds, each with its own age: dirs holding only an old binding.json
    (`candidates`; such a dir records only that a session started) older than
    `older_than_days` (default 14), and expired sessions (`expired`) whose newest
    activity is older than `expired_older_than_days` (default 30). An
    expired session is skipped while its lock is held or if it holds anything a
    session dir does not normally hold, and as FOREIGN_REPO when a stored
    workspace_id names another repo. Linked entries are skipped (listed in
    `skipped`), never followed or removed. Staging dirs left by an interrupted earlier
    run (`.pruning-<digest>`) are listed in `leftovers`; empty ones, ones holding
    only an old binding and expired sessions are reclaimed under apply. They sort
    before session dirs, so a leftover is cleared before its own session is moved
    aside again. The kept session's own leftover is listed as kept, never reclaimed.
    Only the current store is pruned; legacy stores are never touched.
    Each expired session (and reclaimable leftover) lists under `archive` the work_ids
    of its newest valid record per work_id (`archive_skipped` names unusable files);
    under apply they are written to the repo-level `archive/` (newest recorded_at
    wins) before the dir is moved aside, and a failed archive keeps the dir.
    With facts_cache (the native-facts cache dir), each listed entry whose session id is
    known names that session's existing cache files (`facts_cache`: spool, claimed spool
    leftovers, reminder flags, enablement marker); under apply they are deleted once the session itself is.
    Cache files of sessions with no dir anywhere in the store are listed under
    `orphan_facts_cache` (only when there is something to report) once their newest
    file is older than the expiry age; under apply they are deleted.
    """
    for days in (older_than_days, expired_older_than_days):
        if days is not None and (not isinstance(days, int) or isinstance(days, bool) or days < 1):
            raise StoreError("INVALID_ARGUMENTS")
    binding_days = older_than_days or BINDING_ONLY_DAYS
    expiry_days = expired_older_than_days or EXPIRY_DAYS
    ws = as_workspace(root)
    sessions = _base(ws) / "sessions"
    archive = sessions.parent / "archive"
    home = handoff_home()
    guard(home, sessions)
    result = {"older_than_days": binding_days, "expired_older_than_days": expiry_days,
              "applied": bool(apply), "candidates": [], "removed": 0, "expired": [], "expired_removed": 0,
              "leftovers": [], "reclaimed": 0, "skipped": [], "errors": []}
    if not sessions.is_dir():
        return result
    keep = digest(keep_session) if keep_session else None
    clock = now if now is not None else datetime.now(timezone.utc).timestamp()
    cutoff, expiry_cutoff = clock - binding_days * 86400, clock - expiry_days * 86400
    for entry in sorted(sessions.iterdir()):
        if STAGING.fullmatch(entry.name):
            if keep and entry.name == ".pruning-" + keep:
                # The current session's own history may be parked here; listed, never reclaimed.
                result["leftovers"].append({"staging_dir": entry.name, "state": "kept"})
                continue
            try:
                state = _leftover_state(home, entry, cutoff)
                if state == "kept" and _expired_leftover(home, entry, expiry_cutoff, ws.repo_key):
                    state = "reclaimable"
                item = {"staging_dir": entry.name, "state": state}
                name = entry.name[len(".pruning-"):]
                cached = _cache_of(home, entry, name, facts_cache) if state == "reclaimable" else []
                plan, unusable = _archive_plan(home, ws, entry, name) if state == "reclaimable" else ({}, [])
                result["leftovers"].append(item | ({"archive": sorted(plan)} if plan else {})
                                           | ({"archive_skipped": unusable} if unusable else {})
                                           | ({"facts_cache": [str(p) for p in cached]} if cached else {}))
                if apply and state == "reclaimable":
                    _archive(home, ws, archive, plan)  # raises, keeping the leftover, unless archived
                if apply and state == "reclaimable" and _reclaim(home, entry, cutoff, expiry_cutoff, ws.repo_key):
                    result["reclaimed"] += 1
                    _drop_cache(result, entry.name, cached)
            except StoreError as exc:
                result["skipped"].append({"entry": entry.name, "reason": str(exc)})
            except OSError as exc:
                result["errors"].append({"session_dir": entry.name, "error": type(exc).__name__,
                                         "left_at": entry.name if entry.exists() else None})
            continue
        if entry.name == keep:
            continue
        staging = sessions / (".pruning-" + entry.name)
        try:
            found = _stale_binding(home, entry, cutoff)
            if found is None:
                if not ID.fullmatch(entry.name) or not entry.is_dir():
                    continue
                scan = _scan(home, entry)
                if scan.newest is None or scan.newest >= expiry_cutoff:
                    continue
                if scan.extra:
                    result["skipped"].append({"entry": entry.name, "reason": "UNEXPECTED_CONTENT"})
                    continue
                if scan.foreign(ws.repo_key):
                    result["skipped"].append({"entry": entry.name, "reason": "FOREIGN_REPO"})
                    continue
                if _lock_held(home, entry):
                    result["skipped"].append({"entry": entry.name, "reason": "STORE_BUSY"})
                    continue
                cached = _cache_of(home, entry, entry.name, facts_cache)
                plan, unusable = _archive_plan(home, ws, entry, entry.name)
                result["expired"].append({
                    "session_dir": entry.name, "records": scan.records, "snapshots": scan.snapshots,
                    "newest": datetime.fromtimestamp(scan.newest, timezone.utc).isoformat(),
                    "archive": sorted(plan), **({"archive_skipped": unusable} if unusable else {}),
                    **({"facts_cache": [str(p) for p in cached]} if cached else {})})
                if apply:
                    try:
                        _archive(home, ws, archive, plan)  # raises, keeping the session, unless archived
                        removed = _remove_expired(home, entry, staging, expiry_cutoff, ws.repo_key)
                    except StoreError:
                        result["expired"].pop()  # a kept session is listed once: under skipped
                        raise
                    if removed:
                        result["expired_removed"] += 1
                        _drop_cache(result, entry.name, cached)
                continue
            binding, info = found
            try:
                session_id = json.loads(binding.read_text(encoding="utf-8")).get("session_id")
            except (OSError, ValueError, AttributeError):
                session_id = None
            cached = _cache_of(home, entry, entry.name, facts_cache)
            result["candidates"].append({
                "session_dir": entry.name, "session_id": session_id if isinstance(session_id, str) else None,
                "bound_at": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
                **({"facts_cache": [str(p) for p in cached]} if cached else {})})
            if apply and _remove_stale(home, entry, staging, info):
                result["removed"] += 1
                _drop_cache(result, entry.name, cached)
        except StoreError as exc:
            result["skipped"].append({"entry": entry.name, "reason": str(exc)})
        except OSError as exc:
            result["expired"] = [item for item in result["expired"] if item["session_dir"] != entry.name]
            result["errors"].append({"session_dir": entry.name, "error": type(exc).__name__,
                                     "left_at": staging.name if staging.exists() else None})
    if facts_cache is not None:
        claimed = {path for item in (*result["candidates"], *result["expired"], *result["leftovers"])
                   for path in item.get("facts_cache", ())}
        orphans = _orphan_cache(home, facts_cache, expiry_cutoff, keep_session, claimed, apply)
        if orphans["candidates"] or orphans["skipped"] or orphans["errors"]:
            result["orphan_facts_cache"] = orphans
    return result
