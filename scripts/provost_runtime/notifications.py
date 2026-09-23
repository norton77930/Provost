"""Best-effort, non-sensitive notification state; never block compaction."""
import json
from pathlib import Path
from .store import atomic_write, digest, guard, StoreError


def notice(session_key: str, error_code: str | None, state_dir: Path) -> dict:
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
            message = ("Provost: handoff not saved; compaction continues. " if level == "first"
                       else "Provost: handoff save still failing. ") + error_code
        elif old:
            level, message = "recovered", "Provost: handoff saving recovered."
        else:
            return {"level": "none", "message": ""}
        atomic_write(state_dir, path, {"error": error_code})
        return {"level": level, "message": message}
    except (OSError, StoreError):
        return {"level": "untracked",
                "message": "Provost: notification deduplication unavailable." +
                           (" Handoff not saved; compaction continues. " + error_code if error_code else "")}
