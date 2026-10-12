from pathlib import Path
from unittest import mock
from _support import FixtureCase
from provost_runtime.notifications import notice


class NotificationTests(FixtureCase):
    def test_first_repeat_recovery_once(self):
        self.assertEqual(notice("s", "WRITE_FAILED", self.root)["level"], "first")
        self.assertEqual(notice("s", "WRITE_FAILED", self.root)["level"], "repeat")
        self.assertEqual(notice("s", None, self.root)["level"], "recovered")
        self.assertEqual(notice("s", None, self.root)["level"], "none")

    def test_sessions_and_error_causes_are_independent(self):
        notice("s1", "WRITE_FAILED", self.root)
        self.assertEqual(notice("s2", "WRITE_FAILED", self.root)["level"], "first")
        self.assertEqual(notice("s1", "STORE_BUSY", self.root)["level"], "first")

    def test_cache_failure_is_honest_and_nonfatal(self):
        with mock.patch("pathlib.Path.mkdir", side_effect=OSError("fixture")):
            result = notice("s", "WRITE_FAILED", self.root)
        self.assertEqual(result["level"], "untracked")
        self.assertIn("deduplication unavailable", result["message"])

    def test_corrupt_cache_does_not_suppress_failure(self):
        notice("s", "WRITE_FAILED", self.root)
        for path in self.root.glob("*.json"):
            if path != self.config_path:
                path.write_text("{bad")
        self.assertEqual(notice("s", "WRITE_FAILED", self.root)["level"], "first")

    def test_partial_codes_dedupe_and_recover_like_errors(self):
        for code, text in (("partial-missing", "Provost: facts saved; decision record missing."),
                           ("partial-stale", "Provost: facts saved; decision record is older than the latest change.")):
            first = notice("p-" + code, code, self.root)
            self.assertEqual((first["level"], first["message"]), ("first", text))
            repeat = notice("p-" + code, code, self.root)
            self.assertEqual((repeat["level"], repeat["message"]), ("repeat", text))
            self.assertEqual(notice("p-" + code, None, self.root)["message"], "Provost: handoff saving recovered.")

    def test_facts_failure_with_saved_record_has_its_own_wording(self):
        result = notice("s", "FACTS_FAILED", self.root, record_saved=True)
        self.assertEqual(result["message"], "Provost: decision record saved; facts not saved. FACTS_FAILED")
        self.assertEqual(notice("s", "FACTS_FAILED", self.root, record_saved=True)["level"], "repeat")

    def test_partial_without_cache_keeps_partial_text(self):
        with mock.patch("pathlib.Path.mkdir", side_effect=OSError("fixture")):
            result = notice("s", "partial-missing", self.root)
        self.assertEqual(result["level"], "untracked")
        self.assertIn("decision record missing", result["message"])
        self.assertNotIn("not saved", result["message"])
