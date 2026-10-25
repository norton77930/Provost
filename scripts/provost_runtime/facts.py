"""Facts layer: fold the native-facts spool into <session_dir>/facts.json.

Facts are observations of what a session touched; git decides the current state.
No model, network or scheduler. Every git command is bounded and its failure
becomes a code on the repo entry, never an exception out of fold.
"""
import json
import os
import re
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import handoff_home
from .store import StoreError, _lock, _seal, as_workspace, atomic_write, encoded, guard, session_dir

SPOOL_SESSION = re.compile(r"^[A-Za-z0-9-]+$")  # the capture hook spools nothing else
KINDS = ("edit", "agent", "worktree", "dir")
# Matched case-insensitively against the path with forward slashes, before any git lookup:
# ~/.claude is itself a git repo, but its jobs and projects dirs are scratch, not repo work.
OUTSIDE_DEFAULT = ("/.claude/jobs/", "/.claude/projects/", "/appdata/local/temp/", "/tmp/")
OUTSIDE = OUTSIDE_DEFAULT
MAX_REPOS = 5
MAX_DIRTY = 50
MAX_COMMITS = 20
MAX_WORKTREES = 20  # the newest worktree events are kept
SUBJECT_CHARS = 80
DESC_CHARS = 120
MAX_BYTES = 65536
CHECKSUM_BYTES = len(',"checksum":""') + 64  # what _seal adds to the encoded body
GIT_TIMEOUT = 3
GIT_BUDGET = 12
RENAME_ATTEMPTS = 5
GIT_FIELDS = ("branch", "dirty", "dirty_total", "commits")  # repo entry fields read from git


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _epoch(text):
    when = datetime.fromisoformat(text)
    return int((when if when.tzinfo else when.replace(tzinfo=timezone.utc)).timestamp())


class _GitError(Exception):
    pass


class _Git:
    """Runs git with a per-command timeout inside one overall budget."""

    def __init__(self, budget=None):
        self.deadline = time.monotonic() + (GIT_BUDGET if budget is None else budget)

    def __call__(self, cwd, *args):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise _GitError("GIT_TIMEOUT")
        try:
            # Optional locks off: a read must never take the index lock from the user's own git.
            result = subprocess.run(["git", "-C", str(cwd), "-c", "core.quotepath=false", *args],
                                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    timeout=min(GIT_TIMEOUT, remaining),
                                    env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"))
        except subprocess.TimeoutExpired as exc:
            raise _GitError("GIT_TIMEOUT") from exc
        except FileNotFoundError as exc:
            raise _GitError("GIT_UNAVAILABLE") from exc
        except OSError as exc:
            raise _GitError("GIT_FAILED") from exc
        if result.returncode != 0:
            raise _GitError("GIT_FAILED")
        return result.stdout


def _key(path):
    return os.path.normcase(os.path.normpath(str(path)))


def _outside(path):
    text = str(path).replace("\\", "/").lower() + "/"
    return any(pattern in text for pattern in OUTSIDE)


def _absolute(text, cwd):
    """Absolute Path for a spooled path (relative ones against cwd), else None."""
    if not isinstance(text, str) or not text:
        return None
    text = os.path.expanduser(text)
    if os.name == "nt":
        msys = re.match(r"^/([A-Za-z])(/.*)?$", text)  # Git Bash form of a drive path: /c/x
        if msys:
            text = msys.group(1) + ":" + (msys.group(2) or "/")
    path = Path(text)
    if path.is_absolute():
        return path
    if isinstance(cwd, str) and cwd and Path(cwd).is_absolute():
        return Path(cwd) / path
    return None


class _Finder:
    """Enclosing repo of a path: nearest ancestor holding .git, confirmed by
    `git rev-parse --show-toplevel` once per candidate. Cached per fold."""

    def __init__(self, git):
        self.git = git
        self.walked = {}
        self.tops = {}

    def _walk(self, path):
        start = path if path.is_dir() else path.parent
        key = _key(start)
        if key not in self.walked:
            self.walked[key] = next((node for node in (start, *start.parents) if (node / ".git").exists()), None)
        return self.walked[key]

    def root_of(self, path):
        """Toplevel string of the repo holding path, else None (outside every repo)."""
        if path is None or _outside(path):
            return None
        candidate = self._walk(path)
        if candidate is None:
            return None
        key = _key(candidate)
        if key not in self.tops:
            try:
                top = self.git(candidate, "rev-parse", "--show-toplevel").strip()
                self.tops[key] = str(Path(top)) if top else None
            except _GitError as exc:
                # Not a repo after all; or git could not answer in time, then the walk stands.
                self.tops[key] = None if str(exc) == "GIT_FAILED" else str(candidate)
        return self.tops[key]


def _parse(raw):
    """(valid events, invalid line count) of complete spool lines."""
    events, invalid = [], 0
    for line in raw.split(b"\n"):
        if not line.strip():
            continue
        try:
            # The hook clips text by character; a non-UTF-8 locale can split a multibyte one.
            item = json.loads(line.decode("utf-8", errors="replace"))
        except ValueError:
            invalid += 1
            continue
        try:
            encoded(item)  # a lone surrogate escape loads but could never be written to facts.json
        except ValueError:
            invalid += 1
            continue
        if _valid(item):
            events.append(item)
        else:
            invalid += 1
    return events, invalid


def _valid(item):
    if (not isinstance(item, dict) or type(item.get("v")) is not int or item["v"] != 1
            or item.get("k") not in KINDS or type(item.get("ts")) is not int
            or not isinstance(item.get("cwd"), str) or not isinstance(item.get("agent"), str)):
        return False
    if item["k"] == "agent":
        return (isinstance(item.get("role"), str) and isinstance(item.get("desc"), str)
                and type(item.get("bg")) is bool)
    return isinstance(item.get("p"), str) and bool(item["p"])


def _worktree_path(text, cwd):
    """A worktree event names a path or a bare worktree name (<cwd>/.claude/worktrees/<name>)."""
    if "/" not in text and "\\" not in text and isinstance(cwd, str) and Path(cwd).is_absolute():
        named = Path(cwd) / ".claude" / "worktrees" / text
        if named.exists():
            return named
    return _absolute(text, cwd)


def gather(previous, events, invalid, finder):
    """Merge spooled events into the previous facts (no git status/log yet).

    Returns the working state: files keyed by normalized path, touched repos with
    first/last touch, dispatches, worktrees and the running counters.
    """
    files = {_key(f["path"]): dict(f, agents=list(f["agents"])) for f in previous.get("files", [])}
    # Git facts are carried too: a git step that fails in this fold keeps the previous value.
    repos = {_key(r["root"]): {k: r[k] for k in ("root", "first_touched_at", "last_touched_at", *GIT_FIELDS) if k in r}
             for r in previous.get("repos", [])}
    state = {"files": files, "repos": repos, "agents": list(previous.get("agents", [])),
             "worktrees": list(previous.get("worktrees", [])),
             "outside_repo_count": previous.get("outside_repo_count", 0),
             "files_omitted": previous.get("files_omitted", 0),
             "agents_omitted": previous.get("agents_omitted", 0),
             "repos_omitted": previous.get("repos_omitted", 0),
             "spool_errors": previous.get("spool_errors", 0) + invalid,
             "events_captured": previous.get("events_captured", 0) + len(events),
             "last_change_at": previous.get("last_change_at")}

    def touch(path, ts):
        root = finder.root_of(path)
        if root is not None:
            when = _iso(ts)
            entry = repos.setdefault(_key(root), {"root": root, "first_touched_at": when, "last_touched_at": when})
            entry["first_touched_at"] = min(entry["first_touched_at"], when)
            entry["last_touched_at"] = max(entry["last_touched_at"], when)
        return root

    for event in sorted(events, key=lambda e: e["ts"]):
        ts, cwd, kind = event["ts"], event["cwd"], event["k"]
        touch(_absolute(cwd, None), ts)
        if kind == "edit":
            path = _absolute(event["p"], cwd)
            if touch(path, ts) is None:
                state["outside_repo_count"] += 1
                continue
            entry = files.setdefault(_key(path), {"path": str(path), "count": 0, "last_ts": ts, "agents": []})
            entry["count"] += 1
            entry["last_ts"] = max(entry["last_ts"], ts)
            if event["agent"] not in entry["agents"]:
                entry["agents"] = sorted(entry["agents"] + [event["agent"]])
        elif kind == "agent":
            state["agents"].append({"role": event["role"], "desc": event["desc"][:DESC_CHARS],
                                    "bg": event["bg"], "ts": ts})
        elif kind == "worktree":
            tool = event.get("tool")
            state["worktrees"].append({"path": event["p"], "tool": tool if isinstance(tool, str) else "", "ts": ts})
            touch(_worktree_path(event["p"], cwd), ts)
        else:
            touch(_absolute(event["p"], cwd), ts)
    state["worktrees"] = state["worktrees"][-MAX_WORKTREES:]
    return state


def _inspect(git, repo):
    """Current git facts of one repo; each failed step leaves a code and the rest still run.
    A failed step keeps the value the previous fold stored (branch, dirty/dirty_total or commits),
    else the empty one. Returns (entry, newest commit epoch or None, the dirty list read in this
    fold or [] when git status failed: a kept list never dates a change)."""
    root = repo["root"]
    entry = {"root": root, "branch": repo.get("branch"), "first_touched_at": repo["first_touched_at"],
             "last_touched_at": repo["last_touched_at"], "dirty": list(repo.get("dirty", [])),
             "dirty_total": repo.get("dirty_total", 0), "commits": list(repo.get("commits", [])), "error": None}
    newest, fresh = None, []
    try:
        entry["branch"] = git(root, "rev-parse", "--abbrev-ref", "HEAD").strip() or None
    except _GitError as exc:
        entry["error"] = str(exc)
    try:
        tokens = iter(git(root, "status", "--porcelain=v1", "-z").split("\0"))
        dirty = []
        for token in tokens:
            if len(token) < 4:
                continue
            dirty.append({"status": token[:2], "path": token[3:]})
            if token[0] in "RC":
                next(tokens, None)  # -z puts a rename's source path in its own field
        fresh = dirty[:MAX_DIRTY]
        entry["dirty"], entry["dirty_total"] = fresh, len(dirty)
    except _GitError as exc:
        entry["error"] = entry["error"] or str(exc)
    try:
        since = "--since=@%d" % _epoch(repo["first_touched_at"])
        commits = []
        for line in git(root, "log", since, "--no-merges", "-n", str(MAX_COMMITS), "--format=%h%x1f%ct%x1f%s").splitlines():
            parts = line.split("\x1f", 2)
            if len(parts) != 3:
                continue
            when = int(parts[1]) if parts[1].isdigit() else None
            commits.append({"hash": parts[0], "subject": parts[2][:SUBJECT_CHARS], "ts": when})
            if when is not None:
                newest = max(newest or 0, when)
        entry["commits"] = commits
    except _GitError as exc:
        entry["error"] = entry["error"] or str(exc)
    return entry, newest, fresh


def _dirty_mtimes(root, dirty, now):
    """mtimes of the (capped) tracked dirty paths that still exist, each clamped to now: a script
    edit the hook never saw still dates the change, an old uncommitted file does not look new, and
    a future mtime cannot keep a record stale. Untracked (??) entries stay listed but never date a
    change: logs and build output appear there without being session work."""
    found = []
    for item in dirty:
        if item["status"] == "??":
            continue
        try:
            found.append(min(os.stat(Path(root) / item["path"]).st_mtime, now))
        except (OSError, ValueError):
            continue  # deleted or unreadable
    return found


def summarize(ws, session, state, git, now):
    """facts.json body (unsealed) from the gathered state plus live git facts, within MAX_BYTES."""
    ranked = sorted(state["repos"].values(), key=lambda r: r["last_touched_at"], reverse=True)
    repos, changes = [], []
    for repo in ranked[:MAX_REPOS]:
        entry, newest, fresh = _inspect(git, repo)
        repos.append(entry)
        if newest is not None:
            changes.append(newest)
        changes.extend(_dirty_mtimes(entry["root"], fresh, now))
    files = sorted(state["files"].values(), key=lambda f: (f["last_ts"], f["path"]))
    if files:
        changes.append(files[-1]["last_ts"])
    if state["last_change_at"]:
        changes.append(_epoch(state["last_change_at"]))
    data = {"schema_version": 1, "kind": "facts", "workspace_id": ws.repo_key, "session_id": session,
            "updated_at": _iso(now), "last_change_at": _iso(max(changes)) if changes else None,
            "files": files, "files_omitted": state["files_omitted"],
            "outside_repo_count": state["outside_repo_count"],
            "agents": sorted(state["agents"], key=lambda a: a["ts"]), "agents_omitted": state["agents_omitted"],
            "worktrees": state["worktrees"], "repos": repos,
            "repos_omitted": state["repos_omitted"] + max(0, len(ranked) - MAX_REPOS),
            "spool_errors": state["spool_errors"], "events_captured": state["events_captured"],
            "spool_consumed": state.get("spool_consumed", {})}
    return _cap(data)


def _cap(data):
    """Drop oldest files, then oldest agents, then trailing dirty entries until the sealed file fits.

    Per-entry sizes only estimate the saving: an omitted counter can gain digits and an emptied
    list keeps no comma to lose, so each estimated fit is re-measured before it is accepted.
    """
    def excess():
        return len(encoded(data)) + CHECKSUM_BYTES - MAX_BYTES

    for key, counter in (("files", "files_omitted"), ("agents", "agents_omitted")):
        items, omitted = data[key], data[counter]
        over, drop = excess(), 0
        while over > 0 and drop < len(items):
            over -= len(encoded(items[drop])) + 1
            drop += 1
            if over <= 0:
                data[key], data[counter] = items[drop:], omitted + drop
                over = excess()
        data[key], data[counter] = items[drop:], omitted + drop
    for repo in reversed(data["repos"]):
        over = excess()
        while over > 0 and repo["dirty"]:
            over -= len(encoded(repo["dirty"].pop())) + 1
            if over <= 0:
                over = excess()
    return data


def _previous(ws, session, path):
    """Stored facts of this session, or {} when absent or not verifiable (start afresh)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}
    if (not isinstance(data, dict) or data.get("schema_version") != 1 or data.get("kind") != "facts"
            or data.get("workspace_id") != ws.repo_key or data.get("session_id") != session):
        return {}
    body = {k: v for k, v in data.items() if k != "checksum"}
    return data if _seal(body)["checksum"] == data.get("checksum") else {}


def stored(root, session):
    """(verified facts.json body or None, its path). Read-only."""
    ws = as_workspace(root)
    path = session_dir(ws, session) / "facts.json"
    guard(handoff_home(), path)
    return _previous(ws, session, path) or None, path


def summary(data, path):
    """Compact view of stored facts: per-repo counts (at most MAX_REPOS), dispatch counts by role."""
    roles = {}
    for agent in data.get("agents", []):
        roles[agent["role"]] = roles.get(agent["role"], 0) + 1
    repos = data.get("repos", [])
    return {"path": str(path), "updated_at": data.get("updated_at"), "last_change_at": data.get("last_change_at"),
            "repos": [{"root": r["root"], "branch": r["branch"], "dirty": len(r["dirty"]),
                       "dirty_total": r["dirty_total"], "commits": len(r["commits"]), "error": r["error"]}
                      for r in repos[:MAX_REPOS]],
            "repos_omitted": data.get("repos_omitted", 0) + max(0, len(repos) - MAX_REPOS),
            "agents": dict(sorted(roles.items())), "agents_omitted": data.get("agents_omitted", 0),
            "files": len(data.get("files", [])), "files_omitted": data.get("files_omitted", 0),
            "outside_repo_count": data.get("outside_repo_count", 0),
            # Collection health: valid events folded so far (None when unknown) and unreadable lines.
            "events_captured": data.get("events_captured"), "spool_errors": data.get("spool_errors", 0)}


def recent_files(data, limit=None):
    """Repo file paths of the stored facts, most recently edited first."""
    files = sorted(data.get("files", []), key=lambda f: (f["last_ts"], f["path"]), reverse=True)
    return [f["path"] for f in files][:limit]


def since(data, after):
    """Activity of the stored facts after epoch `after` (None: no record, so all of it).

    Returns dispatches (oldest first), per-repo commits (git order, newest first), the count
    of commits without a time (facts written before commit times were kept) and the newest
    captured activity: last_change_at or the newest dispatch, whichever is later.
    """
    def later(ts):
        return after is None or (type(ts) is int and ts > after)

    agents = sorted(data.get("agents", []), key=lambda a: a["ts"])
    dispatches = [a for a in agents if later(a["ts"])]
    repos, undated = [], 0
    for repo in data.get("repos", [])[:MAX_REPOS]:
        commits = [c for c in repo["commits"] if later(c.get("ts"))]
        if after is not None:
            undated += sum(1 for c in repo["commits"] if type(c.get("ts")) is not int)
        if commits:
            repos.append({"root": repo["root"], "commits": commits})
    stamps = [a["ts"] for a in agents if type(a["ts"]) is int]
    if data.get("last_change_at"):
        stamps.append(_epoch(data["last_change_at"]))
    return {"dispatches": dispatches, "repos": repos, "commits": sum(len(r["commits"]) for r in repos),
            "undated": undated, "latest": max(stamps) if stamps else None}


def _unread(path, recorded):
    """(complete lines past the offset facts.json recorded for this spool file, their end offset)."""
    data = path.read_bytes()
    start = recorded.get(path.name, 0) if isinstance(recorded, dict) else 0
    start = min(start, len(data)) if type(start) is int and start > 0 else 0
    end = max(start, data.rfind(b"\n") + 1)
    return data[start:end], end


def _pending(cache, session, recorded):
    """Spool lines not folded yet (claimed leftovers past their offsets, then the live file). Read-only."""
    live = cache / "facts" / (session + ".jsonl")
    guard(cache, live)
    if not live.parent.is_dir():
        return b""
    chunks = []
    for path in [*sorted(live.parent.glob(live.name + ".folding-*")), live]:
        guard(cache, path)
        try:
            chunks.append(_unread(path, recorded)[0])
        except OSError:  # gone (a fold claimed it meanwhile) or held by an appender
            continue
    return b"".join(chunks)


def changes(config_path, root, session, *, now=None):
    """Read-only view of what the session changed: stored facts merged with the spool lines
    not folded yet, plus a fresh git query. Writes neither facts.json nor any spool file."""
    ws = as_workspace(root)
    previous, path = stored(ws, session)
    previous = previous or {}
    raw = b""
    if SPOOL_SESSION.fullmatch(session):
        raw = _pending(Path(config_path).parent / "cache", session, previous.get("spool_consumed"))
    events, invalid = _parse(raw)
    git = _Git()
    state = gather(previous, events, invalid, _Finder(git))
    body = summarize(ws, session, state, git, int(time.time()) if now is None else now)
    body.pop("spool_consumed", None)
    return {"workspace": str(ws.root), "session_id": session, "facts_path": str(path),
            "folded_at": previous.get("updated_at"), "pending_events": len(events), "pending_invalid": invalid,
            "facts": body}


class _Spool:
    """Claim and release of one session's spool, safe against the async appender.

    native-facts appends lines with `>>` and cannot take a lock. claim() renames the
    live file aside (<session>.jsonl.folding-<token>), so later appends start a new
    live file, and consumes only complete lines. release() runs once facts.json is
    durable: bytes that reached a claimed file after it was read (an appender that
    opened it before the rename) are moved back to the live spool and the claimed
    file is removed. A claimed file left by an interrupted fold is resumed from the
    byte offset facts.json recorded for it (spool_consumed): never lost, never merged twice.
    """

    def __init__(self, cache, session):
        self.cache = cache
        self.live = cache / "facts" / (session + ".jsonl")
        self.claimed = []

    def claim(self, recorded):
        guard(self.cache, self.live)
        self.live.parent.mkdir(parents=True, exist_ok=True)
        paths = sorted(self.live.parent.glob(self.live.name + ".folding-*"))
        fresh = self.live.with_name(self.live.name + ".folding-" + uuid.uuid4().hex)
        for _ in range(RENAME_ATTEMPTS):
            try:
                os.replace(self.live, fresh)
                paths.append(fresh)
                break
            except FileNotFoundError:
                break
            except PermissionError:  # Windows: an appender holds it; it closes within microseconds
                time.sleep(0.05)
        chunks = []
        for path in paths:
            guard(self.cache, path)
            chunk, end = _unread(path, recorded)
            chunks.append(chunk)
            self.claimed.append((path, end))
        return b"".join(chunks)

    def consumed(self):
        return {path.name: end for path, end in self.claimed}

    def release(self):
        for path, end in self.claimed:
            try:
                tail = path.read_bytes()[end:]
                path.unlink()
            except OSError:
                continue  # kept; the next fold resumes it at the recorded offset
            if tail:
                try:
                    with self.live.open("ab") as stream:
                        stream.write(tail)
                except OSError:
                    pass


def fold(config_path, root, session, *, now=None):
    """Merge the session's spool into facts.json, refresh git facts, then truncate the spool.

    Store problems raise StoreError; git problems only mark the repo entry.
    Assumes a single folder per session: only PreCompact and the SessionStart(compact) that
    follows it fold (changes() is read-only); Claude Code runs them one after the other and
    never two compactions of one session at once. The facts.json write is
    locked, but the spool claim/release is not, so concurrent folds of one session could merge
    a claimed spool file twice.
    """
    ws = as_workspace(root)
    target = session_dir(ws, session) / "facts.json"
    guard(handoff_home(), target)
    now = int(time.time()) if now is None else now
    previous = _previous(ws, session, target)
    spool = None
    raw = b""
    if SPOOL_SESSION.fullmatch(session):
        spool = _Spool(Path(config_path).parent / "cache", session)
        raw = spool.claim(previous.get("spool_consumed"))
    events, invalid = _parse(raw)
    git = _Git()
    state = gather(previous, events, invalid, _Finder(git))
    state["spool_consumed"] = spool.consumed() if spool else {}
    data = _seal(summarize(ws, session, state, git, now))
    with _lock(ws, target.parent):
        atomic_write(handoff_home(), target, data)
    if spool:
        spool.release()
    return data
