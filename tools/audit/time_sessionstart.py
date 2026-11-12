# Profile SessionStart handling against a COPY of the handoff store, never the live one:
# SessionStart folds facts and writes bindings, so this refuses to run unless PROVOST_CONFIG and
# PROVOST_HANDOFF_HOME both point outside the default <config dir>/provost.
# Usage (after copying config.json, cache/ and handoffs/ somewhere temporary):
#   PROVOST_CONFIG=<copy>/config.json PROVOST_HANDOFF_HOME=<copy>/handoffs \
#     python -B tools/audit/time_sessionstart.py <session-id> <transcript.jsonl> [<cwd>]
import cProfile
import io
import os
import pstats
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
live = (Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "provost").resolve()
for name in ("PROVOST_CONFIG", "PROVOST_HANDOFF_HOME"):
    value = os.environ.get(name)
    if not value:
        sys.exit("%s is not set: point it at a copy of the store" % name)
    target = Path(value).resolve()
    if target == live or live in target.parents:
        sys.exit("%s points into the live store (%s): use a copy" % (name, live))
if len(sys.argv) < 3:
    sys.exit("usage: time_sessionstart.py <session-id> <transcript.jsonl> [<cwd>]")

sys.path.insert(0, str(REPO / "scripts"))
calls = []
_run = subprocess.run


def counted(cmd, *a, **k):
    t = time.perf_counter()
    try:
        return _run(cmd, *a, **k)
    finally:
        calls.append((" ".join(str(c) for c in cmd[:7])[:100], (time.perf_counter() - t) * 1000))


subprocess.run = counted
t0 = time.perf_counter()
from provost_runtime.config import config_path  # noqa: E402
from provost_runtime.hooks import handle_event  # noqa: E402

imp = (time.perf_counter() - t0) * 1000
sess, transcript = sys.argv[1], sys.argv[2]
cwd = sys.argv[3] if len(sys.argv) > 3 else str(REPO)
for source in ("compact", "startup", "compact"):
    calls.clear()
    event = {"hook_event_name": "SessionStart", "source": source, "session_id": sess, "cwd": cwd, "transcript_path": transcript}
    prof = cProfile.Profile()
    t = time.perf_counter()
    prof.enable()
    out = handle_event(event, config_path())
    prof.disable()
    total = (time.perf_counter() - t) * 1000
    ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext", "")
    print("SessionStart:%s total %.0f ms (import %.0f ms) | child processes %d taking %.0f ms | additionalContext %d chars" % (source, total, imp, len(calls), sum(c[1] for c in calls), len(ctx)))
    for name, ms in calls:
        print("   %.0f ms  %s" % (ms, name))
    s = io.StringIO()
    pstats.Stats(prof, stream=s).sort_stats("cumulative").print_stats(14)
    for line in s.getvalue().splitlines():
        if "provost_runtime" in line or ("subprocess.py" in line and "run" in line):
            print("     " + line.strip()[:150])
