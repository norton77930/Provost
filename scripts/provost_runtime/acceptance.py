"""Acceptance gates: the synchronous Stop check behind hooks/acceptance-gate.

A main session that wrote an acceptance file for its task (<cwd>/.claude/acceptance/*.json, schema in
<runtime root>/docs/native-handoff.md, "Acceptance gates"; the file is the session's own when its transcript
holds a successful Write, Edit or NotebookEdit of it, see session_files()) cannot end its turn while a non-human gate is pending,
failed or stale. A verdict covers content, not a time: a pass is stale when its files no longer match its
digest, or when the code tree differs from its tree (tree_digest()); without either, a machine, live or visual
pass goes by the time rule (older than the last main-session edit), and a review pass by any main-session edit;
see unmet_state(). check() returns a block decision naming every unmet gate id and its file. Without a session
acceptance file, with only closed ones (session_files() drops those before it opens the transcript), or with
only human gates unmet, it returns {}. A malformed file is named
in the block reason, so a typo cannot switch enforcement off unnoticed. A subagent's Stop, running background work
and a stated blocker line end the turn, it blocks at most three times per episode, and it does not repeat an
unchanged stale block after the person's prompt (see check()).
Any internal error returns {}: a broken check must never trap a session.
"""
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

DIRECTORY = (".claude", "acceptance")
PUSH_BUDGET = 3        # blocks per episode; the next unmet Stop ends the turn
COUNTER_DIR = "acceptance"  # under <config dir>/cache, beside facts/ and notifications/
COUNTER_SUFFIX = ".pushes"  # hooks/native-facts empties <session>.pushes on the person's prompt
SEEN_SUFFIX = ".seen"  # <session>.seen holds a hash of the unmet state that last blocked
SESSION = re.compile(r"^[A-Za-z0-9-]+$")  # store.CACHE_SESSION: ids the cache files may be named by
# A blocker line: optional bullet (- or *), quote (>) and bold (** or __) before `needs input:` or `failed:`.
BLOCKER = re.compile(r"^[ \t]*(?:[-*][ \t]+|>[ \t]*)*(?:\*\*|__)?(?:needs input|failed)(?:\*\*|__)?:",
                     re.IGNORECASE | re.MULTILINE)
KINDS = ("machine", "live", "visual", "review", "human")
STATUSES = ("pass", "fail", "pending")
OPTIONAL = ("ranAt", "evidence", "source")
FILE_LIMIT = 1024 * 1024           # an acceptance file larger than this is reported as malformed
EDIT_SCAN_BYTES = 8 * 1024 * 1024  # most transcript bytes (the newest) read to find the last edit
STALE_KINDS = ("machine", "live", "visual")  # a pass of these goes stale after an edit
FUTURE_SLACK = timedelta(minutes=5)  # a zoned ranAt further ahead of now than this is a mislabelled local time
SHORT_DIGEST = 12  # characters of a digest named in a stale reason
TREE_BUDGET = 15  # seconds for all git calls of one tree_digest(); the hook's own timeout is 30
TREE_GRACE = 1  # tree_digest() answers None at most TREE_BUDGET + TREE_GRACE seconds in, even if git's thread is stuck
DOC_SUFFIXES = (".md", ".mdx", ".markdown", ".rst", ".txt")  # not code, tracked or not (lower case)
ARTIFACT_SUFFIXES = (".log", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".webm", ".mp4")  # untracked: a check's output
CONFLICTS = ("DD", "AU", "UD", "UA", "DU", "AA", "UU")  # the unmerged XY codes of `git status --porcelain`
GITLINK_MODE = b"160000"  # a submodule's index entry
NO_COMMITS = (b"## No commits yet", b"## Initial commit")  # the status --branch header of a repo without commits
# Cheap byte filter before parsing a line for last_edit(): an edit tool call or a failed tool result.
EDIT_TOKENS = (b'"Edit"', b'"Write"', b'"NotebookEdit"', b'"is_error"')
# The runtime root is the parent of scripts/, in the repo checkout and in the installed runtime alike.
GUIDE = "%s, section Acceptance gates" % (Path(__file__).resolve().parents[2] / "docs" / "native-handoff.md")


class Malformed(ValueError):
    """An acceptance file that does not match the schema; the message names the first problem."""


def check(event, config_path=None):
    """Stop (sync): {"decision": "block", "reason": ...} while an open session acceptance file (one without
    a top-level closed reason) has an unmet gate, or any session acceptance file is malformed, else {}.
    Never raises.

    Exempt, with no decision and no push spent: a subagent's Stop (agent_id), running background work
    (a non-empty background_tasks) and a stated blocker (a `needs input:` or `failed:` line in
    last_assistant_message). At most PUSH_BUDGET blocks per episode; the per-session counter lives in
    <config dir>/cache/acceptance/<session>.pushes (config_path is the Provost config file). A Stop with no
    unmet gate deletes it; hooks/native-facts empties it on the person's next prompt. A counter that cannot
    be read or written ends the turn: an uncountable push could loop forever.

    An unchanged stale block is not repeated across the person's prompts: every block writes a hash of the
    unmet state to <session>.seen, and a Stop whose unmet items are all stale, with that same hash and a
    counter reading zero (a new episode since that block) is exempt too. Pending and failed gates and malformed
    files always block. A clean Stop deletes the file; one that cannot be read or written only loses the dedupe."""
    try:
        if "agent_id" in event:  # only the main session's turn end is gated
            return {}
        directory = acceptance_directory(event)
        if directory is None:
            return {}
        files = session_files(event, directory)
        if files is None:  # unknown transcript: whose files these are cannot be told, so no decision
            return {}
        unmet, malformed, memo, tree_memo = [], [], [], []

        def edited():  # read the transcript once, and only when a passed gate could be stale
            if not memo:
                try:
                    memo.append(last_edit(event))
                except Exception:  # unreadable edit history: unknown, so nothing is stale
                    memo.append(None)
            return memo[0]

        def tree():  # run git once, and only when a passed gate carries a tree
            if not tree_memo:
                tree_memo.append(tree_digest(event["cwd"]))
            return tree_memo[0]

        for path in files:
            try:
                data = load(path)
            except FileNotFoundError:  # deleted since the listing
                continue
            except Malformed as exc:
                malformed.append((path, str(exc)))
                continue
            if "closed" in data:  # finished work: load() guarantees the reason
                continue
            held = [(item["id"], state) for item in data["gates"]
                    for state in (unmet_state(item, edited, event["cwd"], tree),) if state]
            if held:
                unmet.append((path, held))
        counter = _counter(event, config_path)
        seen = None if counter is None else counter.with_suffix(SEEN_SUFFIX)
        if not unmet and not malformed:  # a clean state ends the episode
            for leftover in (counter, seen):
                if leftover is not None:
                    try:
                        leftover.unlink(missing_ok=True)
                    except OSError:
                        pass
            return {}
        if event.get("background_tasks") or _blocker(event.get("last_assistant_message")):
            return {}
        key = _state_key(unmet, malformed)
        if _already_seen(unmet, malformed, key, counter, seen):
            return {}
        push = _spend(counter)
        if push is None:  # budget spent, or the counter is unusable
            return {}
        _remember(seen, key)
        return {"decision": "block", "reason": _reason(unmet, malformed, push)}
    except Exception:  # fail-soft: the turn ends
        return {}


def _blocker(message):
    return isinstance(message, str) and BLOCKER.search(message) is not None


def _counter(event, config_path):
    """The session's push counter file, or None when the session id or config path is unusable."""
    session = event.get("session_id")
    if config_path is None or not isinstance(session, str) or not SESSION.fullmatch(session):
        return None
    cache = Path(config_path).parent / "cache"
    return cache.joinpath(COUNTER_DIR, session + COUNTER_SUFFIX)


def _state_key(unmet, malformed):
    """A hash of everything a block reports but the clock of the block itself: stale states name the current
    digest or edit time, so it changes whenever the content under a stale gate does."""
    state = [[[str(path), held] for path, held in unmet], [[str(path), problem] for path, problem in malformed]]
    return hashlib.sha256(json.dumps(state, ensure_ascii=True).encode("ascii")).hexdigest()


def _already_seen(unmet, malformed, key, counter, seen):
    """True when this all-stale state already blocked and the person has prompted since (the counter reads
    zero), so a repeat would only ask again what the last block asked. A `.seen` that cannot be read means no."""
    from .store import encoded
    if malformed or seen is None or _pushes(counter) != 0:
        return False
    if not all(state.startswith("stale") for _, held in unmet for _, state in held):
        return False
    try:
        return seen.read_bytes() == encoded(key)
    except OSError:
        return False


def _remember(seen, key):
    """Record the state of this block; a failure only loses the dedupe."""
    from .store import StoreError, atomic_write
    if seen is not None:
        try:
            atomic_write(seen.parent.parent, seen, key)
        except (StoreError, OSError):
            pass


def _pushes(counter):
    """The pushes counted in this episode; None when the counter holds no count or cannot be read."""
    try:
        text = counter.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return 0
    except (OSError, UnicodeError):
        return None
    if not text:  # missing, or emptied by the person's prompt
        return 0
    return int(text) if text.isdigit() else None


def _spend(counter):
    """Count one more push in this episode and return its number (1..PUSH_BUDGET); None when the budget is
    spent or the counter cannot be read or written atomically."""
    from .store import StoreError, atomic_write
    if counter is None:
        return None
    pushes = _pushes(counter)
    if pushes is None or pushes >= PUSH_BUDGET:
        return None
    try:
        atomic_write(counter.parent.parent, counter, pushes + 1)
    except (StoreError, OSError):
        return None
    return pushes + 1


def unmet_state(gate, edited=None, cwd=None, tree=None):
    """Why a gate holds the turn ("pending", "fail" or "stale: ..."), or None when it does not; human gates
    never do.

    A verdict covers content. Any passed gate but a human one with files and digest is judged by them alone:
    it is stale when digest() of its files (relative ones read from cwd, the event cwd; spelling and order do
    not matter, see canonical_files()) no longer matches,
    whoever changed them: a subagent, Bash or the main session; an unreadable or deleted file counts as
    changed; ranAt is not used.

    Without them, a machine, live or visual gate with a tree field is compared with tree, the current
    tree_digest() of the work tree (or a callable returning it, called only for such a gate): equal is fresh
    and different is stale, regardless of ranAt. When tree is None (unknown) the gate is stale too: the time rule
    cannot see a subagent's edits, so it must not vouch for a gate git cannot confirm. A review gate ignores
    the field.

    The time rule: edited is the session's last main-session edit (an aware datetime, see last_edit()), or a
    callable returning it, called only for a gate that could be stale. A machine, live or visual gate with
    status pass is stale when its ranAt is before edited, or when it has no usable ranAt (missing, or without a
    time zone, which cannot be compared with the transcript's UTC) and edited is not None. Independent of
    any edit, a zoned ranAt more than FUTURE_SLACK after the current UTC time is stale too: it is local time
    labelled UTC (Windows PowerShell's `date -u`), which would always look later than the last edit.
    A review gate without files and digest is stale whenever edited is not None, like a machine gate without
    ranAt (its ranAt is not used). Human gates are never stale."""
    if gate["kind"] == "human":
        return None
    if gate["status"] in ("pending", "fail"):
        return gate["status"]
    if "files" in gate:  # load() guarantees digest beside it
        return _files_changed(gate, cwd)
    if gate["kind"] == "review":
        if callable(edited):
            edited = edited()
        return None if edited is None else "stale: review gate has no files/digest, last edit at %s" % _clock(edited)
    if gate["kind"] not in STALE_KINDS:
        return None
    if "tree" in gate:
        current = tree() if callable(tree) else tree
        if current == gate["tree"]:
            return None
        if current is None:
            return "stale: the code's current state is unknown (git gave no usable answer)"
        return "stale: code changed since the check (now %s)" % current[:SHORT_DIGEST]
    ran = _when(gate.get("ranAt"))
    if ran is not None and _zoned(gate["ranAt"]) and ran > datetime.now(timezone.utc) + FUTURE_SLACK:
        return "stale: ranAt %s is later than now, likely local time labelled UTC" % _clock(ran)
    if callable(edited):
        edited = edited()
    if edited is None:
        return None
    if ran is None:
        return "stale: no ranAt, last edit at %s" % _clock(edited)
    if not _zoned(gate["ranAt"]):
        return "stale: ranAt has no time zone, last edit at %s" % _clock(edited)
    if ran < edited:
        return "stale: ranAt %s is before the last edit at %s" % (_clock(ran), _clock(edited))
    return None


def _files_changed(gate, cwd):
    """The stale reason of a passed gate whose files no longer match its digest, else None. It carries a
    prefix of the files' current digest, so that the reason changes whenever their content does (see check())."""
    reason = ("stale: reviewed files changed since the review" if gate["kind"] == "review"
              else "stale: files changed since the check")
    try:
        current = digest(gate["files"], cwd)
    except OSError as exc:  # deleted or unreadable: changed, and named
        return "%s (%s unreadable, now %s)" % (reason, exc.filename or "a file", _readable_digest(gate["files"], cwd))
    except Exception:
        # Fail-open, as for an unreadable edit history: a digest that cannot be computed for any other
        # reason is an unknown state, and an unknown state never holds the turn.
        return None
    return None if current == gate["digest"] else "%s (now %s)" % (reason, current[:SHORT_DIGEST])


def _readable_digest(files, cwd):
    """A short digest of those of files that can still be read: what `now` says when one of them cannot."""
    entries = []
    for name in files:
        try:
            entries.append(digest([name], cwd))
        except OSError:
            entries.append(None)
    return hashlib.sha256(json.dumps(entries).encode("ascii")).hexdigest()[:SHORT_DIGEST]


def last_edit(event):
    """When the main session last edited a file (aware datetime), or None when it did not or it is unknown.

    Source: the session transcript, not the facts layer. The facts spool (cache/facts) exists only with
    handoff on and is filled by an async PostToolUse hook that can lag this synchronous Stop; the transcript
    is always there and already holds every tool call before the Stop fires. Counted, mirroring
    hooks._scan_lines: an Edit, Write or NotebookEdit tool_use on a main-session (non-sidechain) line whose
    tool_result did not come back as an error, on a path that is neither facts.OUTSIDE nor under
    .claude/acceptance/ (writing the acceptance file is not a code change); its time is the tool_use line's
    timestamp. Subagent edits live in their own sidechain transcripts, so they do not count; a gate the
    main session re-runs after a subagent's work still records a fresh ranAt. Only the newest
    EDIT_SCAN_BYTES are read: an edit older than that is not seen (fail-open, the gate passes)."""
    from . import facts
    from .hooks import EDIT_TOOLS, resolve_transcript  # hooks imports this module
    path = resolve_transcript(event.get("transcript_path"))
    if not isinstance(path, str) or not path:
        return None
    with open(path, "rb") as handle:
        size = handle.seek(0, 2)
        start = max(0, size - EDIT_SCAN_BYTES)
        handle.seek(start)
        lines = handle.read().split(b"\n")
    if start:
        lines = lines[1:]  # the first line was cut by the tail
    edits, failed = {}, set()
    for raw in lines:
        if not any(token in raw for token in EDIT_TOKENS):
            continue
        try:
            line = json.loads(raw)
            content = line["message"]["content"]
        except (ValueError, KeyError, TypeError):
            continue
        if line.get("isSidechain") or not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if line.get("type") == "assistant" and block.get("type") == "tool_use" and block.get("name") in EDIT_TOOLS:
                tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                target = tool_input.get(EDIT_TOOLS[block["name"]])
                when = _when(line.get("timestamp"))
                if (isinstance(target, str) and target and when is not None and not facts._outside(target)
                        and not _acceptance_path(target)):
                    edits[block.get("id")] = when
            elif line.get("type") == "user" and block.get("type") == "tool_result" and block.get("is_error"):
                failed.add(block.get("tool_use_id"))
    times = [when for call, when in edits.items() if call not in failed]
    return max(times) if times else None


def canonical_files(files, cwd=None):
    """The files of a gate as a set in one spelling: each name made absolute against cwd (the process cwd when
    None) and normalised, then de-duplicated and sorted. The only place that spelling is decided: digest() hashes
    it and the `digest` command prints it, so the Stop check and the command cannot disagree. The case of a
    name is kept (no normcase), so a digest the command printed earlier stays valid."""
    base = os.path.abspath(cwd) if cwd else os.getcwd()
    return sorted({os.path.normpath(os.path.join(base, name)) for name in files})


def digest(files, cwd=None):
    """The digest of a review gate's files (64 hex characters); OSError when one cannot be read.

    sha256 over the JSON list of [name, sha256 of the file's bytes with CRLF read as LF], where the names are
    canonical_files(files, cwd): absolute against cwd, normalised, de-duplicated and sorted. So the same files
    spelled relatively or absolutely, or listed in another order, have the same digest, while adding, removing or
    replacing a file changes it. Line endings are ignored so a CRLF checkout of the same text does not count
    as a change."""
    entries = []
    for name in canonical_files(files, cwd):
        with open(name, "rb") as handle:
            body = handle.read().replace(b"\r\n", b"\n")
        entries.append([name, hashlib.sha256(body).hexdigest()])
    return hashlib.sha256(json.dumps(entries, ensure_ascii=True, separators=(",", ":")).encode("ascii")).hexdigest()


def tree_digest(cwd):
    """The content identity of the code in cwd's git work tree (64 hex characters), or None when it is unknown:
    not a work tree, no git, a git failure, timeout or empty answer, a file that vanished while hashing, or a new
    or modified file whose name holds a newline. Never raises, never writes to the repository.

    sha256 of the compact ASCII JSON of the sorted [path, blob id] pairs, from the repo top: the index's blobs,
    overlaid with the work tree's dirty files as read by `git status`. A worktree-deleted path drops out; a
    modified, conflicted or untracked one gets the id `git hash-object` gives it, filters applied and nothing
    written, which is the blob the same content has once committed: a check run on a dirty tree stays fresh
    after the commit. No file time is read. Left out: docs (DOC_SUFFIXES, whatever their state), anything under
    .claude/acceptance/, untracked files with ARTIFACT_SUFFIXES (a check's own output must not stale it) and
    nested repos. A submodule counts by the commit its index entry records, not by its own work tree.

    Answers within TREE_BUDGET + TREE_GRACE seconds, whatever git or its children do: the work runs in a daemon
    thread that is joined for that long, and None is the answer when it is still running. On Windows
    subprocess.run waits for the output pipe to close after killing git, so a child of git holding the pipe can
    outlive the timeout; such a thread is abandoned until the process exits (it never blocks the exit)."""
    answer = []

    def work():
        try:
            answer.append(_tree_digest(cwd))
        except Exception:
            pass
    try:
        thread = threading.Thread(target=work, name="tree-digest", daemon=True)
        thread.start()
        thread.join(TREE_BUDGET + TREE_GRACE)
    except Exception:
        return None
    return answer[0] if answer and not thread.is_alive() else None


def _tree_digest(cwd):
    deadline = time.monotonic() + TREE_BUDGET

    def git(directory, *args, stdin=None):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        return subprocess.run(["git", "-C", directory, *args], input=stdin, capture_output=True, check=True,
                              timeout=remaining, env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"),
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout

    # A loaded Windows machine was seen to have git exit 0 with an empty stdout. Read as "nothing is dirty" or
    # "nothing is tracked" that would be a wrong digest, not an unknown one, so an empty answer is checked for:
    # --branch makes status always print its ## header, and only a repo without commits has an empty index
    # (one whose commits track nothing, which is rare, then reads as unknown). -z ends every record with a NUL,
    # so output without a final one was cut short.
    top = _git_path(git(cwd, "rev-parse", "--show-toplevel").rstrip(b"\r\n"))
    if not top:
        return None
    index = git(top, "ls-files", "-s", "-z")
    raw_status = git(top, "--no-optional-locks", "status", "--porcelain=v1", "-z", "--branch",
                     "--untracked-files=all", "--no-renames")
    if not raw_status.endswith(b"\0") or (index and not index.endswith(b"\0")):
        return None
    status = raw_status.split(b"\0")
    if not status[0].startswith(b"## ") or not (index or status[0].startswith(NO_COMMITS)):
        return None
    blobs, gitlinks = {}, set()
    for entry in index.split(b"\0"):
        if entry:
            meta, _, name = entry.partition(b"\t")
            mode, blob, _ = meta.split(b" ")
            path = _git_path(name)
            if not _left_out(path):
                blobs[path] = blob.decode("ascii")
                if mode == GITLINK_MODE:
                    gitlinks.add(path)
    dirty = []
    for entry in status[1:]:
        code, path = entry[:2].decode("ascii"), _git_path(entry[3:])
        if not path or path.endswith("/") or _left_out(path):
            continue
        if code == "??":
            if not path.lower().endswith(ARTIFACT_SUFFIXES):
                dirty.append(path)
        elif code in CONFLICTS:
            if os.path.lexists(os.path.join(top, path)):
                dirty.append(path)
            else:
                blobs.pop(path, None)
        elif code[1] == "D":
            blobs.pop(path, None)
        elif code[1] != " " and path not in gitlinks:
            dirty.append(path)
    if any("\n" in path for path in dirty):  # --stdin-paths reads one path per line
        return None
    if dirty:
        names = b"".join(path.encode("utf-8", "surrogateescape") + b"\n" for path in dirty)
        ids = git(top, "hash-object", "--stdin-paths", stdin=names).decode("ascii").split()
        if len(ids) != len(dirty):
            return None
        blobs.update(zip(dirty, ids))
    pairs = sorted([path, blob] for path, blob in blobs.items())
    return hashlib.sha256(json.dumps(pairs, ensure_ascii=True, separators=(",", ":")).encode("ascii")).hexdigest()


def _git_path(raw):
    """A path as git printed it: UTF-8, with any other byte kept reversibly (a name need not be valid UTF-8)."""
    return raw.decode("utf-8", "surrogateescape")


def _left_out(path):
    """True for a path no tree digest covers: a doc, or an acceptance file."""
    return path.lower().endswith(DOC_SUFFIXES) or _acceptance_path(path)


def _acceptance_path(path):
    return "/.claude/acceptance/" in "/" + path.replace("\\", "/").lower()


def _clock(when):
    """Local wall-clock time for a reason line."""
    return when.astimezone().strftime("%H:%M:%S")


def acceptance_directory(event):
    """<cwd>/.claude/acceptance of the event as a Path; None without an absolute cwd or without the directory."""
    cwd = event.get("cwd")
    if not isinstance(cwd, str) or not cwd or not Path(cwd).is_absolute():
        return None
    directory = Path(cwd).joinpath(*DIRECTORY)
    return directory if directory.is_dir() else None


def session_files(event, directory):
    """The open *.json files in directory that this session wrote, by name; None when the transcript is unknown.
    A file that loads cleanly with a valid top-level `closed` reason is finished and left out before the
    transcript is read; one that is malformed (a bad `closed` too), unreadable or gone stays a candidate, so a
    malformed file the session owns still blocks. A directory with no *.json file, or with only finished
    ones, answers [] without resolving or opening the transcript, which is the common case.

    A file belongs to the session when its transcript holds a main-session (non-sidechain) Write, Edit or
    NotebookEdit tool_use on that file whose tool_result did not come back as an error. Not the file's
    modification time against the session start: two sessions running in one cwd share this directory, and
    the later one would be gated by the other's pending file, modified after it started. The acceptance file
    is written with the Write tool (see the guide), so a Bash or PowerShell write does not count.

    The whole transcript is read, not only its tail: the file is usually written early in the task. A resume
    or compaction keeps the session id and appends to the same transcript, so a resumed session keeps its
    files; /clear starts a new transcript and drops them. Only lines that mention `acceptance` or carry an
    `is_error` result are parsed, which keeps a large transcript cheap. A tool path is matched as written:
    relative paths resolve against the event cwd, and Windows ignores case and slash direction. Only files
    directly in `directory` count, so a worktree's acceptance directory stays apart from the checkout's."""
    from .hooks import EDIT_TOOLS, resolve_transcript  # hooks imports this module
    candidates = []
    for candidate in sorted(directory.glob("*.json")):
        try:
            if candidate.is_file() and not _finished(candidate):
                candidates.append(candidate)
        except OSError:
            continue
    if not candidates:  # nothing to own: leave the (possibly huge) transcript unread
        return []
    path = resolve_transcript(event.get("transcript_path"))
    if not isinstance(path, str) or not path:
        return None
    try:
        handle = open(path, "rb")
    except OSError:
        return None
    cwd = event.get("cwd") if isinstance(event.get("cwd"), str) else ""
    written, failed = {}, set()
    with handle:
        for raw in handle:
            if b"acceptance" not in raw.lower() and b'"is_error"' not in raw:
                continue
            try:
                line = json.loads(raw)
                content = line["message"]["content"]
            except (ValueError, KeyError, TypeError):
                continue
            if line.get("isSidechain") or not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if line.get("type") == "assistant" and block.get("type") == "tool_use" and block.get("name") in EDIT_TOOLS:
                    tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                    target = tool_input.get(EDIT_TOOLS[block["name"]])
                    if isinstance(target, str) and target:
                        written[block.get("id")] = _key(target, cwd)
                elif line.get("type") == "user" and block.get("type") == "tool_result" and block.get("is_error"):
                    failed.add(block.get("tool_use_id"))
    owned = {key for call, key in written.items() if call not in failed}
    return [candidate for candidate in candidates if _key(str(candidate), "") in owned]


def _finished(path):
    """True when the file loads cleanly and carries a valid `closed` reason: finished work, which neither blocks
    nor counts as the task a brief must cite, so its ownership is not worth a transcript read. A file that is
    malformed (a bad `closed` included), unreadable or gone is not finished here: check() reports it.
    Any exception counts as not finished (a deeply nested file makes json raise RecursionError): a file this
    session does not own must not switch the gate off for the others, so ownership decides as before."""
    try:
        return "closed" in load(path)
    except Exception:
        return False


def _key(target, cwd):
    """A path in comparable form: made absolute against cwd, normalised, case-folded on Windows."""
    if cwd and not os.path.isabs(target):
        target = os.path.join(cwd, target)
    return os.path.normcase(os.path.normpath(target))


def load(path):
    """The acceptance file as a dict; Malformed names the first schema problem (never quotes content)."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(FILE_LIMIT + 1)
    except FileNotFoundError:
        raise
    except OSError:
        raise Malformed("unreadable") from None
    if len(raw) > FILE_LIMIT:
        raise Malformed("larger than 1 MB")
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except ValueError:  # includes UnicodeDecodeError
        raise Malformed("not valid UTF-8 JSON") from None
    if not isinstance(data, dict):
        raise Malformed("not a JSON object")
    if not isinstance(data.get("task"), str):
        raise Malformed("task must be a string")
    if "closed" in data and not (isinstance(data["closed"], str) and data["closed"].strip()):
        raise Malformed("closed must be a non-empty string")
    gates = data.get("gates")
    if not isinstance(gates, list):
        raise Malformed("gates must be a list")
    for index, item in enumerate(gates):
        where = "gates[%d]" % index
        if not isinstance(item, dict):
            raise Malformed(where + " must be an object")
        if not isinstance(item.get("id"), str) or not item["id"].strip():
            raise Malformed(where + ".id must be a non-empty string")
        if item.get("kind") not in KINDS:
            raise Malformed(where + ".kind must be one of " + ", ".join(KINDS))
        if not isinstance(item.get("check"), str):
            raise Malformed(where + ".check must be a string")
        if item.get("status") not in STATUSES:
            raise Malformed(where + ".status must be one of " + ", ".join(STATUSES))
        for key in OPTIONAL:
            if key in item and not isinstance(item[key], str):
                raise Malformed("%s.%s must be a string" % (where, key))
        if "ranAt" in item and _when(item["ranAt"]) is None:
            raise Malformed(where + ".ranAt must be an ISO 8601 time")
        if item["kind"] in STALE_KINDS and "tree" in item and not isinstance(item["tree"], str):
            raise Malformed(where + ".tree must be a string (runtime digest --tree prints it)")
        if item["kind"] != "human" and ("files" in item or "digest" in item):
            if "files" not in item or "digest" not in item:
                raise Malformed(where + ".files and .digest go together (runtime digest prints both)")
            files = item["files"]
            if not isinstance(files, list) or not files or not all(isinstance(name, str) and name for name in files):
                raise Malformed(where + ".files must be a non-empty list of paths")
            if not isinstance(item["digest"], str):
                raise Malformed(where + ".digest must be a string")
    return data


def _zoned(text):
    """True when ISO 8601 text carries a zone or offset (call only with text _when() parsed)."""
    return datetime.fromisoformat(text).tzinfo is not None


def _when(text):
    """An aware datetime from ISO 8601 text (naive counts as UTC), or None."""
    try:
        when = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _reason(unmet, malformed, push):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = ["Acceptance gates are not passed, so this turn cannot end yet (push %d/%d; after %d the turn "
             "ends and waits for the owner)." % (push, PUSH_BUDGET, PUSH_BUDGET),
             "If you are blocked, say so on its own line starting with `needs input:` or `failed:`."]
    if unmet:
        lines.append("Run each unmet gate's check and update its status, evidence and ranAt in the file; a "
                     "stale gate passed before the last edit, has a ranAt without a time zone, or has a ranAt later than now, and must be "
                     "re-run; a stale review gate needs a re-review of the changed files and fresh files and "
                     "digest from the runtime `digest` command (human gates are the owner's decision and never "
                     "hold the turn). Stamp `tree` from the runtime `digest --tree`, read before the check starts; "
                     "when the code's current state is unknown, rerun `digest --tree`, and if git still fails, use "
                     "`files` and `digest` or say so on a `needs input:` line; "
                     "a finished task's file takes a top-level `closed` reason. ranAt must be "
                     "the clock read from a command, in UTC: now %s; Bash `date -u +%%Y-%%m-%%dT%%H:%%M:%%SZ`, "
                     "PowerShell `(Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')` (its `date -u` "
                     "prints local time labelled Z, which is stale). Guide: %s. Unmet gates:" % (now, GUIDE))
        lines += ["- %s: %s" % (path, ", ".join("%s (%s)" % held for held in gates)) for path, gates in unmet]
    if malformed:
        lines.append("Malformed acceptance files; fix each to the schema (%s):" % GUIDE)
        lines += ["- %s: %s" % (path, problem) for path, problem in malformed]
    return "\n".join(lines)
