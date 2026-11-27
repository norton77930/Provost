"""Dispositions check (#155): the second Stop check behind hooks/acceptance-gate.

Policy §3 asks the main session, after reading a code-reviewer report with Important findings, to write one line
`Dispositions: I1 fixed; I2 rejected — <reason>; …`. When the main session's turn ends after a finished
code-reviewer result whose report has Important > 0, and no main-session text after that result holds such a
line, check() asks for it once, naming the review.

State: one marker per review, <config dir>/cache/acceptance/<session>.review-<tool_use_id>, holding the
dispatch's transcript path as a JSON string (for a person reading the folder; check() reads the Stop's own
transcript, so after /clear a review missing from it is dropped). note() writes it on the PreToolUse (Agent) event of a main-session
code-reviewer dispatch; check() deletes it once the review is decided (asked, answered, without Important findings,
failed, or gone from the transcript). One file per review means two dispatches in one message never rewrite a
shared file, and only this synchronous hook touches these files. hooks/acceptance-gate starts Python on a Stop
only while such a marker exists, so a session without a pending review keeps its bash-only fast path. A review
still unfinished at a Stop more than WAIT_LIMIT after its dispatch is dropped with no block, so a lost result
cannot send every later Stop to Python; `prune` removes markers a session left behind.
Any error ends the turn with no decision: a broken check must never trap a session.
"""
import json
import re
import time
from pathlib import Path

from .acceptance import COUNTER_DIR, SESSION, _blocker

ROLE = "code-reviewer"
MARKER = ".review-"  # <session>.review-<tool_use_id>; hooks/acceptance-gate globs the same name
TOOL_ID = re.compile(r"^[A-Za-z0-9_-]+$")
COUNTS = re.compile(r"Important\s*[:：]?\s*(\d+)")  # the report's counts line (tools/dispatch-report.js reviewInfo)
IDS = re.compile(r"\bI(\d+)\b")  # older reports only number their findings
# A Dispositions line: optionally a list item and/or bold, as tools/dispatch-report.js DISP_RE reads it. Outcomes
# are not parsed here: the line being there is what this check asks for.
LINE = re.compile(r"^[ \t]*(?:[-*][ \t]+)?(?:\*\*)?Dispositions(?:\*\*)?:", re.MULTILINE)
DESCRIPTION_LIMIT = 80
# A background run: its tool_result is a launch notice naming the agent; the report arrives as the agent's
# SubagentHandback (an agent-message frame) or its task-notification <result>, either as a user line or, when it
# is delivered while the main session is mid-turn, as a queued_command attachment line (attachment.prompt).
LAUNCHED = "Async agent launched"
AGENT = re.compile(r"^[A-Za-z0-9_-]+$")
AGENT_ID = re.compile(r"agentId:\s*([A-Za-z0-9_-]+)")
HANDBACK = re.compile(r'<agent-message from="([A-Za-z0-9_-]+)">\s*\[Subagent hand-back\]')
NOTIFICATION = re.compile(r"<task-notification>\s*<task-id>([A-Za-z0-9_-]+)</task-id>")
STATUS = re.compile(r"<status>(\w+)</status>")
RESULT = re.compile(r"<result>(.*?)</result>", re.DOTALL)
# A result that only points at the hand-back message: one of the harness's two sentences, starting a line within
# the first POINTER_WINDOW characters (a harness note can precede it; a report that quotes the phrase is the report).
POINTER = re.compile(r'^[ \t]*This agent(?:\'s report was delivered to you as a message from "[A-Za-z0-9_-]+" '
                     r"\(its SubagentHandback call\)|"
                     r" has not reported yet:[^\n]* will deliver its report through SubagentHandback)", re.MULTILINE)
POINTER_WINDOW = 600
# Report sources the transcript prefilter passes on, besides the dispatches' own ids: a hand-back can be written
# before or after its Agent tool_result, so the agent id is not known in time to filter on it.
SOURCES = (b"[Subagent hand-back]", b"<task-notification>")
# An unfinished review is dropped at a Stop this long after its dispatch (the marker's mtime): background reviews
# take up to about an hour, and a dropped marker only loses one reminder.
WAIT_LIMIT = 4 * 3600


def note(event, config_path):
    """PreToolUse (Agent): write the marker of a main-session code-reviewer dispatch. Never raises."""
    from .store import atomic_write
    try:
        if "agent_id" in event or event.get("tool_name") not in ("Agent", "Task"):
            return
        tool_input = event.get("tool_input")
        if not isinstance(tool_input, dict) or tool_input.get("subagent_type") != ROLE:
            return
        call, transcript = event.get("tool_use_id"), event.get("transcript_path")
        folder = _folder(event, config_path)
        if folder is None or not isinstance(call, str) or not TOOL_ID.fullmatch(call):
            return
        if not isinstance(transcript, str) or not transcript:
            return
        atomic_write(folder.parent, folder / (event["session_id"] + MARKER + call), transcript)
    except Exception:  # a lost marker only skips this review's check
        return


def check(event, config_path):
    """Stop (sync): the block reason asking for the Dispositions line of each newly finished review, else None.
    Never raises."""
    try:
        if "agent_id" in event:
            return None
        folder = _folder(event, config_path)
        if folder is None:
            return None
        pending = {}
        for marker in folder.glob(event["session_id"] + MARKER + "*"):
            call = marker.name[len(event["session_id"] + MARKER):]
            if TOOL_ID.fullmatch(call):
                pending[call] = marker
        if not pending:
            return None
        # A legitimate pause, as for the acceptance gates: nothing is decided, so the next Stop asks.
        if event.get("background_tasks") or _blocker(event.get("last_assistant_message")):
            return None
        scanned = _scan(event.get("transcript_path"), set(pending))
        if scanned is None:
            return None
        reviews, answered_at = scanned
        if _answers(event.get("last_assistant_message")):  # the turn's last text follows every result
            answered_at = float("inf")
        asked = []
        for call, marker in sorted(pending.items()):
            state = reviews.get(call)
            if state is None:  # not in this transcript (/clear started a new one): nothing to ask
                _forget(marker)
                continue
            if not state["finished"]:  # still running: the Stop after its report decides, unless it overran
                if _overdue(marker):
                    _forget(marker)
                continue
            if not _forget(marker):
                continue
            if state["important"] > 0 and answered_at <= state["finished_at"]:
                asked.append(state)
        return _reason(asked) if asked else None
    except Exception:
        return None


def _folder(event, config_path):
    session = event.get("session_id")
    if config_path is None or not isinstance(session, str) or not SESSION.fullmatch(session):
        return None
    return Path(config_path).parent / "cache" / COUNTER_DIR


def _forget(marker):
    """Delete a decided review's marker; False when it is gone or cannot be deleted, so it is never asked twice."""
    try:
        marker.unlink()
        return True
    except OSError:
        return False


def _overdue(marker):
    try:
        return time.time() - marker.stat().st_mtime > WAIT_LIMIT
    except OSError:
        return False


def _answers(text):
    return isinstance(text, str) and LINE.search(text) is not None


def _scan(transcript, calls):
    """({tool_use_id: {"description", "finished", "finished_at", "important"}} for the dispatches among calls found
    in the transcript, the line number of its last main-session Dispositions line or -1); None when the transcript
    cannot be read."""
    from .hooks import resolve_transcript  # hooks imports this module
    path = resolve_transcript(transcript)
    if not isinstance(path, str) or not path:
        return None
    try:
        handle = open(path, "rb")
    except OSError:
        return None
    # waiting: background agent id -> its review; reports: agent id -> (line, Important) of a report seen first
    reviews, answered_at, waiting, reports = {}, -1, {}, {}
    tokens = [call.encode("ascii") for call in calls] + [b"Dispositions", *SOURCES]
    with handle:
        for number, raw in enumerate(handle):
            if not any(token in raw for token in tokens):
                continue
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(line, dict) or line.get("isSidechain"):
                continue
            kind = line.get("type")
            if kind == "attachment":  # delivered mid-turn: a queued prompt, no message key
                attachment = line.get("attachment")
                if isinstance(attachment, dict) and attachment.get("type") == "queued_command":
                    _report(_text(attachment.get("prompt")), waiting, reports, number)
                continue
            message = line.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if kind == "user":
                _report(_text(content), waiting, reports, number)
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if kind == "assistant" and block.get("type") == "text" and _answers(block.get("text")):
                    answered_at = number
                elif kind == "assistant" and block.get("type") == "tool_use" and block.get("id") in calls:
                    tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                    reviews[block["id"]] = {"description": tool_input.get("description"), "finished": False,
                                            "finished_at": None, "important": 0}
                elif kind == "user" and block.get("type") == "tool_result" and block.get("tool_use_id") in reviews:
                    review, text = reviews[block["tool_use_id"]], _text(block.get("content"))
                    agent = None if block.get("is_error") else _background(text, line.get("toolUseResult"))
                    if agent in reports:  # its report was written first
                        review["finished"], review["finished_at"] = True, number
                        review["important"] = reports.pop(agent)[1]
                    elif agent:  # the report comes later
                        waiting[agent] = review
                    else:
                        review["finished"], review["finished_at"] = True, number
                        review["important"] = 0 if block.get("is_error") else _important(text)
    return reviews, answered_at


def _background(text, result):
    """The agent id of a background run (or of a foreground one that delivered its report through a hand-back
    message), else None: this tool_result is the report itself."""
    result = result if isinstance(result, dict) else {}
    if not (result.get("isAsync") or text.lstrip().startswith(LAUNCHED) or _pointer(text)):
        return None
    agent = result.get("agentId")
    if not isinstance(agent, str):
        found = AGENT_ID.search(text)
        agent = found.group(1) if found else None
    return agent if isinstance(agent, str) and AGENT.fullmatch(agent) else None


def _pointer(text):
    found = POINTER.search(text)
    return found is not None and found.start() < POINTER_WINDOW


def _report(text, waiting, reports, number):
    """Read a report source, a user line's text or a queued attachment's prompt: an agent's SubagentHandback
    message, or a completed task-notification whose result is the report (not a pointer to the hand-back); a
    notification of a failed or killed run reports no finding. It finishes the waiting review of that agent, or is
    kept in reports (first one per agent) for a tool_result written after it."""
    found = HANDBACK.search(text)
    if found:
        report = text[found.end():]
    else:
        found = NOTIFICATION.search(text)
        if not found:
            return
        status, result = STATUS.search(text), RESULT.search(text)
        if status and status.group(1) != "completed":
            report = ""
        else:
            report = result.group(1) if result else text
            if _pointer(report):
                return
    agent = found.group(1)
    if agent in waiting:
        review = waiting.pop(agent)
        review["finished"], review["finished_at"], review["important"] = True, number, _important(report)
    else:
        reports.setdefault(agent, (number, _important(report)))


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part["text"] for part in content
                         if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str))
    return ""


def _important(report):
    """Important findings of a report: its counts line, else the distinct I<n> ids it names."""
    counts = COUNTS.search(report)
    if counts:
        return int(counts.group(1))
    return len(set(IDS.findall(report)))


def _description(review):
    text = review["description"] if isinstance(review["description"], str) else ""
    text = " ".join(text.split()) or "(no description)"
    return text if len(text) <= DESCRIPTION_LIMIT else text[:DESCRIPTION_LIMIT - 1] + "…"


def _reason(asked):
    named = "; ".join('"%s" (%d Important)' % (_description(review), review["important"]) for review in asked)
    return ("Dispositions: no main-session text after the result of code-reviewer %s %s has a Dispositions line. "
            "Write one line per review naming each Important finding with exactly one outcome: "
            "`Dispositions: I1 fixed; I2 rejected — <reason>; …` (fixed, rejected, intentional, moot or unproven; "
            "every outcome but fixed needs a reason after —). This is asked once per review."
            % ("reviews" if len(asked) > 1 else "review", named))
