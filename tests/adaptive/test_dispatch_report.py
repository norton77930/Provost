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


def async_result(tid, ts, agent_id):
    text = f"Async agent launched successfully.\nagentId: {agent_id} (internal ID)\nThe agent is working in the background."
    return {"type": "user", "timestamp": ts, "toolUseResult": {"isAsync": True, "status": "async_launched", "agentId": agent_id},
            "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": [{"type": "text", "text": text}]}]}}


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
        self.assertEqual(out[0], "ts\tproject\tsession\tmodel\timportant\tfixed\trejected\tintentional\tmoot\tunrecorded\tdesc\ttagged\treport")
        self.assertEqual(out[1:], [
            "2026-10-20 10:00\tproj-x\tabcdef12\tfable-5-1\t4\t1\t1\t1\t1\t0\tReview one\tyes\t",
            "2026-10-20 11:00\tproj-x\tabcdef12\topus-5-5\t2\t0\t0\t0\t0\t2\tReview two\tyes\t",
        ])
        self.assertIn("fable-5-1: reviews=1 important=4 fixed=1 rejected=1 intentional=1 moot=1 unrecorded=0 tagged=1 precision=25%", err)
        self.assertIn("opus-5-5: reviews=1 important=2 fixed=0 rejected=0 intentional=0 moot=0 unrecorded=2 tagged=1 precision=n/a", err)

    def test_reviews_mode_markdown_table(self):
        self.reviews_fixture()
        out, _ = self.run_report("--reviews")
        self.assertTrue(out[0].startswith("| ts | project | session | model | important |"))
        self.assertEqual(out[1], "|" + "|".join(["---"] * 13) + "|")
        self.assertEqual(len(out), 4)

    def async_fixture(self):
        report = "Review done\nI1 a.py:1 bug\nI2 b.py:2 bug"
        jl(self.proj / f"{SESSION}.jsonl", [
            dispatch("r1", "2026-10-20T10:00:00Z", "code-reviewer", "Review async"),
            async_result("r1", "2026-10-20T10:00:02Z", "a1"),
            say("2026-10-20T10:06:00Z", "Dispositions: I1 fixed; I2 rejected - nope"),
        ])
        lines = sub_lines("claude-fable-5-1", "2026-10-20T10:00:05Z", "2026-10-20T10:04:05Z")
        draft = say("2026-10-20T10:03:00Z", "Draft: I7 and I8 and I9 maybe")
        final = say("2026-10-20T10:04:05Z", report)
        thinking = {"type": "assistant", "timestamp": "2026-10-20T10:04:06Z",
                    "message": {"content": [{"type": "thinking", "thinking": "I5"}]}}
        # The report is the text-bearing line with the latest timestamp, wherever it sits in the file.
        jl(self.proj / SESSION / "subagents" / "agent-a1.jsonl", [lines[0], final, draft, thinking, lines[1]])

    def test_reviews_mode_reads_report_of_background_dispatch(self):
        self.async_fixture()
        out, err = self.run_report("--reviews", "--tsv")
        self.assertEqual(out[1:], ["2026-10-20 10:00\tproj-x\tabcdef12\tfable-5-1\t2\t1\t1\t0\t0\t0\tReview async\tyes\t"])
        self.assertIn("fable-5-1: reviews=1 important=2 fixed=1 rejected=1 intentional=0 moot=0 unrecorded=0 tagged=1 precision=50%", err)

    def test_reviews_mode_async_without_subagent_transcript_counts_zero(self):
        self.async_fixture()
        (self.proj / SESSION / "subagents" / "agent-a1.jsonl").unlink()
        out, _ = self.run_report("--reviews", "--tsv")
        self.assertEqual(out[1].split("\t")[4], "0")

    def test_reviews_mode_prefers_subagent_handback_over_interim_text(self):
        jl(self.proj / f"{SESSION}.jsonl", [
            dispatch("r1", "2026-10-20T10:00:00Z", "code-reviewer", "Review handback"),
            async_result("r1", "2026-10-20T10:00:02Z", "a1"),
        ])
        lines = sub_lines("claude-fable-5-1", "2026-10-20T10:00:05Z", "2026-10-20T10:04:05Z")
        handback = {"type": "assistant", "timestamp": "2026-10-20T10:04:00Z", "message": {"content": [
            {"type": "tool_use", "id": "h1", "name": "SubagentHandback",
             "input": {"message": "Final report\nI1 a.py:1 bug\nI2 b.py:2 bug"}}]}}
        delivered = {"type": "user", "timestamp": "2026-10-20T10:04:01Z", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "h1", "content": '{"success":true,"message":"Report delivered to your caller."}'}]}}
        # Interim text before the handback, and a closing line after it: the handback message is the report.
        jl(self.proj / SESSION / "subagents" / "agent-a1.jsonl",
           [lines[0], say("2026-10-20T10:03:00Z", "One more check before I write up: I9"), handback, delivered,
            say("2026-10-20T10:04:05Z", "Done.")])
        dest = self.home / "reports"
        out, _ = self.run_report("--reviews", "--tsv", "--reports", str(dest))
        self.assertEqual(out[1].split("\t")[4], "2")
        saved = (dest / out[1].split("\t")[-1]).read_text(encoding="utf-8")
        self.assertIn("Final report", saved)
        self.assertNotIn("One more check", saved)

    def handback_review(self, report):
        jl(self.proj / f"{SESSION}.jsonl", [
            dispatch("r1", "2026-10-20T10:00:00Z", "code-reviewer", "Review counts"),
            async_result("r1", "2026-10-20T10:00:02Z", "a1"),
            say("2026-10-20T10:06:00Z", "Dispositions: I1 fixed; I2 rejected - nope"),
        ])
        lines = sub_lines("claude-fable-5-1", "2026-10-20T10:00:05Z", "2026-10-20T10:04:05Z")
        handback = {"type": "assistant", "timestamp": "2026-10-20T10:04:00Z", "message": {"content": [
            {"type": "tool_use", "id": "h1", "name": "SubagentHandback", "input": {"message": report}}]}}
        jl(self.proj / SESSION / "subagents" / "agent-a1.jsonl", [lines[0], handback, lines[1]])

    def test_reviews_mode_reads_important_from_counts_line(self):
        findings = "\n\n**Important 1 — a.py:1 bug**\n**Important 2 — b.py:2 bug**\nNit 1 — c"
        cases = [
            ("**Counts: Important 2 · Nit 3 · Pre-existing 0.**" + findings, "2"),
            ("Counts: Important 0 / Nit 3 / Pre-existing 0", "0"),
            ("— Important: 0, Nit: 3, Pre-existing: 0.", "0"),
            ("**統計：Important 1 · Nit 3 · Pre-existing 0**", "1"),
            ("**計數:Important 0 · Nit 3 · Pre-existing 2**", "0"),
            ("Counts: Important 2 · Nit 0\nsee also I7 in the notes", "2"),  # the counts line wins over I<n> ids
        ]
        for report, expected in cases:
            with self.subTest(report=report):
                self.handback_review(report)
                out, _ = self.run_report("--reviews", "--tsv")
                cells = out[1].split("\t")
                self.assertEqual((cells[4], cells[11]), (expected, "yes"))

    def test_reviews_mode_counts_line_dispositions_are_recorded(self):
        self.handback_review("Counts: Important 2 · Nit 0 · Pre-existing 0")
        out, err = self.run_report("--reviews", "--tsv")
        self.assertEqual(out[1].split("\t")[4:10], ["2", "1", "1", "0", "0", "0"])
        self.assertIn("tagged=1 precision=50%", err)

    def test_reviews_mode_untagged_report_is_not_zero_findings(self):
        self.handback_review("Verdict: PASS. Nothing blocking.")
        out, err = self.run_report("--reviews", "--tsv")
        cells = out[1].split("\t")
        self.assertEqual((cells[4], cells[11]), ("0", "no"))
        self.assertIn("reviews=1 important=0", err)
        self.assertIn("tagged=0 precision=n/a", err)

    def test_reports_option_writes_redacted_reports(self):
        # r1: synchronous, planted token in the tool_result; r2: background, report only in the subagent transcript.
        important = "Report: 1 Important\nI1 a.py:1 bug\ntoken ghp_abcdefghijklmnop1234"
        jl(self.proj / f"{SESSION}.jsonl", [
            dispatch("r1", "2026-10-20T10:00:00Z", "code-reviewer", "Review one"),
            result("r1", "2026-10-20T10:05:00Z", "a1", important),
            dispatch("r2", "2026-10-20T11:00:00Z", "code-reviewer", "Review two"),
            async_result("r2", "2026-10-20T11:00:02Z", "a2"),
        ])
        self.agent("a1", "claude-fable-5-1", "2026-10-20T10:00:05Z", "2026-10-20T10:04:05Z")
        a2 = sub_lines("claude-opus-5-5", "2026-10-20T11:00:05Z", "2026-10-20T11:04:05Z")
        jl(self.proj / SESSION / "subagents" / "agent-a2.jsonl", [a2[0], say("2026-10-20T11:04:05Z", "Async report\nI1 x\nI2 y"), a2[1]])
        dest = self.home / "out" / "reports"
        out, err = self.run_report("--reviews", "--tsv", "--reports", str(dest))
        names = sorted(f.name for f in dest.iterdir())
        self.assertEqual(names, ["20261020-1000-abcdef12-a1.md", "20261020-1100-abcdef12-a2.md"])
        self.assertEqual([l.split("\t")[-1] for l in out[1:]], names)
        self.assertEqual([l.split("\t")[4] for l in out[1:]], ["1", "2"])
        first = (dest / names[0]).read_text(encoding="utf-8")
        self.assertIn("I1 a.py:1 bug", first)
        self.assertIn("token [REDACTED]", first)
        self.assertNotIn("ghp_", first)
        self.assertIn("Async report", (dest / names[1]).read_text(encoding="utf-8"))
        # A second run does not overwrite existing files: it warns and leaves the column empty.
        (dest / names[0]).write_text("mine", encoding="utf-8")
        out, err = self.run_report("--reviews", "--tsv", "--reports", str(dest))
        self.assertEqual((dest / names[0]).read_text(encoding="utf-8"), "mine")
        self.assertIn(names[0], err)
        self.assertEqual([l.split("\t")[-1] for l in out[1:]], ["", ""])


if __name__ == "__main__":
    unittest.main()
