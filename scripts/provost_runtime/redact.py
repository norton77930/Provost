"""Maintainer-only redaction of one decision log entry (docs/native-handoff.md, Redact).

The entry's content (a decision's text; an evidence entry's claim and ref) becomes a
fixed marker; its id, kind, time, session, record and links stay, so every chain that
points at it still resolves. The whole log is rewritten under the per-work log lock
only (never a session lock: record takes the session lock first, then the log lock).
"""
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import store
from .store import REMOVED, StoreError
CONTENT = {"decision": ("text",), "evidence": ("claim", "ref")}
MAX_REASON = 1000


def redact(root, work_id, entry_id, reason=None):
    """Redact entry_id of work_id's log; {work_id, entry, changed}. An already redacted entry is left as it is."""
    if not isinstance(work_id, str) or not work_id or not isinstance(entry_id, str):
        raise StoreError("INVALID_ARGUMENTS")
    if reason is not None and (not isinstance(reason, str) or not reason or len(reason) > MAX_REASON):
        raise StoreError("INVALID_REASON")
    ws = store.as_workspace(root)
    home = store.handoff_home()
    path, _ = store._log_paths(ws, work_id)
    store.guard(home, path)
    if not path.exists():  # a mistyped work: refuse before the lock creates a stray .lock file
        raise StoreError("ENTRY_NOT_FOUND")
    with store._log_lock(ws, work_id):
        store.guard(home, path)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            raise StoreError("ENTRY_NOT_FOUND") from None
        except OSError as exc:
            raise StoreError("WRITE_FAILED") from exc
        entries, end = store._parse_log(ws, work_id, raw)
        index = next((i for i, entry in enumerate(entries) if entry["id"] == entry_id), None)
        if index is None:
            raise StoreError("ENTRY_NOT_FOUND")
        entry = entries[index]
        if "redacted_at" in entry:
            return {"work_id": work_id, "entry": entry, "changed": False}
        entry = {**entry, **dict.fromkeys(CONTENT[entry["kind"]], REMOVED),
                 "redacted_at": datetime.now(timezone.utc).isoformat(),
                 **({"redaction_reason": reason} if reason is not None else {})}
        # Committed lines are kept byte for byte except the redacted one; a torn tail
        # (bytes past `end`) is dropped, and the file still ends with a newline.
        lines = raw[:end].split(b"\n")[:-1]
        lines[index + 1] = store.encoded(entry)  # line 0 is the header
        data = b"".join(line + b"\n" for line in lines)
        store._parse_log(ws, work_id, data)  # never write a log the reader would refuse
        _replace(home, path, data)
    return {"work_id": work_id, "entry": entry, "changed": True}


def _replace(home, path, data):
    """store.atomic_write for raw bytes: temp file in the log dir, fsync, os.replace.

    On Windows os.replace fails with PermissionError while a reader has the log open;
    that is WRITE_FAILED, and the log is unchanged, so the redact can be retried.
    """
    store.guard(home, path)
    temp = None
    try:
        fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
        temp = Path(name)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        temp = None
    except OSError as exc:
        raise StoreError("WRITE_FAILED") from exc
    finally:
        if temp is not None:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass  # best effort: never mask the write result or its error
    if os.name != "nt":  # best effort: make the rename durable so the old content cannot return
        try:
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            pass  # the replace is done; a later write or the OS flushes the dir
