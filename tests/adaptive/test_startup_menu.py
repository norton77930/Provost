"""SessionStart startup/clear/fork menu of joinable works (#24), driven through the hook entry point."""
from datetime import datetime, timedelta, timezone
from unittest import mock
from _support import FixtureCase
from provost_runtime import store
from provost_runtime.config import handoff_home
from provost_runtime.hooks import handle_event

SOURCES = ("startup", "clear", "fork")
TOKEN = "ghp_" + "A1b2C3d4E5" * 3


class StartupMenuTests(FixtureCase):
    def start(self, source="startup", session="fresh"):
        output = handle_event({"session_id": session, "cwd": str(self.root), "hook_event_name": "SessionStart",
                               "source": source}, self.config_path)
        self.assertNotIn("unavailable", output.get("systemMessage", ""))
        text = output.get("hookSpecificOutput", {}).get("additionalContext", "")
        self.assertLessEqual(len(text), 8000)
        return text

    def write(self, work, session, minutes_ago, phase="implementation", next_step="next step"):
        """Record a card of work in session, then date it minutes_ago (resealed)."""
        previous = store.current(self.root, session, work)
        store.record(self.root, session, dict(self.payload(next_step), work_id=work, phase=phase),
                     previous["revision"] if previous else None, "r-%s-%s" % (session[:8], work[:20]))
        head = store.current(self.root, session, work)
        data = {k: v for k, v in head.items() if k != "checksum"}
        data["recorded_at"] = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
        path = store.session_dir(self.root, session) / "records" / (head["revision"] + ".json")
        store.atomic_write(handoff_home(), path, store._seal(data))

    def menu_lines(self, text):
        return [line for line in text.splitlines() if line.startswith("- ") and "session " in line]

    def test_lists_open_works_newest_first_at_most_five_without_done_ones(self):
        for index in range(7):
            self.write("work-%d" % index, "abcdef0%d-aaaa-4bbb-8ccc-000000000000" % index, 100 - index * 10)
        self.write("work-closed", "abcdef99-aaaa-4bbb-8ccc-000000000000", 1, phase="done")
        text = self.start()
        lines = self.menu_lines(text)
        self.assertEqual([line.split()[1] for line in lines], ["work-6", "work-5", "work-4", "work-3", "work-2"])
        self.assertNotIn("work-closed", text)
        self.assertNotIn("work-1", text)
        self.assertIn("implementation", lines[0])
        self.assertIn("abcdef06", lines[0])
        self.assertNotIn("abcdef06-aaaa", text)  # short session id only
        self.assertRegex(lines[0], r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC")
        self.assertIn("/handoff resume <snapshot-id>", text)
        self.assertIn("--join", text)
        self.assertIn("New work context", text)

    def test_hint_says_to_list_snapshots_before_resuming(self):
        self.write("work-a", "abcdef01-aaaa-4bbb-8ccc-000000000000", 5)
        hint = next(line for line in self.start().splitlines() if "/handoff resume" in line)
        self.assertRegex(hint, r"\blist\b.*snapshot id.*/handoff resume <snapshot-id>")

    def test_the_newest_card_of_a_work_names_the_session_that_wrote_it(self):
        self.write("shared", "aaaaaaaa-aaaa-4bbb-8ccc-000000000000", 50)
        self.write("shared", "bbbbbbbb-aaaa-4bbb-8ccc-000000000000", 5)
        lines = self.menu_lines(self.start())
        self.assertEqual(len(lines), 1)
        self.assertIn("bbbbbbbb", lines[0])
        self.assertNotIn("aaaaaaaa", lines[0])

    def test_no_card_content_is_injected(self):
        self.write("work-a", "abcdef01-aaaa-4bbb-8ccc-000000000000", 5, next_step="PRIVATE NEXT STEP")
        self.assertNotIn("PRIVATE NEXT STEP", self.start())

    def test_work_id_and_phase_are_redacted_and_clipped(self):
        self.write(TOKEN + "-" + "w" * 200, "abcdef01-aaaa-4bbb-8ccc-000000000000", 5, phase="phase-" + TOKEN)
        text = self.start()
        self.assertNotIn(TOKEN, text)
        self.assertIn("[REDACTED]", text)
        self.assertNotIn("w" * 100, text)

    def test_a_failing_menu_keeps_the_notice_only(self):
        self.write("work-a", "abcdef01-aaaa-4bbb-8ccc-000000000000", 5)
        for error in (store.StoreError("CORRUPT_RECORD"), ValueError("boom")):
            with mock.patch("provost_runtime.hooks.work_states", side_effect=error):
                text = self.start()
            self.assertIn("New work context", text)
            self.assertNotIn("work-a", text)
            self.assertNotIn("/handoff resume", text)

    def test_clear_and_fork_list_the_same_menu_and_compact_and_resume_do_not(self):
        self.write("work-a", "abcdef01-aaaa-4bbb-8ccc-000000000000", 5)
        menus = {source: self.menu_lines(self.start(source)) for source in SOURCES}
        self.assertEqual(len(menus["startup"]), 1)
        self.assertEqual(menus["clear"], menus["startup"])
        self.assertEqual(menus["fork"], menus["startup"])
        for source in ("compact", "resume"):
            self.assertEqual(self.menu_lines(self.start(source)), [])

    def test_no_open_works_adds_nothing(self):
        self.write("work-closed", "abcdef01-aaaa-4bbb-8ccc-000000000000", 5, phase="done")
        text = self.start()
        self.assertIn("New work context", text)
        self.assertNotIn("/handoff resume", text)
