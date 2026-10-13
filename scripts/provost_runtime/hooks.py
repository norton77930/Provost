"""Claude event adapter. Saving errors must never become compaction decisions."""
import json
from datetime import datetime, timezone
from pathlib import Path

from . import facts
from .config import ConfigError, handoff_home, load_config, resolve_mode, workspace_key
from .notifications import PARTIAL, notice
from .store import (CACHE_SESSION, FACTS_MARKER, StoreError, _base, _chains, as_workspace, atomic_write, current,
                    digest, guard, latest, locate, newest_entries, read_log, session_dir, snapshot, storage_ready)

PLUGIN = Path(__file__).resolve().parents[2]
RECOVERY_PAYLOAD_LIMIT = 4500     # inline the payload in full up to this size; digest is capped to it too
RECOVERY_ASSIGNMENT_LIMIT = 300   # per-assignment JSON cap inside the digest
SESSION_START_BUDGET = 8000       # Claude Code swaps a larger additionalContext for a 2 KB preview
RECOVERY_FILES = 10               # edited files listed after compaction; the rest are counted
RECOVERY_DISPATCHES = 5           # newest dispatches listed since the last record
RECOVERY_COMMITS = 3              # newest commits listed per repo since the last record
RECOVERY_LINE_LIMIT = 300         # per-repo commit line cap; also per other-work line
RECOVERY_DECISIONS = 15           # newest non-superseded decisions of the current work, filled newest first
RECOVERY_NEXT_LIMIT = 160         # next step cap in an other-work line
PARTIAL_STATUS = ("Save status: the last compaction saved facts only. Before continuing, write a decision record "
                  "from the compaction summary: run status (it prefills facts and template.sources), then record.")
NEVER_RECORDED = ("No handoff checkpoint: this session has not recorded yet; continue from the compact summary "
                  "and record a milestone now.")


def workspace_root(cwd):
    """The Workspace for cwd: its top-level dir and repo-level store identity (one spawn)."""
    return locate(cwd)


def cursor(path):
    if not isinstance(path, str) or not path:
        return None
    try:
        info = Path(path).stat()
        return {"source": digest(str(Path(path).resolve())), "bytes": info.st_size, "mtime_ns": info.st_mtime_ns}
    except OSError:
        return None


def resolve_transcript(path):
    """Follow a transcript Claude Code moved to a `<project>--<worktree>` sibling dir.

    An existing path is returned unchanged. Otherwise the newest same-named file
    in a sibling project dir prefixed `<project>--` wins; no match returns the
    original path so cursor() still yields None. One directory listing, no spawns.
    """
    if not isinstance(path, str) or not path:
        return path
    try:
        target = Path(path)
        if target.exists():
            return path
        prefix = target.parent.name + "--"
        best = None
        for sibling in target.parent.parent.iterdir():
            if not sibling.name.startswith(prefix):
                continue
            try:
                mtime = (sibling / target.name).stat().st_mtime_ns
            except OSError:
                continue
            if best is None or mtime > best[0]:
                best = (mtime, sibling / target.name)
        return str(best[1]) if best else path
    except OSError:
        return path


def last_binding_at(root):
    """ISO-8601 UTC mtime of the newest binding.json in the store, else None. Read-only."""
    try:
        home = handoff_home()
        sessions = _base(as_workspace(root)) / "sessions"
        guard(home, sessions)
        newest = None
        for entry in sessions.iterdir():
            try:
                path = entry / "binding.json"
                guard(home, path)
                mtime = path.stat().st_mtime
            except (StoreError, OSError):
                continue
            newest = mtime if newest is None or mtime > newest else newest
        return None if newest is None else datetime.fromtimestamp(newest, timezone.utc).isoformat()
    except (StoreError, OSError):
        return None


def binding(root, session):
    root = as_workspace(root)
    path = session_dir(root, session) / "binding.json"
    guard(handoff_home(), path)
    if not path.exists():
        return {}
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(result, dict) or result.get("workspace_id") != root.repo_key
                or result.get("session_id") != session):
            raise StoreError("BINDING_MISMATCH")
        return result
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StoreError("BINDING_UNREADABLE") from exc


def bind(root, session, event):
    root = as_workspace(root)
    storage_ready(root)
    atomic_write(handoff_home(), session_dir(root, session) / "binding.json",
                 {"workspace_id": root.repo_key, "worktree_path": str(root.root), "session_id": session,
                  "transcript_path": event.get("transcript_path"),
                  "last_cursor": cursor(resolve_transcript(event.get("transcript_path")))})


def _context(text):
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}


def _notification(message, session_start=False):
    if not message:
        return {}
    if session_start:
        return {"systemMessage": message}
    # Fixed text and error codes only. Never put arbitrary payload into an OSC.
    safe = "".join(c for c in message if 32 <= ord(c) < 127).replace(";", ",")
    return {"terminalSequence": "\u001b]9;" + safe + "\u0007"}


def _clip(text, limit):
    """Deterministic cut to at most `limit` chars, ending in an ellipsis marker."""
    if len(text) <= limit:
        return text
    head = text[:limit - 1]
    if head and "\ud800" <= head[-1] <= "\udbff":  # never leave half a surrogate pair
        head = head[:-1]
    return head + "…"


def _digest_text(payload):
    digest = {"work_id": payload["work_id"], "phase": payload["phase"], "next": payload["next"],
              "blockers": payload["blockers"],
              # Stored keys are sorted; lead with task/agent_id so a cut keeps the identifying fields.
              "assignments": [_clip(json.dumps({k: item[k] for k in ("task", "agent_id", "observed_state", "write_scope")},
                                               ensure_ascii=False), RECOVERY_ASSIGNMENT_LIMIT)
                              for item in payload["assignments"]]}
    if "decisions" in payload:  # a schema 1 card; schema 2 keeps decisions in the decision log
        digest["decisions"] = payload["decisions"][-3:]
    return ("Observed payload digest (full JSON in the checkpoint file): "
            + _clip(json.dumps(digest, ensure_ascii=False), RECOVERY_PAYLOAD_LIMIT))


def _payload_text(payload):
    full = json.dumps(payload, ensure_ascii=False)
    if len(full) <= RECOVERY_PAYLOAD_LIMIT:
        return "Observed payload: " + full
    return _digest_text(payload)


# Schema 2 cards hold no decisions or evidence; the work_id is in the payload shown after this line.
LOG_POINTER = ("Decisions and evidence are in the work's decision log, not in this card: "
               "status --work <work_id> shows its newest entries (log field).\n")


def _recovery(root, session, chains=None):
    """(header, payload, checkpoint path, checkpoint) of the newest checkpoint, or (message, None, None, None).

    chains: the session's record chains when the caller already read them (None reads the store)."""
    # The latest milestone may be newer than the last compaction snapshot.
    root = as_workspace(root)
    data = current(root, session) if chains is None else (chains[0][0] if chains else None)
    if data is None:
        data = latest(root, session)
    if data is None:
        return "No checkpoint for this session. Verify current sources before continuing.", None, None, None
    kind = "records" if data["kind"] == "record" else "snapshots"
    ident = data["revision"] if kind == "records" else data["snapshot_id"]
    path = session_dir(root, session) / kind / (ident + ".json")
    return ("Saved observations, not current facts. Verify sources, code, evidence and worker state before acting. "
            "Do not duplicate a possibly running worker. Checkpoints can lag later decisions.\n"
            + ("Checkpoint (old format, not migrated): " if data.get("schema_version") == 1 else "Checkpoint: ")
            + str(path) + "\nRevision: " + data["revision"]
            + "\nTranscript cursor: " + ("available (not a freshness guarantee)" if data.get("transcript_cursor") else "unavailable; gaps must be checked")
            + "\nRead this JSON file for the complete checkpoint.\n"
            + "If any assignment is not marked completed, verify that worker's actual state before dispatching; "
            + "read the checkpoint file for full details.\n"), data["payload"], path, data


def _never_recorded(root, session):
    """True when the session has no record or snapshot; unreadable counts as False."""
    try:
        return current(root, session) is None and latest(root, session) is None
    except (StoreError, OSError):
        return False


def recovery_context(root, session):
    header, payload, _, data = _recovery(root, session)
    if payload is None:
        return header
    return header + (LOG_POINTER if data.get("schema_version") == 2 else "") + _payload_text(payload)


def _other_works(chains):
    """Variants of the session's other open works (every chain head but the current; phase done is closed)."""
    others = [chain[0]["payload"] for chain in chains[1:] if chain[0]["payload"]["phase"] != "done"]
    if not others:
        return []
    lines = ["Other open works in this session (status --work <work_id> shows a card):"]
    lines += [_clip("- %s [%s] next: %s" % (p["work_id"], p["phase"], _clip(p["next"], RECOVERY_NEXT_LIMIT)),
                    RECOVERY_LINE_LIMIT) for p in others]
    return ["\n".join(lines), "Other open works in this session: %d, omitted for size; status lists them "
                              "as other_works." % len(others)]


def _decision_line(entry):
    extra = ""
    if entry.get("refs"):
        extra += " [refs: " + ", ".join(entry["refs"]) + "]"
    if entry.get("supersedes"):
        extra += " [replaces " + entry["supersedes"] + "]"
    return "- %s (%s) %s%s" % (entry["id"], entry["at"][:10], entry["text"], extra)


def _evidence_line(item):
    """A log evidence entry (with id) or a schema 1 evidence item (without)."""
    return "- %s[%s] %s (ref: %s)" % (item["id"] + " " if "id" in item else "", item["state"], item["claim"],
                                      item["ref"])


def _evidence_variants(title, items, passed):
    """Open evidence (oldest first) and the passed count: the full list, then counts only. Empty without any."""
    if not items:
        return ["%s: none open. Passed claims: %d." % (title, passed)] if passed else []
    return ["\n".join(["%s (latest state per claim, not pass-observed). Passed claims: %d." % (title, passed)]
                      + [_evidence_line(item) for item in items]),
            "%s: %d open, list omitted for size; status --work <work_id> shows the log. Passed claims: %d."
            % (title, len(items), passed)]


def _decision_variants(title, lines, total, note=""):
    """The newest RECOVERY_DECISIONS of `lines` (oldest first), then one fewer per variant, then counts only."""
    shown = lines[-RECOVERY_DECISIONS:]
    if not shown:
        return []
    variants = ["\n".join(["%s (newest %d of %d, oldest first%s):" % (title, k, total, note)] + shown[-k:])
                for k in range(len(shown), 0, -1)]
    return variants + ["%s: %d, omitted for size%s; status --work <work_id> shows the newest." % (title, total, note)]


def _log_state(entries):
    """(newest current decisions, current count, open evidence, passed count) of a work's log.

    Superseded decisions are dropped; each evidence chain (linked by updates) counts by its
    newest entry. Lists are oldest first by store.newest_entries (time, then log order)."""
    superseded = {e["supersedes"] for e in entries if e["kind"] == "decision" and "supersedes" in e}
    updated = {e["updates"] for e in entries if e["kind"] == "evidence" and "updates" in e}
    live = [e for e in entries if e["kind"] == "decision" and e["id"] not in superseded]
    heads = [e for e in entries if e["kind"] == "evidence" and e["id"] not in updated]
    pending = [e for e in heads if e["state"] != "pass-observed"]
    return (newest_entries(live, RECOVERY_DECISIONS), len(live), newest_entries(pending, len(pending)),
            len(heads) - len(pending))


def _work_sections(root, session, chains, checkpoint):
    """(other works, open evidence, decisions) variants for the checkpoint's work.

    A schema 1 checkpoint shows its own decisions in the payload. A work without a log falls
    back to the newest schema 1 card of its chain, labelled not migrated. An unreadable log
    becomes one line: the log never breaks injection."""
    others = _other_works(chains)
    if checkpoint is None or checkpoint.get("schema_version") == 1:
        return others, [], []
    work = checkpoint["payload"]["work_id"]
    try:
        entries = read_log(root, work)
    except (StoreError, OSError) as exc:
        code = str(exc) if isinstance(exc, StoreError) else "READ_FAILED"
        return others, [], ["Decision log of work %s could not be read (%s): its decisions and open evidence are "
                            "not shown; status --work <work_id> reports the error." % (work, code)]
    if entries:
        decisions, total, pending, passed = _log_state(entries)
        return (others, _evidence_variants("Open evidence of work " + work, pending, passed),
                _decision_variants("Decisions of work " + work, [_decision_line(e) for e in decisions], total))
    chain = next((c for c in chains if c[0]["payload"]["work_id"] == work), [])
    old = next((card for card in chain if card.get("schema_version") == 1), None)
    if old is None:
        return others, [], ["Decision log of work %s: no entries yet." % work]
    path = session_dir(root, session) / "records" / (old["revision"] + ".json")
    label = " (not migrated: no decision log yet; from its old-format card %s)" % path
    evidence, decisions = old["payload"]["evidence"], old["payload"]["decisions"]
    pending = [item for item in evidence if item["state"] != "pass-observed"]
    return (others, _evidence_variants("Open evidence of work " + work + label, pending, len(evidence) - len(pending)),
            _decision_variants("Decisions of work " + work + label, ["- " + text for text in decisions],
                               len(decisions)))


def _checkpoint_variants(header, payload, path):
    """Checkpoint section, richest first: payload (its digest when large), digest, path only."""
    if payload is None:
        return [header]
    return [header + _payload_text(payload), header + _digest_text(payload),
            header + "Payload omitted for size; full checkpoint: " + str(path)]


def _stored_facts(root, session):
    """(verified facts.json body, its path), or (None, None) when absent or unreadable."""
    try:
        data, path = facts.stored(root, session)
        return (data, path) if data is not None else (None, None)
    except (StoreError, OSError):
        return None, None


def _facts_variants(data, path):
    """(facts summary variants, edited file list variants); both empty without a verified facts.json."""
    if data is None:
        return [], []
    try:
        info = facts.summary(data, path)
        files = facts.recent_files(data)
    except (KeyError, TypeError, AttributeError, ValueError):
        return [], []
    lines = ["Session facts (hook observations; git decides current state): " + info["path"]]
    for repo in info["repos"]:
        lines.append("- %s [%s] dirty %d/%d, session commits %d%s" % (
            repo["root"], repo["branch"] or "unknown", repo["dirty"], repo["dirty_total"], repo["commits"],
            "; error " + repo["error"] if repo["error"] else ""))
    if info["repos_omitted"]:
        lines.append("- %d more repos not listed" % info["repos_omitted"])
    agents = ", ".join("%s x%d" % item for item in info["agents"].items()) or "none"
    if info["agents_omitted"]:
        agents += " (+%d older not listed)" % info["agents_omitted"]
    lines += ["Subagents dispatched: " + agents, "Edits outside repos: %d" % info["outside_repo_count"]]
    note = _capture_note(info)
    summary = ["\n".join(lines + [note] if note else lines),
               "Session facts omitted for size: " + info["path"] + ("\n" + note if note else "")]
    # files_omitted: oldest files the facts size cap dropped; they are in neither list nor facts.json.
    more, dropped = max(0, len(files) - RECOVERY_FILES), info["files_omitted"]
    if not files:
        return summary, (["Edited repo files: %d older not kept" % dropped] if dropped else [])
    listed = ["Edited repo files, most recent first:"] + ["- " + f for f in files[:RECOVERY_FILES]]
    if more and dropped:
        listed.append("- %d more not listed: %d in %s, %d older not kept" % (more + dropped, more, info["path"], dropped))
    elif more:
        listed.append("- %d more not listed; full list: %s" % (more, info["path"]))
    elif dropped:
        listed.append("- %d more not listed: older, not kept" % dropped)
    shrunk = ("Edited repo files omitted for size; %d in %s, %d older not kept" % (len(files), info["path"], dropped)
              if dropped else "Edited repo files omitted for size; full list: " + info["path"])
    return summary, ["\n".join(listed), shrunk]


def _minutes(seconds):
    return "less than 1 min" if seconds < 60 else "%d min" % (seconds // 60)


def _count(number, noun, plural=None):
    """"1 dispatch", "2 dispatches": number and noun in agreement."""
    return "%d %s" % (number, noun if number == 1 else plural or noun + "s")


def _since_variants(data, path, checkpoint):
    """Activity captured after the checkpoint's recorded_at (all of it without a checkpoint):
    the full section, then its headline only. Empty without facts or when unreadable."""
    if data is None:
        return []
    try:
        recorded = _utc(checkpoint["recorded_at"]).timestamp() if checkpoint is not None else None
        found = facts.since(data, recorded)
        counts = "%s / %s" % (_count(len(found["dispatches"]), "dispatch", "dispatches"),
                              _count(found["commits"], "commit"))
        if checkpoint is None:
            head = ["Since the last record: no decision record in this session yet; %s captured, none recorded."
                    % counts]
        else:
            latest = found["latest"]
            if latest is None and not found["dispatches"] and not found["commits"]:
                line = "No activity captured after the checkpoint."  # no time at all: no freshness claim
            else:
                line = ("Checkpoint is %s older than the latest captured activity; " % _minutes(latest - recorded)
                        if latest is not None and latest > recorded
                        else "Checkpoint is not older than the latest captured activity; ") + counts + " after it."
            if found["undated"]:
                line += (" 1 commit has no time and is not counted." if found["undated"] == 1
                         else " %d commits have no time and are not counted." % found["undated"])
            head = ["Since the last record (recorded_at %s):" % checkpoint["recorded_at"], line]
        lines = list(head)
        shown = found["dispatches"][-RECOVERY_DISPATCHES:]
        if len(found["dispatches"]) > len(shown):
            lines.append("- %s not listed" % _count(len(found["dispatches"]) - len(shown), "earlier dispatch",
                                                     "earlier dispatches"))
        lines += ["- dispatched " + (a["role"] + ": " + a["desc"] if a["desc"] else a["role"]) for a in shown]
        for repo in found["repos"]:
            commits = repo["commits"][:RECOVERY_COMMITS]
            more = len(repo["commits"]) - len(commits)
            lines.append(_clip("- %s: %s: %s%s" % (
                repo["root"], _count(len(repo["commits"]), "commit"), "; ".join(c["hash"] + " " + c["subject"] for c in commits),
                " (+%d more)" % more if more else ""), RECOVERY_LINE_LIMIT))
    except (KeyError, TypeError, AttributeError, ValueError, OverflowError):
        return []
    return ["\n".join(lines), "\n".join(head + ["Details: " + str(path)])]


def _capture_note(info):
    """Collection anomaly line, else "". facts.json is written only by the PreCompact fold (and the
    SessionStart(compact) refold after it), which runs only for a session with the enablement
    marker: zero events there means capture is not reaching
    the spool (hook not installed, not firing, or failing)."""
    events, errors = info.get("events_captured"), info.get("spool_errors") or 0
    if not errors and events != 0:
        return ""
    note = "Facts capture: %s events" % ("unknown" if events is None else events)
    if errors:
        note += ", %d unreadable spool lines" % errors
    if events == 0:
        note += " — capture may be failing"
    return note


def _fit(sections, steps, budget=SESSION_START_BUDGET):
    """Join sections (each a list of variants, richest first) in display order within budget.

    Each entry of steps moves that section to its next smaller variant while the text
    is over budget, so a section is replaced whole and never cut. Only when every step
    still overflows is the text clipped: SessionStart never fails for its size.
    """
    chosen = [0] * len(sections)

    def text():
        return "\n".join(s[i] for s, i in zip(sections, chosen) if s and s[i])

    for index in steps:
        if len(text()) > budget and chosen[index] + 1 < len(sections[index]):
            chosen[index] += 1
    result = text()
    return result if len(result) <= budget else _clip(result, budget)


def _policy():
    return ("Native handoff runtime active. Follow personal CLAUDE.md for orchestration. "
            "This context supplies runtime identity and operations, not a second dispatch policy.")


def _marker(config_path, session):
    """Path of the session's facts enablement marker (<config dir>/cache/facts/<session>.enabled),
    None for a session id the capture hook would never use."""
    if not isinstance(session, str) or not CACHE_SESSION.fullmatch(session):
        return None
    cache = Path(config_path).parent / "cache"
    marker = cache / "facts" / (session + "." + FACTS_MARKER)
    guard(cache, marker)
    return marker


def _enable_facts(config_path, session):
    """Best effort: create cache/facts (the capture hook appends there but cannot create it) and the
    session's enablement marker, which native-facts and the PreCompact fold require."""
    try:
        marker = _marker(config_path, session)
        if marker is not None:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
    except (StoreError, OSError):
        pass


def facts_enabled(config_path, session):
    """True when the session's enablement marker exists; an unreadable cache counts as absent."""
    try:
        marker = _marker(config_path, session)
        return marker is not None and marker.is_file()
    except (StoreError, OSError):
        return False


def fold_facts(config_path, root, session):
    """Fold the facts spool before the snapshot. Never raises: returns (facts, None) or (None, code)."""
    try:
        return facts.fold(config_path, root, session), None
    except StoreError as exc:
        return None, str(exc)
    except Exception:  # facts must never decide or block compaction
        return None, "FACTS_FAILED"


def _utc(text):
    when = datetime.fromisoformat(text)
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def save_level(checkpoint, saved):
    """partial-missing / partial-stale code for a facts-only save, None when the record is current."""
    if checkpoint is None:
        return "partial-missing"
    changed = saved.get("last_change_at") if isinstance(saved, dict) else None
    if not changed:
        return None
    try:
        return "partial-stale" if _utc(checkpoint["recorded_at"]) < _utc(changed) else None
    except (KeyError, TypeError, ValueError):  # freshness unprovable: not complete
        return "partial-stale"


def _event_key(event, checkpoint):
    return digest(json.dumps({"trigger": event.get("trigger"), "cursor": cursor(resolve_transcript(event.get("transcript_path"))),
                              "revision": checkpoint["revision"] if checkpoint else None}, sort_keys=True))


def handle_event(event, config_path):
    name = event.get("hook_event_name") if isinstance(event, dict) else None
    source = event.get("source", "startup") if isinstance(event, dict) else "startup"
    key = None
    try:
        if name not in ("SessionStart", "PreCompact", "PostCompact"):
            return {}
        cfg = load_config(config_path)
        if cfg.get("mode") != "adaptive":
            return {}
        if (not isinstance(event.get("cwd"), str) or not Path(event["cwd"]).is_absolute()
                or not isinstance(event.get("session_id"), str) or not event["session_id"]):
            raise StoreError("INVALID_HOOK_INPUT")
        root = workspace_root(event["cwd"])
        session = event["session_id"]
        if resolve_mode(cfg, root.root, root.main) != "adaptive":
            return {}
        enabled = cfg["handoff"]
        state_dir = config_path.parent / "cache" / "notifications"
        key = root.repo_key + ":" + session
        if name == "SessionStart":
            policy = _policy()
            runtime = str(PLUGIN / "hooks/runtime")
            info = ("\nRuntime: " + runtime
                    + "\nWorkspace: " + str(root.root) + "\nSession: " + session
                    + "\nWriter limits: normal=" + str(cfg["max_writers"]) + ", burst=" + str(cfg["burst_writers"])
                    + "\nHandoff: " + ("enabled" if enabled else "disabled; do not record or snapshot")
                    + "\nSource guides: " + str(PLUGIN / "docs/compatibility")
                    + "\nUse doctor/status/record/snapshot/list/resume/changes/search/prune through the runtime."
                    + " Start a save with status (read-only: expected revision and a complete template);"
                    + " record reads JSON from --input <payload-file>.")
            if enabled:
                info += ("\nOperations guide: " + str(PLUGIN / "docs/native-handoff.md")
                         + "\nUse Bash: bash <quoted-runtime-path> status --workspace <quoted-workspace> --session <quoted-session>."
                         + "\nStatus is read-only and returns expected revision, checkpoint and a complete template;"
                         + " other_works lists the session's other work_ids."
                         + "\nFill the template with actual sources/decisions, then record --workspace <root> --session <id>"
                         + " --expected <revision-or-null> --request-id <unique-milestone-id> --input <payload-file>."
                         + "\nExpected is per work_id: when the payload's work_id is not the checkpoint's, take expected"
                         + " from status --work <quoted-work-id> (null starts that work's chain)."
                         + "\nRecord at plan approval, dispatch, result receipt or changed decisions/blockers."
                         + " Use snapshot for explicit /handoff. Never treat an unfilled template as saved work."
                         + "\nOn conflict rerun status (with --work for that work_id) and reconcile; do not force overwrite. A worktree and its main checkout share one handoff store.")
            # Keep identity and restrictions; never slice a decision halfway.
            short = "Native handoff runtime. Personal policy remains authoritative."
            shortest = (short + "\nRuntime: " + runtime + "\nWorkspace: " + str(root.root) + "\nSession: " + session + "\n"
                        + ("Use Bash: bash <quoted-runtime-path> status --workspace <quoted-workspace> --session <quoted-session>."
                           if enabled else "Handoff: disabled; do not record or snapshot."))
            # Display order: identity, save status, facts, checkpoint, other open works, open evidence,
            # decisions, activity since the checkpoint, edited files.
            sections = [[policy + info, short + info, shortest], [], [], [], [], [], [], [], []]
            status, warning, partial = [], "", False
            if enabled:
                # Only a session PreCompact could fold for: the marker this SessionStart creates does not count.
                refold = source == "compact" and facts_enabled(config_path, session)
                _enable_facts(config_path, session)
                try:
                    bind(root, session, event)
                    recovering = source in ("compact", "resume")
                    if recovering:
                        if refold:
                            # Auto compaction runs PreCompact minutes before the boundary: fold that gap in.
                            # Never raises; on failure the PreCompact facts.json is rendered unchanged.
                            fold_facts(config_path, root, session)
                        # Facts first: a corrupt checkpoint raises and must not drop them.
                        stored, stored_path = _stored_facts(root, session)
                        sections[2], sections[8] = _facts_variants(stored, stored_path)
                        chains = _chains(root, session, "record")[0]
                        header, payload, path, checkpoint = _recovery(root, session, chains)
                        sections[3] = _checkpoint_variants(header, payload, path)
                        sections[7] = _since_variants(stored, stored_path, checkpoint)
                        sections[4], sections[5], sections[6] = _work_sections(root, session, chains, checkpoint)
                    elif source in ("startup", "clear", "fork"):
                        status.append("New work context: do not load or start old work unless explicitly requested.")
                    else:
                        status.append("Unrecognized session source: no automatic continuation.")
                    cache = state_dir / (digest(key) + ".json")
                    guard(state_dir.parent, cache)
                    if cache.exists():
                        try:
                            saved = json.loads(cache.read_text(encoding="utf-8"))
                            if isinstance(saved, dict) and saved.get("error") in PARTIAL:
                                partial = True
                                warning = ("Provost: last compaction saved facts only; write a decision record from the "
                                           "compaction summary before continuing (run status, then record).")
                            elif (isinstance(saved, dict) and saved.get("error") == "NO_CHECKPOINT"
                                  and _never_recorded(root, session)):
                                warning = "Provost: " + NEVER_RECORDED
                            elif isinstance(saved, dict) and saved.get("error"):
                                warning = "Provost: previous handoff save failed; verify any older checkpoint before continuing."
                        except (OSError, UnicodeError, json.JSONDecodeError):
                            warning = "Provost: save health is unknown; verify checkpoint freshness."
                    if recovering and warning:
                        status.append(PARTIAL_STATUS if partial else "Save status: " + warning[len("Provost: "):])
                except (StoreError, OSError) as exc:
                    code = str(exc) if isinstance(exc, StoreError) else "READ_FAILED"
                    warning = notice(key, code, state_dir)["message"]
                    status.append("Checkpoint unavailable; verify current sources. Error: " + code)
            sections[1] = ["\n".join(status)] if status else []
            # Edited files go first; then decisions drop one at a time, oldest shown first, so they fill
            # what the other sections leave; then open evidence and other works shrink to counts. The
            # activity since the checkpoint shrinks to its headline (which keeps the staleness line)
            # before the checkpoint loses its digest.
            steps = (8,) + (6,) * len(sections[6]) + (5, 4, 3, 7, 3, 2, 0, 0)
            return _context(_fit(sections, steps)) | _notification(warning, True)
        if not enabled:
            return {}
        if name == "PreCompact":
            if not facts_enabled(config_path, session):
                # A session no native SessionStart enabled: save exactly as before
                # the facts layer (a snapshot, or NO_CHECKPOINT without a record).
                checkpoint = current(root, session)
                snapshot(root, session, _event_key(event, checkpoint))
                return _notification(notice(key, None, state_dir)["message"])
            saved, facts_error = fold_facts(config_path, root, session)
            checkpoint = current(root, session)
            if checkpoint is None:
                # partial-missing: facts alone are the save; no empty snapshot. failed: neither saved.
                code = facts_error or ("partial-missing" if saved is not None else "NO_CHECKPOINT")
                return _notification(notice(key, code, state_dir)["message"])
            snapshot(root, session, _event_key(event, checkpoint))
            if facts_error:
                return _notification(notice(key, facts_error, state_dir, record_saved=True)["message"])
            return _notification(notice(key, save_level(checkpoint, saved), state_dir)["message"])
        # Metadata only: the compaction summary may contain sensitive context.
        storage_ready(root)
        atomic_write(handoff_home(), session_dir(root, session) / "compaction.json",
                     {"observed": True, "workspace_id": root.repo_key, "session_id": session,
                      "cursor": cursor(resolve_transcript(event.get("transcript_path")))})
        return {}
    except (ConfigError, StoreError, OSError) as exc:
        code = str(exc) if isinstance(exc, (ConfigError, StoreError)) else "RUNTIME_IO_FAILED"
        if name == "SessionStart":
            return _notification("Provost adaptive unavailable: " + code
                                 + ". Personal policy unchanged; handoff unavailable.", True)
        if name in ("PreCompact", "PostCompact"):
            if key is None:
                key = str(event.get("session_id", "unknown"))
                try:
                    key = workspace_key(Path(event["cwd"])) + ":" + key
                except (KeyError, TypeError, ValueError):
                    pass
            message = notice(key, code, config_path.parent / "cache" / "notifications")["message"]
            return _notification(message)
        return {}
