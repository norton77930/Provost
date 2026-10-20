"""Acceptance gates: the synchronous Stop check behind hooks/acceptance-gate.

A main session that wrote an acceptance file for its task (<cwd>/.claude/acceptance/*.json, schema in
docs/native-handoff.md, "Acceptance gates") cannot end its turn while a non-human gate is pending or
failed: check() returns a block decision naming every unmet gate id and its file. Without a session
acceptance file, or with only human gates unmet, it returns {}. A malformed file is named in the block
reason, so a typo cannot switch enforcement off unnoticed. Any internal error returns {}: a broken
check must never trap a session.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

DIRECTORY = (".claude", "acceptance")
KINDS = ("machine", "live", "visual", "review", "human")
STATUSES = ("pass", "fail", "pending")
OPTIONAL = ("ranAt", "evidence", "source")
FILE_LIMIT = 1024 * 1024           # an acceptance file larger than this is reported as malformed
START_SCAN_BYTES = 8 * 1024 * 1024  # most transcript bytes read to find the session start
GUIDE = "docs/native-handoff.md, section Acceptance gates"


class Malformed(ValueError):
    """An acceptance file that does not match the schema; the message names the first problem."""


def check(event):
    """Stop (sync): {"decision": "block", "reason": ...} while a session acceptance file has an unmet gate
    or is malformed, else {}. Never raises."""
    try:
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
        if not unmet and not malformed:
            return {}
        # Issue #45 plugs in here, before the block: the exemptions that let a turn with unmet gates end
        # (a Stop event with agent_id, a running entry in background_tasks, a `needs input:` or `failed:`
        # line in last_assistant_message) and the per-episode push budget, each returning {}.
        return {"decision": "block", "reason": _reason(unmet, malformed)}
    except Exception:  # fail-soft: the turn ends
        return {}


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


def _reason(unmet, malformed):
    lines = ["Acceptance gates are not passed, so this turn cannot end yet."]
    if unmet:
        lines.append("Run each unmet gate's check and update its status, evidence and ranAt in the file "
                     "(human gates are the owner's decision and never hold the turn):")
        lines += ["- %s: %s" % (path, ", ".join("%s (%s)" % held for held in gates)) for path, gates in unmet]
    if malformed:
        lines.append("Malformed acceptance files; fix each to the schema (%s):" % GUIDE)
        lines += ["- %s: %s" % (path, problem) for path, problem in malformed]
    return "\n".join(lines)
