"""Acceptance gates: the synchronous Stop check behind hooks/acceptance-gate.

A main session that wrote an acceptance file for its task (<cwd>/.claude/acceptance/*.json, schema in
docs/native-handoff.md, "Acceptance gates") cannot end its turn while a non-human gate is pending or
failed: check() returns a block decision naming every unmet gate id and its file. Without a session
acceptance file, or with only human gates unmet, it returns {}. A malformed file is named in the block
reason, so a typo cannot switch enforcement off unnoticed. A subagent's Stop, running background work
and a stated blocker line end the turn, and it blocks at most three times per episode (see check()).
Any internal error returns {}: a broken check must never trap a session.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

DIRECTORY = (".claude", "acceptance")
PUSH_BUDGET = 3        # blocks per episode; the next unmet Stop ends the turn
COUNTER_DIR = "acceptance"  # under <config dir>/cache, beside facts/ and notifications/
COUNTER_SUFFIX = ".pushes"  # hooks/native-facts empties <session>.pushes on the person's prompt
SESSION = re.compile(r"^[A-Za-z0-9-]+$")  # store.CACHE_SESSION: ids the cache files may be named by
# A blocker line: optional bullet (- or *), quote (>) and bold (** or __) before `needs input:` or `failed:`.
BLOCKER = re.compile(r"^[ \t]*(?:[-*][ \t]+|>[ \t]*)*(?:\*\*|__)?(?:needs input|failed)(?:\*\*|__)?:",
                     re.IGNORECASE | re.MULTILINE)
KINDS = ("machine", "live", "visual", "review", "human")
STATUSES = ("pass", "fail", "pending")
OPTIONAL = ("ranAt", "evidence", "source")
FILE_LIMIT = 1024 * 1024           # an acceptance file larger than this is reported as malformed
START_SCAN_BYTES = 8 * 1024 * 1024  # most transcript bytes read to find the session start
GUIDE = "docs/native-handoff.md, section Acceptance gates"


class Malformed(ValueError):
    """An acceptance file that does not match the schema; the message names the first problem."""


def check(event, config_path=None):
    """Stop (sync): {"decision": "block", "reason": ...} while a session acceptance file has an unmet gate
    or is malformed, else {}. Never raises.

    Exempt, with no decision and no push spent: a subagent's Stop (agent_id), running background work
    (a non-empty background_tasks) and a stated blocker (a `needs input:` or `failed:` line in
    last_assistant_message). At most PUSH_BUDGET blocks per episode; the per-session counter lives in
    <config dir>/cache/acceptance/<session>.pushes (config_path is the Provost config file). A Stop with no
    unmet gate deletes it; hooks/native-facts empties it on the person's next prompt. A counter that cannot
    be read or written ends the turn: an uncountable push could loop forever."""
    try:
        if "agent_id" in event:  # only the main session's turn end is gated
            return {}
        directory = _directory(event)
        if directory is None:
            return {}
        start = session_start(event)
        if start is None:  # unknown start: earlier sessions' files cannot be told apart, so no decision
            return {}
        unmet, malformed = [], []
        for path in session_files(directory, start):
            try:
                gates = load(path)["gates"]
            except FileNotFoundError:  # deleted since the listing
                continue
            except Malformed as exc:
                malformed.append((path, str(exc)))
                continue
            held = [(item["id"], state) for item in gates for state in (unmet_state(item),) if state]
            if held:
                unmet.append((path, held))
        counter = _counter(event, config_path)
        if not unmet and not malformed:  # a clean state ends the episode
            if counter is not None:
                try:
                    counter.unlink(missing_ok=True)
                except OSError:
                    pass
            return {}
        if event.get("background_tasks") or _blocker(event.get("last_assistant_message")):
            return {}
        push = _spend(counter)
        if push is None:  # budget spent, or the counter is unusable
            return {}
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


def _spend(counter):
    """Count one more push in this episode and return its number (1..PUSH_BUDGET); None when the budget is
    spent or the counter cannot be read or written atomically."""
    from .store import StoreError, atomic_write
    if counter is None:
        return None
    try:
        text = counter.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        text = ""
    except (OSError, UnicodeError):
        return None
    if not text:  # missing, or emptied by the person's prompt
        pushes = 0
    elif text.isdigit():
        pushes = int(text)
    else:
        return None
    if pushes >= PUSH_BUDGET:
        return None
    try:
        atomic_write(counter.parent.parent, counter, pushes + 1)
    except (StoreError, OSError):
        return None
    return pushes + 1


def unmet_state(gate):
    """Why a gate holds the turn ("pending" or "fail"), or None when it does not; human gates never do.

    Issue #46 adds "stale" here: a machine, live or visual gate whose ranAt predates the session's last
    main-session edit (taken from the facts layer)."""
    if gate["kind"] == "human":
        return None
    return gate["status"] if gate["status"] in ("pending", "fail") else None


def _directory(event):
    cwd = event.get("cwd")
    if not isinstance(cwd, str) or not cwd or not Path(cwd).is_absolute():
        return None
    directory = Path(cwd).joinpath(*DIRECTORY)
    return directory if directory.is_dir() else None


def session_start(event):
    """When the session began (POSIX seconds), or None when unknown.

    Source: the first top-level "timestamp" in the session's transcript (Claude Code writes untimed
    metadata lines before it). A resume or compaction keeps the session id and appends to the same
    transcript, so a resumed session keeps its own acceptance files; /clear starts a new transcript.
    Not the facts layer's <session>.enabled marker: every SessionStart (resume, compact) touches it again,
    which would drop the files a session wrote before its last resume, and it exists only with handoff on.
    """
    from .hooks import resolve_transcript  # hooks imports this module
    path = resolve_transcript(event.get("transcript_path"))
    if not isinstance(path, str) or not path:
        return None
    try:
        handle = open(path, "rb")
    except OSError:
        return None
    with handle:
        read = 0
        for raw in handle:
            read += len(raw)
            if b'"timestamp"' in raw:
                try:
                    line = json.loads(raw)
                except ValueError:
                    line = None
                stamp = line.get("timestamp") if isinstance(line, dict) else None
                if isinstance(stamp, str):
                    when = _when(stamp)
                    if when is not None:
                        return when.timestamp()
            if read >= START_SCAN_BYTES:
                return None
    return None


def session_files(directory, start):
    """The *.json files in directory modified at or after start, by name; earlier files are ignored."""
    found = []
    for path in sorted(directory.glob("*.json")):
        try:
            if path.is_file() and path.stat().st_mtime >= start:
                found.append(path)
        except OSError:
            continue
    return found


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
    return data


def _when(text):
    """An aware datetime from ISO 8601 text (naive counts as UTC), or None."""
    try:
        when = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _reason(unmet, malformed, push):
    lines = ["Acceptance gates are not passed, so this turn cannot end yet (push %d/%d; after %d the turn "
             "ends and waits for the owner)." % (push, PUSH_BUDGET, PUSH_BUDGET),
             "If you are blocked, say so on its own line starting with `needs input:` or `failed:`."]
    if unmet:
        lines.append("Run each unmet gate's check and update its status, evidence and ranAt in the file "
                     "(human gates are the owner's decision and never hold the turn):")
        lines += ["- %s: %s" % (path, ", ".join("%s (%s)" % held for held in gates)) for path, gates in unmet]
    if malformed:
        lines.append("Malformed acceptance files; fix each to the schema (%s):" % GUIDE)
        lines += ["- %s: %s" % (path, problem) for path, problem in malformed]
    return "\n".join(lines)
