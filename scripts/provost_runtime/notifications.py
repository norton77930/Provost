"""Best-effort, non-sensitive notification state; never block compaction."""
import json
from pathlib import Path
from .store import atomic_write, digest, guard, StoreError

# Save levels below complete that are not failures: facts were saved, the decision record lags.
PARTIAL = {"partial-missing": "Provost: facts saved; decision record missing.",
           "partial-stale": "Provost: facts saved; decision record is older than the latest change."}


def _message(code, level, record_saved):
    if code in PARTIAL:
        return PARTIAL[code]
    if record_saved:
        return "Provost: decision record saved; facts not saved. " + code
    return ("Provost: handoff not saved; compaction continues. " if level == "first"
            else "Provost: handoff save still failing. ") + code


def notice(session_key: str, error_code: str | None, state_dir: Path, *, record_saved: bool = False) -> dict:
    """Dedupe per session: an error or partial code is first/repeat; a later clean save is recovered.

    record_saved marks an error that lost only the facts (the decision record was snapshotted).
    """
    path = state_dir / (digest(session_key) + ".json")
    try:
        guard(state_dir.parent, path)
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(previous, dict):
                previous = {}
        except (FileNotFoundError, UnicodeError, json.JSONDecodeError):
            previous = {}
        old = previous.get("error")
        if error_code:
            level = "repeat" if old == error_code else "first"
            message = _message(error_code, level, record_saved)
        elif old:
            level, message = "recovered", "Provost: handoff saving recovered."
        else:
            return {"level": "none", "message": ""}
        atomic_write(state_dir, path, {"error": error_code})
        return {"level": level, "message": message}
    except (OSError, StoreError):
        if error_code in PARTIAL or (error_code and record_saved):
            detail = " " + _message(error_code, "first", record_saved)[len("Provost: "):]
        else:
            detail = " Handoff not saved; compaction continues. " + error_code if error_code else ""
        return {"level": "untracked", "message": "Provost: notification deduplication unavailable." + detail}
