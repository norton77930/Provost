# Read-only timing of the acceptance gate's in-process parts, using this checkout's runtime source.
# Usage: python -B tools/audit/time_gate.py [<cwd>] [<transcript.jsonl> ...]
# <cwd> defaults to this checkout. Without transcripts it takes the median and the largest one
# under <config dir>/projects (CLAUDE_CONFIG_DIR, else ~/.claude).
import glob
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
t0 = time.perf_counter()
sys.path.insert(0, str(REPO / "scripts"))
from provost_runtime import acceptance  # noqa: E402

print("import provost_runtime.acceptance: %.0f ms" % ((time.perf_counter() - t0) * 1000))
cwd = sys.argv[1] if len(sys.argv) > 1 else str(REPO)
transcripts = sys.argv[2:]
if not transcripts:
    config = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    found = sorted(glob.glob(os.path.join(config, "projects", "*", "*.jsonl")), key=os.path.getsize)
    transcripts = [found[len(found) // 2], found[-1]] if found else []
for tp in transcripts:
    ev = {"cwd": cwd, "transcript_path": tp, "session_id": "00000000-0000-0000-0000-000000000000", "hook_event_name": "Stop"}
    size = os.path.getsize(tp) / 1e6
    d = acceptance.acceptance_directory(ev)
    a = time.perf_counter()
    owned = acceptance.session_files(ev, d) if d else None
    b = time.perf_counter()
    try:
        acceptance.last_edit(ev)
    except Exception as exc:  # timing only; report and keep going
        print("last_edit error %s" % type(exc).__name__)
    c = time.perf_counter()
    print("transcript %.1f MB: session_files %.0f ms (owned=%s) | last_edit %.0f ms" % (size, (b - a) * 1000, None if owned is None else len(owned), (c - b) * 1000))
for i in range(2):
    a = time.perf_counter()
    td = acceptance.tree_digest(cwd)
    b = time.perf_counter()
    print("tree_digest %s run %d: %.0f ms (%s)" % (cwd, i + 1, (b - a) * 1000, "ok" if td else "None"))
