"""Claude event adapter. Saving errors must never become compaction decisions."""
import json
from datetime import datetime, timezone
from pathlib import Path

from . import legacy
from .config import ConfigError, handoff_home, load_config, resolve_mode, workspace_key
from .notifications import notice
from .store import (StoreError, _base, as_workspace, atomic_write, current, digest, guard, latest,
                    locate, session_dir, snapshot, storage_ready)

PLUGIN = Path(__file__).resolve().parents[2]
RECOVERY_PAYLOAD_LIMIT = 4500     # inline the payload in full up to this size; digest is capped to it too
RECOVERY_ASSIGNMENT_LIMIT = 300   # per-assignment JSON cap inside the digest


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


def _legacy(source):
    if source not in ("startup", "clear", "compact"):
        return {}
    return _context((PLUGIN / "orchestration.md").read_text(encoding="utf-8"))


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


def _payload_text(payload):
    full = json.dumps(payload, ensure_ascii=False)
    if len(full) <= RECOVERY_PAYLOAD_LIMIT:
        return "Observed payload: " + full
    digest = {"work_id": payload["work_id"], "phase": payload["phase"], "next": payload["next"],
              "blockers": payload["blockers"],
              # Stored keys are sorted; lead with task/agent_id so a cut keeps the identifying fields.
              "assignments": [_clip(json.dumps({k: item[k] for k in ("task", "agent_id", "observed_state", "write_scope")},
                                               ensure_ascii=False), RECOVERY_ASSIGNMENT_LIMIT)
                              for item in payload["assignments"]],
              "decisions": payload["decisions"][-3:]}
    return ("Observed payload digest (full JSON in the checkpoint file): "
            + _clip(json.dumps(digest, ensure_ascii=False), RECOVERY_PAYLOAD_LIMIT))


def recovery_context(root, session):
    # The latest milestone may be newer than the last compaction snapshot.
    root = as_workspace(root)
    data = current(root, session)
    if data is None:
        data = latest(root, session)
    source = ""
    if data is None:
        # Only a session with nothing in the new store may read the old per-repo store.
        found = legacy.checkpoint(root, session)
        if found is None:
            return "No checkpoint for this session. Verify current sources before continuing."
        data, path = found
        source = "Checkpoint source: legacy store (read-only); the first record starts a fresh chain in the new store.\n"
    else:
        kind = "records" if data["kind"] == "record" else "snapshots"
        ident = data["revision"] if kind == "records" else data["snapshot_id"]
        path = session_dir(root, session) / kind / (ident + ".json")
    payload = _payload_text(data["payload"])
    return (source + "Saved observations, not current facts. Verify sources, code, evidence and worker state before acting. "
            "Do not duplicate a possibly running worker. Checkpoints can lag later decisions.\n"
            + "Checkpoint: " + str(path) + "\nRevision: " + data["revision"]
            + "\nTranscript cursor: " + ("available (not a freshness guarantee)" if data.get("transcript_cursor") else "unavailable; gaps must be checked")
            + "\nRead this JSON file for the complete checkpoint.\n"
            + "If any assignment is not marked completed, verify that worker's actual state before dispatching; "
            + "read the checkpoint file for full details.\n"
            + payload)


def handle_event(event, config_path, *, native=False):
    name = event.get("hook_event_name") if isinstance(event, dict) else None
    source = event.get("source", "startup") if isinstance(event, dict) else "startup"
    key = None
    try:
        if name not in ("SessionStart", "PreCompact", "PostCompact"):
            return {}
        cfg = load_config(config_path)
        if cfg.get("mode") != "adaptive":
            return _legacy(source) if name == "SessionStart" and not native else {}
        if (not isinstance(event.get("cwd"), str) or not Path(event["cwd"]).is_absolute()
                or not isinstance(event.get("session_id"), str) or not event["session_id"]):
            raise StoreError("INVALID_HOOK_INPUT")
        root = workspace_root(event["cwd"])
        session = event["session_id"]
        if resolve_mode(cfg, root.root, root.main) != "adaptive":
            if native:
                return {}
            if name == "SessionStart":
                return _legacy(source) | _notification(
                    "Provost adaptive not enabled for this workspace; legacy behavior retained.", True)
            return {}
        enabled = cfg["handoff"]
        state_dir = config_path.parent / "cache" / "notifications"
        key = root.repo_key + ":" + session
        if name == "SessionStart":
            policy = ("Native handoff runtime active. Follow personal CLAUDE.md for orchestration. "
                      "This context supplies runtime identity and operations, not a second dispatch policy."
                      if native else (PLUGIN / "policies/adaptive-orchestration.md").read_text(encoding="utf-8"))
            info = ("\nRuntime: " + str(PLUGIN / "hooks/runtime")
                    + "\nWorkspace: " + str(root.root) + "\nSession: " + session
                    + "\nWriter limits: normal=" + str(cfg["max_writers"]) + ", burst=" + str(cfg["burst_writers"])
                    + "\nHandoff: " + ("enabled" if enabled else "disabled; do not record or snapshot")
                    + "\nSource guides: " + str(PLUGIN / "docs/compatibility")
                    + "\nUse doctor/list/record/snapshot/resume through the runtime; record reads JSON from --input <payload-file>.")
            if native and enabled:
                info += ("\nOperations guide: " + str(PLUGIN / "docs/native-handoff.md")
                         + "\nUse Bash: bash <quoted-runtime-path> status --workspace <quoted-workspace> --session <quoted-session>."
                         + "\nStatus is read-only and returns expected revision, checkpoint and a complete template."
                         + "\nFill the template with actual sources/decisions, then record --workspace <root> --session <id>"
                         + " --expected <revision-or-null> --request-id <unique-milestone-id> --input <payload-file>."
                         + "\nRecord at plan approval, dispatch, result receipt or changed decisions/blockers."
                         + " Use snapshot for explicit /handoff. Never treat an unfilled template as saved work."
                         + "\nOn conflict rerun status and reconcile; do not force overwrite. A worktree and its main checkout share one handoff store.")
            warning = ""
            if enabled:
                try:
                    bind(root, session, event)
                    if source in ("compact", "resume"):
                        info += "\n" + recovery_context(root, session)
                    elif source in ("startup", "clear", "fork"):
                        info += "\nNew work context: do not load or start old work unless explicitly requested."
                    else:
                        info += "\nUnrecognized session source: no automatic continuation."
                    cache = state_dir / (digest(key) + ".json")
                    guard(state_dir.parent, cache)
                    if cache.exists():
                        try:
                            saved = json.loads(cache.read_text(encoding="utf-8"))
                            if isinstance(saved, dict) and saved.get("error"):
                                warning = "Provost: previous handoff save failed; verify any older checkpoint before continuing."
                        except (OSError, UnicodeError, json.JSONDecodeError):
                            warning = "Provost: save health is unknown; verify checkpoint freshness."
                except (StoreError, OSError) as exc:
                    code = str(exc) if isinstance(exc, StoreError) else "READ_FAILED"
                    warning = notice(key, code, state_dir)["message"]
                    info += "\nCheckpoint unavailable; verify current sources. Error: " + code
            text = policy + info
            if len(text) > 8000:
                # Keep identity and restrictions; never slice a decision halfway.
                text = (("Native handoff runtime. Personal policy remains authoritative." if native else
                         "Provost adaptive mode. Read the policy before dispatch: "
                         + str(PLUGIN / "policies/adaptive-orchestration.md")) + info)
            if len(text) > 8000:
                raise StoreError("CONTEXT_TOO_LARGE")
            return _context(text) | _notification(warning, True)
        if not enabled:
            return {}
        if name == "PreCompact":
            checkpoint = current(root, session)
            event_key = digest(json.dumps({"trigger": event.get("trigger"), "cursor": cursor(resolve_transcript(event.get("transcript_path"))),
                                          "revision": checkpoint["revision"] if checkpoint else None}, sort_keys=True))
            snapshot(root, session, event_key)
            return _notification(notice(key, None, state_dir)["message"])
        # Metadata only: the compaction summary may contain sensitive context.
        storage_ready(root)
        atomic_write(handoff_home(), session_dir(root, session) / "compaction.json",
                     {"observed": True, "workspace_id": root.repo_key, "session_id": session,
                      "cursor": cursor(resolve_transcript(event.get("transcript_path")))})
        return {}
    except (ConfigError, StoreError, OSError) as exc:
        code = str(exc) if isinstance(exc, (ConfigError, StoreError)) else "RUNTIME_IO_FAILED"
        if name == "SessionStart":
            result = {} if native else _legacy(source)
            suffix = ". Personal policy unchanged; handoff unavailable." if native else ". Legacy behavior retained."
            result.update(_notification("Provost adaptive unavailable: " + code + suffix, True))
            return result
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
