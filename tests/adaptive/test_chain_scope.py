"""Chain validation per work chain: one broken chain is reported, the session's other works stay served (#26)."""
import json
import os
import subprocess
from _support import BASH, FixtureCase, REPO
from provost_runtime import store
from provost_runtime.hooks import bind, handle_event
from provost_runtime.store import _seal, digest, session_dir

RUNTIME = REPO / "hooks" / "runtime"


class ChainScopeTests(FixtureCase):
    def setUp(self):
        super().setUp()
        bind(self.root, "s1", {})

    def runtime(self, *args, data=None):
        """(exit code, parsed JSON output) of `bash hooks/runtime <args>` for session s1."""
        env = dict(os.environ, PROVOST_CONFIG=str(self.config_path), PYTHONDONTWRITEBYTECODE="1")
        env.pop("PROVOST_PYTHON", None)
        result = subprocess.run([BASH, str(RUNTIME), *args, "--workspace", str(self.root), "--session", "s1"],
                                input=json.dumps(data) if data is not None else "", capture_output=True,
                                text=True, encoding="utf-8", env=env, timeout=60)
        return result.returncode, json.loads(result.stdout or "null")

    def record(self, work, step, expected, request_id):
        code, data = self.runtime("record", "--expected", expected or "null", "--request-id", request_id,
                                  data=dict(self.payload(step), work_id=work))
        return data if code == 0 else data["error"]

    def path(self, card):
        return session_dir(self.root, "s1") / "records" / (card["revision"] + ".json")

    def two_works(self):
        """A (two cards) written before B; returns (a1, a2, b1)."""
        a1 = self.record("A", "a1", None, "a1")
        a2 = self.record("A", "a2", a1["revision"], "a2")
        b1 = self.record("B", "b1", None, "b1")
        return a1, a2, b1

    def injected(self):
        output = handle_event({"session_id": "s1", "cwd": str(self.root), "hook_event_name": "SessionStart",
                               "source": "compact"}, self.config_path)
        return output.get("hookSpecificOutput", {}).get("additionalContext", ""), output.get("systemMessage", "")

    def assert_served_without(self, work, error):
        """status serves B and reports `work` broken with `error`; `work` reads as unrecorded and refuses writes."""
        code, status = self.runtime("status")
        self.assertEqual(code, 0, status)
        self.assertEqual(status["checkpoint"]["payload"]["work_id"], "B")
        self.assertEqual(status["broken_works"], [{"work_id": work, "error": error}])
        code, status = self.runtime("status", "--work", work)
        self.assertEqual((code, status["checkpoint"], status["expected"]), (0, None, None), status)
        self.assertEqual(self.record(work, "again", None, "retry-" + work), error)
        text, message = self.injected()
        self.assertNotIn("unavailable", message)
        self.assertIn('"next": "b1"', text)

    def test_a_missing_card_breaks_only_its_own_chain(self):
        a1, _, _ = self.two_works()
        self.path(a1).unlink()
        self.assert_served_without("A", "INCOMPLETE_CHAIN")

    def test_a_corrupt_head_breaks_its_work_instead_of_serving_the_older_card(self):
        _, a2, _ = self.two_works()
        path = self.path(a2)
        path.write_text(path.read_text(encoding="utf-8").replace('"a2"', '"edited"'), encoding="utf-8")
        self.assert_served_without("A", "CORRUPT_RECORD")

    def test_a_fork_breaks_both_works_of_the_fork_and_serves_the_rest(self):
        a1, _, b1 = self.two_works()
        self.path(b1).unlink()
        self.record("C", "c1", None, "c1")
        body = {k: v for k, v in a1.items() if k != "checksum"} | {
            "revision": digest("s1\0record\0fork"), "parent_record": a1["revision"],
            "payload": dict(self.payload("fork"), work_id="F")}
        (session_dir(self.root, "s1") / "records" / (body["revision"] + ".json")).write_text(
            json.dumps(_seal(body)), encoding="utf-8")
        code, status = self.runtime("status")
        self.assertEqual((code, status["checkpoint"]["payload"]["work_id"]), (0, "C"), status)
        self.assertEqual(status["broken_works"], [{"work_id": "A", "error": "AMBIGUOUS_CHAIN"},
                                                  {"work_id": "F", "error": "AMBIGUOUS_CHAIN"}])

    def test_healthy_works_are_written_beside_a_broken_chain(self):
        a1, _, b1 = self.two_works()
        self.path(a1).unlink()
        b2 = self.record("B", "b2", b1["revision"], "b2")
        c1 = self.record("C", "c1", None, "c1")
        self.assertEqual((b2["payload"]["next"], c1["payload"]["next"]), ("b2", "c1"))
        code, status = self.runtime("status")
        self.assertEqual((code, status["checkpoint"]["revision"]), (0, c1["revision"]), status)
        self.assertEqual(status["broken_works"], [{"work_id": "A", "error": "INCOMPLETE_CHAIN"}])

    def test_a_session_with_only_broken_chains_still_fails_closed(self):
        a1 = self.record("A", "a1", None, "a1")
        self.record("A", "a2", a1["revision"], "a2")
        self.path(a1).unlink()
        self.assertEqual(self.runtime("status"), (1, {"error": "INCOMPLETE_CHAIN"}))
        text, message = self.injected()
        self.assertIn("INCOMPLETE_CHAIN", text + message)
        c1 = self.record("C", "c1", None, "c1")  # a new work still starts its own chain
        code, status = self.runtime("status")
        self.assertEqual((code, status["checkpoint"]["revision"]), (0, c1["revision"]), status)
        self.assertEqual(status["broken_works"], [{"work_id": "A", "error": "INCOMPLETE_CHAIN"}])

    def test_an_unattributable_corrupt_file_fails_the_whole_session(self):
        self.two_works()
        (session_dir(self.root, "s1") / "records" / ("c" * 64 + ".json")).write_text("not json", encoding="utf-8")
        self.assertEqual(self.runtime("status"), (1, {"error": "CORRUPT_RECORD"}))

    def test_a_single_healthy_session_has_no_broken_works_field(self):
        self.two_works()
        code, status = self.runtime("status")
        self.assertEqual(code, 0, status)
        self.assertNotIn("broken_works", status)

    def test_work_states_skips_only_the_broken_chain_and_lists_it(self):
        a1, _, _ = self.two_works()
        self.path(a1).unlink()
        states = store.work_states(self.root)
        self.assertEqual((sorted(states["works"]), states["skipped"]), (["B"], []))
        self.assertEqual(states["broken"], [{"session": "s1", "work_id": "A", "error": "INCOMPLETE_CHAIN"}])
