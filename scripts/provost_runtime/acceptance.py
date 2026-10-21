"""Acceptance gates: the synchronous Stop check behind hooks/acceptance-gate.

A main session that wrote an acceptance file for its task (<cwd>/.claude/acceptance/*.json, schema in
<runtime root>/docs/native-handoff.md, "Acceptance gates") cannot end its turn while a non-human gate is pending,
failed or stale (a machine, live or visual pass older than the last main-session edit): check() returns a block decision naming every unmet gate id and its file. Without a session
acceptance file, or with only human gates unmet, it returns {}. A malformed file is named in the block
reason, so a typo cannot switch enforcement off unnoticed. A subagent's Stop, running background work
and a stated blocker line end the turn, and it blocks at most three times per episode (see check()).
Any internal error returns {}: a broken check must never trap a session.
"""
import json
import re
from datetime import datetime, timedelta, timezone
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
EDIT_SCAN_BYTES = START_SCAN_BYTES  # most transcript bytes (the newest) read to find the last edit
STALE_KINDS = ("machine", "live", "visual")  # a pass of these goes stale after an edit
FUTURE_SLACK = timedelta(minutes=5)  # a zoned ranAt further ahead of now than this is a mislabelled local time
# Cheap byte filter before parsing a line for last_edit(): an edit tool call or a failed tool result.
EDIT_TOKENS = (b'"Edit"', b'"Write"', b'"NotebookEdit"', b'"is_error"')
# The runtime root is the parent of scripts/, in the repo checkout and in the installed runtime alike.
GUIDE = "%s, section Acceptance gates" % (Path(__file__).resolve().parents[2] / "docs" / "native-handoff.md")


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
        unmet, malformed, memo = [], [], []

        def edited():  # read the transcript once, and only when a passed gate could be stale
            if not memo:
                try:
                    memo.append(last_edit(event))
                except Exception:  # unreadable edit history: unknown, so nothing is stale
                    memo.append(None)
            return memo[0]

        for path in session_files(directory, start):
            try:
                gates = load(path)["gates"]
            except FileNotFoundError:  # deleted since the listing
                continue
            except Malformed as exc:
                malformed.append((path, str(exc)))
                continue
            held = [(item["id"], state) for item in gates for state in (unmet_state(item, edited),) if state]
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


def unmet_state(gate, edited=None):
    """Why a gate holds the turn ("pending", "fail" or "stale: ..."), or None when it does not; human gates
    never do.

    edited is the session's last main-session edit (an aware datetime, see last_edit()), or a callable
    returning it, called only for a gate that could be stale. A machine, live or visual gate with status
    pass is stale when its ranAt is before edited, or when it has no usable ranAt (missing, or without a
    time zone, which cannot be compared with the transcript's UTC) and edited is not None. Independent of
    any edit, a zoned ranAt more than FUTURE_SLACK after the current UTC time is stale too: it is local time
    labelled UTC (Windows PowerShell's `date -u`), which would always look later than the last edit.
    Review and human gates are never stale."""
    if gate["kind"] == "human":
        return None
    if gate["status"] in ("pending", "fail"):
        return gate["status"]
    if gate["kind"] not in STALE_KINDS:
        return None
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


def _acceptance_path(path):
    return "/.claude/acceptance/" in "/" + path.replace("\\", "/").lower()


def _clock(when):
    """Local wall-clock time for a reason line."""
    return when.astimezone().strftime("%H:%M:%S")


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
                     "re-run (human gates are the owner's decision and never hold the turn). ranAt must be "
                     "the clock read from a command, in UTC: now %s; Bash `date -u +%%Y-%%m-%%dT%%H:%%M:%%SZ`, "
                     "PowerShell `(Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')` (its `date -u` "
                     "prints local time labelled Z, which is stale). Guide: %s. Unmet gates:" % (now, GUIDE))
        lines += ["- %s: %s" % (path, ", ".join("%s (%s)" % held for held in gates)) for path, gates in unmet]
    if malformed:
        lines.append("Malformed acceptance files; fix each to the schema (%s):" % GUIDE)
        lines += ["- %s: %s" % (path, problem) for path, problem in malformed]
    return "\n".join(lines)
