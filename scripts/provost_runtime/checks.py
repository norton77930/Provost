"""Record checks and echo: what a `record` request may not do, what it may have missed, and what it wrote.

Hard rejections raise StoreError with a code and a `detail`; nothing is written. Decision text
is checked before the write; the join guard, entry targets and duplicates are checked by Gate
under the work's log lock, against the committed log. Soft warnings come back with a successful write
as {code, message} items. The echo is a short text (at most ECHO_MAX characters) of the
assigned ids, the state card, open evidence, the newest decisions and the warnings.
"""
import re
from datetime import datetime, timezone

from . import facts, search, store
from .store import LOG_ENTRY_ID, StoreError

TEXT_MAX = 1000         # decision text over this is refused
SHORT_TEXT = 40         # decision text under this warns
LOG_LARGE = 300         # a log with more entries warns
HASH_PREFIX = 7         # shortest hash prefix that references a commit
ECHO_MAX = 1500         # echo characters
ECHO_DECISIONS = 3      # newest current decisions in the echo
ECHO_ITEM = 200         # characters per echoed decision, evidence or warning line
ECHO_IDS = 8            # assigned ids listed in the echo head; more are shortened to first, last and a count
ECHO_WORK = 80          # characters of the work_id in the echo head
PASSED = "pass-observed"
CONCURRENT_MINUTES = 120  # another session's card of the work this recent warns
SHORT_SESSION = 8       # characters of a session id named in a detail or warning
HEX = re.compile(r"(?<![0-9a-z])[0-9a-f]{%d,40}(?![0-9a-z])" % HASH_PREFIX, re.IGNORECASE)


def check_texts(payload):
    """Refuse empty (or whitespace-only) and over-long decision text. Other shapes are left to store."""
    if not isinstance(payload, dict) or {"decisions", "evidence"} & set(payload):
        return
    items = payload.get("new_decisions")
    for index, item in enumerate(items if isinstance(items, list) else ()):
        text = item.get("text") if isinstance(item, dict) else None
        if not isinstance(text, str):
            continue
        if not text.strip():
            raise StoreError("DECISION_TEXT_EMPTY", "new_decisions[%d]: text is empty; write one self-contained "
                                                    "sentence with the decision and its reason" % index)
        if len(text) > TEXT_MAX:
            raise StoreError("DECISION_TEXT_TOO_LONG", "new_decisions[%d]: text has %d characters, the limit is %d; "
                                                       "split it into several decisions" % (index, len(text), TEXT_MAX))


class Gate:
    """Join, target and duplicate checks, called by store under the work's log lock.

    The committed log it checks against is every entry logged before this record's own
    entries (all of them when none is logged yet), so a replay of an interrupted request
    sees the log its first attempt saw. Returns the entries to append (duplicates skipped).
    With a session and joined False (the session has no card of the work and the record has
    no --join), a log holding entries of other sessions refuses the record (JOIN_REQUIRED).
    """

    def __init__(self, session=None, joined=True):
        self.session, self.joined = session, joined
        self.skipped = []  # (text, id of the current decision with that text, or None: earlier in this request)

    def __call__(self, work_id, logged, revision, entries, replay=False):
        first = next((i for i, entry in enumerate(logged) if entry.get("record") == revision), len(logged))
        prior = logged[:first]
        if not (self.joined or replay):  # replay: store found this session's card of the request
            others = sorted({entry["session"] for entry in prior if entry["session"] != self.session})
            if others:
                raise StoreError("JOIN_REQUIRED", "work %s already has log entries from %d other session(s) (%s) and "
                                                  "this session has no card of it: to continue that work, add --join "
                                                  "to this record or resume a snapshot of it; for new work, use "
                                                  "another work_id" % (_clip(work_id, ECHO_WORK), len(others),
                                                                       ", ".join(_short(s) for s in others[:5])))
        known = {entry["id"]: entry for entry in prior}
        superseded = {entry["supersedes"]: entry["id"] for entry in prior
                      if entry["kind"] == "decision" and "supersedes" in entry}
        claimed = set()
        counts = {"decision": 0, "evidence": 0}
        for kind, fields in entries:
            where = "new_%s[%d]" % ("decisions" if kind == "decision" else "evidence", counts[kind])
            counts[kind] += 1
            field = "supersedes" if kind == "decision" else "updates"
            if field not in fields:
                continue
            target = fields[field]
            _target(work_id, known, target, kind, where)
            if kind == "decision":
                if target in superseded:
                    latest = target
                    while latest in superseded:
                        latest = superseded[latest]
                    raise StoreError("ALREADY_SUPERSEDED", "%s: %s is already superseded by %s; the latest entry of "
                                                           "its chain is %s: supersede %s instead"
                                     % (where, target, superseded[target], latest, latest))
                if target in claimed:
                    raise StoreError("ALREADY_SUPERSEDED", "%s: %s is superseded by an earlier entry of this record"
                                     % (where, target))
                claimed.add(target)
        current = {entry["text"]: entry["id"] for entry in prior
                   if entry["kind"] == "decision" and entry["id"] not in superseded}
        kept, self.skipped = [], []
        for kind, fields in entries:
            if kind == "decision":
                if "supersedes" in fields:
                    # A correction is always appended; the text it replaces is current no longer.
                    replaced = known[fields["supersedes"]]["text"]
                    if current.get(replaced) == fields["supersedes"]:
                        del current[replaced]
                elif fields["text"] in current:
                    self.skipped.append((fields["text"], current[fields["text"]]))
                    continue
                current[fields["text"]] = None
            kept.append((kind, fields))
        return kept


def _target(work_id, known, target, kind, where):
    letter, name = ("D", "a decision (D...)") if kind == "decision" else ("E", "an evidence entry (E...)")
    field = "supersedes" if kind == "decision" else "updates"
    match = LOG_ENTRY_ID.fullmatch(target)
    if match is None:
        work, colon, ident = target.rpartition(":")
        if colon and work and work != work_id and LOG_ENTRY_ID.fullmatch(ident):
            raise StoreError("TARGET_OTHER_WORK", "%s: %s %s names an entry of work %s; a record may only point at "
                                                  "entries of its own work (%s)" % (where, field, target, work, work_id))
        raise StoreError("TARGET_NOT_FOUND", "%s: %s %s is not an entry id; write the bare id of an entry of work %s, "
                                             "such as %s1" % (where, field, target, work_id, letter))
    if match[1] != letter:
        raise StoreError("TARGET_WRONG_KIND", "%s: %s must name %s, not %s" % (where, field, name, target))
    if target not in known:
        raise StoreError("TARGET_NOT_FOUND", "%s: %s %s does not exist in the log of work %s"
                         % (where, field, target, work_id))


def _epoch(text):
    return store._when(text).timestamp()  # naive times are UTC, as store reads them


def _last_record(root, session, request_id):
    """Epoch of the session's newest card of any work other than this request's, None without one,
    False when unreadable (no activity warnings then)."""
    try:
        revision = store.digest(session + "\0record\0" + request_id)
        times = [_epoch(head["recorded_at"]) for head in store.cards(root, session) if head["revision"] != revision]
    except (StoreError, OSError, KeyError, TypeError, ValueError):
        return False
    return max(times) if times else None


def _short(session):
    return session[:SHORT_SESSION]


def _others(root, session, payload):
    """Warnings from the work's cards across sessions, read before the write (read-only, fail-soft):
    CONCURRENT_SESSION for another session's card under CONCURRENT_MINUTES old, REOPENED when the
    work's newest card is done and this record's phase is not."""
    work_id = payload.get("work_id") if isinstance(payload, dict) else None
    if not isinstance(work_id, str):
        return []
    try:
        state = store.work_states(root)["works"].get(work_id)
        if state is None:
            return []
        now = datetime.now(timezone.utc).timestamp()
        ages = [(item["session"], int((now - _epoch(item["recorded_at"])) // 60)) for item in state["sessions"]
                if item["session"] != session]
    except (StoreError, OSError, KeyError, TypeError, ValueError):
        return []
    recent = [(other, minutes) for other, minutes in ages if minutes < CONCURRENT_MINUTES]
    warnings = []
    if recent:
        warnings.append(_warning("CONCURRENT_SESSION", "another session wrote work %s in the last %d minutes: %s; "
                                 "coordinate before continuing in parallel"
                                 % (_clip(work_id, ECHO_WORK), CONCURRENT_MINUTES,
                                    ", ".join("session %s %d min ago" % (_short(other), max(minutes, 0))
                                              for other, minutes in recent))))
    if not state["open"] and not store.is_done(payload.get("phase")):
        warnings.append(_warning("REOPENED", "work %s was closed (phase done) by session %s at %s; this record "
                                             "reopens it" % (_clip(work_id, ECHO_WORK), _short(state["session"]),
                                                            state["recorded_at"])))
    return warnings


def record(root, session, payload, expected, request_id, *, config_path, cursor=None, join=False):
    """store.record with the checks; its output plus `warnings` and `echo` (secret-like strings redacted).

    config_path locates the facts spool; activity is read after the write, with no lock held. The
    cross-session warnings are read before the write. join admits a first write to a work that other
    sessions logged; a session whose card of the work `expected` names (written or adopted) has joined
    it already (any other `expected` is a REVISION_CONFLICT before the join check runs).
    """
    check_texts(payload)
    after = _last_record(root, session, request_id) if isinstance(request_id, str) else False
    others = _others(root, session, payload)
    joined = join or expected is not None
    gate = Gate(session, joined=joined)
    # A joined record with no entries has nothing to check: store then never reads the log.
    entries = isinstance(payload, dict) and any(payload.get(field) for field in store.LOG_FIELDS)
    data = store.record(root, session, payload, expected, request_id, cursor=cursor,
                        check=gate if entries or not joined else None)
    work_id = data["payload"]["work_id"]
    try:
        log = store.read_log(root, work_id)
    except StoreError:
        log = None  # written, but unreadable now: no log sections
    ids = data.get("log_entries", [])
    entries = {entry["id"]: entry for entry in log or ()}
    warnings = list(others)
    short = [i for i in ids if i in entries and entries[i]["kind"] == "decision"
             and len(entries[i]["text"].strip()) < SHORT_TEXT]
    if short:
        warnings.append(_warning("SHORT_DECISION", "%s under %d characters: write one self-contained sentence with "
                                                   "the decision, its reason and specifics (files, values, PRs)"
                                 % (_ids(short), SHORT_TEXT)))
    if gate.skipped:
        warnings.append(_warning("DUPLICATE_SKIPPED", "skipped %d decision(s) whose exact text is already logged: %s"
                                 % (len(gate.skipped), "; ".join(
                                     "%s %s" % (ident or "earlier in this record", _clip(text, 60))
                                     for text, ident in gate.skipped))))
    if after is not False:
        decided = any(i.startswith("D") for i in ids)
        warnings += _activity(config_path, root, session, after, payload, decided)
    if log is not None and len(log) > LOG_LARGE:
        warnings.append(_warning("LOG_LARGE", "the log of work %s has %d entries (over %d); consider splitting the "
                                              "work" % (work_id, len(log), LOG_LARGE)))
    warnings = search.redact(warnings)
    return {**data, "warnings": warnings, "echo": echo(search.redact(data), search.redact(log), warnings)}


def _warning(code, message):
    return {"code": code, "message": message}


def _ids(ids):
    return ", ".join(ids) + (" is" if len(ids) == 1 else " are")


def _requested(payload):
    return payload.get("new_decisions", []), payload.get("new_evidence", [])


def _referenced(commit, tokens):
    """True when a token and the commit hash share a prefix of at least HASH_PREFIX characters."""
    full = commit.lower()
    return any(min(len(full), len(token)) >= HASH_PREFIX and full[:len(token)] == token[:len(full)]
               for token in tokens)


def _activity(config_path, root, session, after, payload, decided):
    """Warnings from the session's facts as of now (facts.changes: stored facts, the unfolded spool and a
    fresh bounded git query; read-only): activity after `after` (None: all of it)."""
    try:
        data = facts.changes(config_path, root, session)["facts"]
    except (StoreError, OSError, KeyError, TypeError, AttributeError, ValueError):
        return []
    try:
        found = facts.since(data, after)
        edits = [f for f in data.get("files", [])
                 if type(f.get("last_ts")) is int and (after is None or f["last_ts"] > after)]
        decisions, evidence = _requested(payload)
        texts = ([d["text"] for d in decisions] + [ref for d in decisions for ref in d.get("refs", [])]
                 + [e["claim"] for e in evidence] + [e["ref"] for e in evidence])
        tokens = {token.lower() for text in texts for token in HEX.findall(text)}
        haystack = "\n".join(texts + [a[k] for a in payload.get("assignments", []) for k in ("task", "agent_id")]).lower()
        warnings = []
        missing = [(repo["root"], c["hash"]) for repo in found["repos"] for c in repo["commits"]
                   if not _referenced(c["hash"], tokens)]
        if missing:
            warnings.append(_warning("UNREFERENCED_COMMITS", "%d commit(s) since the last record have no hash prefix "
                                     "(%d+ characters) in this record's entries (refs, text, evidence ref): %s"
                                     % (len(missing), HASH_PREFIX, ", ".join(h for _, h in missing))))
        quiet = [a for a in found["dispatches"]
                 if a["role"].lower() not in haystack and not (a["desc"] and a["desc"].lower() in haystack)]
        if quiet:
            warnings.append(_warning("UNMENTIONED_DISPATCHES", "%d dispatch(es) since the last record are mentioned "
                                     "by neither role nor description in this record's entries or assignments: %s"
                                     % (len(quiet), "; ".join(a["role"] + (": " + a["desc"] if a["desc"] else "")
                                                              for a in quiet))))
        if not decided and (edits or found["dispatches"] or found["commits"]):
            warnings.append(_warning("NO_NEW_DECISION", "activity since the last record (%d edited file(s), %d "
                                     "dispatch(es), %d commit(s)) but this record adds no new decision"
                                     % (len(edits), len(found["dispatches"]), found["commits"])))
        return warnings
    except (KeyError, TypeError, AttributeError, ValueError):
        return []


def _clip(text, limit):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def echo(data, log, warnings):
    """The record echo, at most ECHO_MAX characters. Over the cap it drops the oldest shown decisions,
    then the oldest open evidence, then the last warnings, counting each, and finally clips the card."""
    card = data["payload"]
    ids = data.get("log_entries", [])
    listed = (", ".join(ids) if len(ids) <= ECHO_IDS
              else "%s, ... %s (%d entries)" % (", ".join(ids[:ECHO_IDS - 2]), ", ".join(ids[-2:]), len(ids)))
    head = "Recorded %s (revision %s): %s" % (_clip(card["work_id"], ECHO_WORK), data["revision"][:12],
                                              listed or "no new entries")
    lines = ["Card: phase: %s; next: %s" % (card["phase"], card["next"])]
    if card["blockers"]:
        lines.append("Blockers: " + "; ".join(card["blockers"]))
    if card["assignments"]:
        lines.append("Assignments: " + "; ".join("%s (%s, %s)" % (a["task"], a["agent_id"], a["observed_state"])
                                                 for a in card["assignments"]))
    card_text = " ".join(lines[0].split()) + "".join("\n" + " ".join(line.split()) for line in lines[1:])
    sections = []
    if log is not None:
        evidence = [e for e in log if e["kind"] == "evidence"]
        updated = {e["updates"] for e in evidence if "updates" in e}
        still_open = [e for e in evidence if e["id"] not in updated and e["state"] != PASSED]
        superseded = {e["supersedes"] for e in log if e["kind"] == "decision" and "supersedes" in e}
        current = [e for e in log if e["kind"] == "decision" and e["id"] not in superseded]
        sections.append(["Open evidence", [_clip("- %s [%s] %s (%s)" % (e["id"], e["state"], e["claim"], e["ref"]),
                                                 ECHO_ITEM) for e in store.newest_entries(still_open, len(still_open))],
                         0, 0])
        recent = store.newest_entries(current, ECHO_DECISIONS)[::-1]  # newest first
        sections.append(["Recent decisions", [_clip("- %s %s" % (e["id"], e["text"]), ECHO_ITEM) for e in recent],
                         0, -1])
    sections.append(["Warnings", [_clip("- %s: %s" % (w["code"], w["message"]), ECHO_ITEM) for w in warnings], 0, -1])

    def render(card_part):
        parts = [head, card_part]
        for title, items, trimmed, _ in sections:
            if not items:
                parts.append("%s: %s" % (title, "%d more, see status" % trimmed if trimmed else "none"))
            else:
                parts.append("\n".join([title + ":"] + items + (["- %d more, see status" % trimmed] if trimmed else [])))
        return "\n".join(parts)

    order = [s for title in ("Recent decisions", "Open evidence", "Warnings") for s in sections if s[0] == title]
    text = render(card_text)
    while len(text) > ECHO_MAX:
        section = next((s for s in order if s[1]), None)
        if section is None:
            break
        section[1].pop(section[3])
        section[2] += 1
        text = render(card_text)
    if len(text) > ECHO_MAX:
        text = render(_clip(card_text, max(20, len(card_text) - (len(text) - ECHO_MAX))))
    return text[:ECHO_MAX]
