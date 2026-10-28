import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from _support import REPO

SCRIPT = REPO / "tools" / "dispatch-report.js"
SESSION = "abcdef12-0000-0000-0000-000000000000"
PROJECT = "D--proj-x"


def jl(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(o) + "\n" for o in lines), encoding="utf-8")


def dispatch(tid, ts, subagent_type, desc, **extra):
    block = {"type": "tool_use", "id": tid, "name": "Agent",
             "input": {"subagent_type": subagent_type, "description": desc, **extra}}
    return {"type": "assistant", "timestamp": ts, "message": {"content": [block]}}


def result(tid, ts, agent_id, text="done"):
    return {"type": "user", "timestamp": ts, "toolUseResult": {"agentId": agent_id, "status": "completed"},
            "message": {"content": [{"type": "tool_result", "tool_use_id": tid,
                                     "content": [{"type": "text", "text": text}]}]}}


def say(ts, text):
    return {"type": "assistant", "timestamp": ts, "message": {"content": [{"type": "text", "text": text}]}}


def sub_lines(model, t0, t1):
    usage = {"input_tokens": 100, "output_tokens": 2000, "cache_read_input_tokens": 1000000,
             "cache_creation": {"ephemeral_5m_input_tokens": 200000, "ephemeral_1h_input_tokens": 0}}
    return [{"type": "assistant", "timestamp": t, "message": {"id": mid, "model": model, "usage": usage, "content": []}}
            for t, mid in ((t0, "m1"), (t1, "m2"))]


@unittest.skipUnless(shutil.which("node"), "node is required for tools/dispatch-report.js")
class DispatchReportTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.proj = self.home / ".claude" / "projects" / PROJECT

    def agent(self, agent_id, model, t0, t1):
        jl(self.proj / SESSION / "subagents" / f"agent-{agent_id}.jsonl", sub_lines(model, t0, t1))

    def run_report(self, *args):
        env = {**os.environ, "HOME": str(self.home), "USERPROFILE": str(self.home)}
        p = subprocess.run(["node", str(SCRIPT), *args], env=env, capture_output=True, text=True,
                           encoding="utf-8", check=True)
        return p.stdout.splitlines(), p.stderr

    def test_default_mode_lists_implementer_dispatches(self):
        jl(self.proj / f"{SESSION}.jsonl", [
            dispatch("t1", "2026-10-20T10:00:00Z", "implementer-sonnet", "Add widget"),
            result("t1", "2026-10-20T10:07:00Z", "a1"),
        ])
        self.agent("a1", "claude-sonnet-5-5", "2026-10-20T10:00:05Z", "2026-10-20T10:06:05Z")
        out, err = self.run_report()
        header = ("| ts | project | session | role | override | model | status | wallMin | turns | out | cacheReadM"
                  " | input | write5mM | write1hM | estCost | apiErrors | desc |")
        self.assertEqual(out[0], header)
        self.assertEqual(out[1], "|" + "|".join(["---"] * 17) + "|")
        self.assertEqual(out[2], "| 2026-10-20 10:00 | proj-x | abcdef12 | implementer-sonnet |  | sonnet-5-5 | completed"
                                 " | 6.0 | 2 | 4000 | 2.0 | 200 | 0.40 | 0.00 | 1.24 | 0 | Add widget |")
        self.assertEqual(len(out), 3)
        self.assertEqual(err.strip(), "1 dispatches since 2026-09-23; implementer-sonnet=1")

    def reviews_fixture(self):
        important = "Report: 4 Important\nI1 a.py:1 bug\nI2 b.py:2 bug\nI3 c.py:3 bug\nI4 d.py:4 bug"
        disp = "Reading the report.\nDispositions: I1 fixed; I2 rejected — wrong file; I3 intentional — by design; I4 moot — superseded"
        jl(self.proj / f"{SESSION}.jsonl", [
            dispatch("i1", "2026-10-20T09:00:00Z", "implementer-sonnet", "Build thing"),
            result("i1", "2026-10-20T09:05:00Z", "a0"),
            dispatch("r1", "2026-10-20T10:00:00Z", "code-reviewer", "Review one"),
            result("r1", "2026-10-20T10:05:00Z", "a1", important),
            say("2026-10-20T10:06:00Z", disp),
            dispatch("r2", "2026-10-20T11:00:00Z", "code-reviewer", "Review two", model="opus"),
            result("r2", "2026-10-20T11:05:00Z", "a2", "Two Important\nI1 x.py:1\nI2 y.py:2\nI1 again"),
            say("2026-10-20T11:06:00Z", "Fixing both now."),
        ])
        self.agent("a0", "claude-sonnet-5-5", "2026-10-20T09:00:05Z", "2026-10-20T09:04:05Z")
        self.agent("a1", "claude-fable-5-1", "2026-10-20T10:00:05Z", "2026-10-20T10:04:05Z")
        self.agent("a2", "claude-opus-5-5", "2026-10-20T11:00:05Z", "2026-10-20T11:04:05Z")

    def test_reviews_mode_counts_findings_and_dispositions(self):
        self.reviews_fixture()
        out, err = self.run_report("--reviews", "--tsv")
        self.assertEqual(out[0], "ts\tproject\tsession\tmodel\timportant\tfixed\trejected\tintentional\tmoot\tunrecorded\tdesc")
        self.assertEqual(out[1:], [
            "2026-10-20 10:00\tproj-x\tabcdef12\tfable-5-1\t4\t1\t1\t1\t1\t0\tReview one",
            "2026-10-20 11:00\tproj-x\tabcdef12\topus-5-5\t2\t0\t0\t0\t0\t2\tReview two",
        ])
        self.assertIn("fable-5-1: reviews=1 important=4 fixed=1 rejected=1 intentional=1 moot=1 unrecorded=0 precision=25%", err)
        self.assertIn("opus-5-5: reviews=1 important=2 fixed=0 rejected=0 intentional=0 moot=0 unrecorded=2 precision=n/a", err)

    def test_reviews_mode_markdown_table(self):
        self.reviews_fixture()
        out, _ = self.run_report("--reviews")
        self.assertTrue(out[0].startswith("| ts | project | session | model | important |"))
        self.assertEqual(out[1], "|" + "|".join(["---"] * 11) + "|")
        self.assertEqual(len(out), 4)


if __name__ == "__main__":
    unittest.main()
