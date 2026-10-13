import contextlib
import io
import json
import os
from pathlib import Path
from unittest import mock
from _support import FixtureCase
from provost_runtime import cli
from provost_runtime.store import (_archive_path, _seal, as_workspace, current, record,
                                   session_dir, snapshot)

GITHUB_TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0"


def tree(root):
    """Every directory and file (with bytes) under root."""
    return {p.relative_to(root).as_posix(): (p.read_bytes() if p.is_file() else None) for p in sorted(root.rglob("*"))}


class SearchTests(FixtureCase):
    def setUp(self):
        super().setUp()
        (self.root / "other").mkdir()
        self.ws = as_workspace(self.root)
        self.other = as_workspace(self.root / "other")
        self.assertNotEqual(self.ws.repo_key[:16], self.other.repo_key[:16])

    def payload_of(self, work_id="w1", phase="build", decisions=(), blockers=(), nxt="n", evidence=()):
        return {"work_id": work_id, "phase": phase, "sources": [], "assignments": [], "decisions": list(decisions),
                "blockers": list(blockers),
                "evidence": [{"claim": claim, "ref": "r", "state": "pass-observed"} for claim in evidence],
                "next": nxt, "suggested_skills": []}

    def retime(self, path, at):
        data = json.loads(path.read_text(encoding="utf-8"))
        data.pop("checksum")
        data["recorded_at"] = at
        if "captured_at" in data:
            data["captured_at"] = at
        path.write_text(json.dumps(_seal(data)), encoding="utf-8")

    def rec(self, session, at=None, ws=None, **fields):
        """Record a schema 1 payload (decisions and evidence in the card) on top of the session's
        current record; at back-dates it."""
        ws = ws or self.ws
        previous = current(ws, session)
        data = self.record_v1(ws, session, self.payload_of(**fields), previous["revision"] if previous else None,
                              "req-%d" % (previous is not None) + session)
        if at:
            self.retime(session_dir(ws, session) / "records" / (data["revision"] + ".json"), at)
        return data

    def snap(self, session, key, at=None, ws=None):
        ws = ws or self.ws
        data = snapshot(ws, session, key)
        if at:
            self.retime(session_dir(ws, session) / "snapshots" / (data["snapshot_id"] + ".json"), at)
        return data

    def archive(self, work_id, ws=None, **fields):
        """A record file in the repo archive, written as prune --apply stores it."""
        ws = ws or self.ws
        data = self.rec("archived-" + work_id, ws=ws, work_id=work_id, **fields)
        archive = session_dir(ws, "archived-" + work_id).parents[1] / "archive"
        archive.mkdir(exist_ok=True)
        path = archive / _archive_path(archive, work_id).name
        path.write_text(json.dumps(data), encoding="utf-8")
        # The archived session itself is gone, as after an expiry prune.
        import shutil
        shutil.rmtree(session_dir(ws, "archived-" + work_id))
        return path

    def search(self, *args):
        out = io.StringIO()
        with mock.patch.dict(os.environ, PROVOST_CONFIG=str(self.config_path)), contextlib.redirect_stdout(out):
            code = cli.main(["search", "--workspace", str(self.root), *args])
        return code, json.loads(out.getvalue())

    def found(self, *args):
        code, data = self.search(*args)
        self.assertEqual(code, 0, data)
        return data

    def texts(self, data):
        return [r["text"] for r in data["results"]]

    def test_schema_2_cards_are_searched_for_blockers_and_next(self):
        record(self.ws, "s2", {"work_id": "w2", "phase": "build", "sources": [], "assignments": [],
                               "blockers": ["lambda blocker"], "next": "lambda next", "suggested_skills": [],
                               "new_decisions": [{"text": "lambda logged decision"}]}, None, "r1")
        self.assertEqual(sorted((r["field"], r["text"]) for r in self.found("--query", "lambda")["results"]),
                         [("blocker", "lambda blocker"), ("next", "lambda next")])

    def test_matches_decisions_blockers_next_and_evidence_claims(self):
        self.rec("s1", decisions=["use sqlite for the queue"], blockers=["sqlite lock contention"],
                 nxt="benchmark sqlite", evidence=["sqlite bench passes"])
        data = self.found("--query", "sqlite")
        self.assertEqual({r["field"] for r in data["results"]}, {"decision", "blocker", "next", "evidence"})
        self.assertEqual(data["skipped"], 0)
        self.assertEqual(data["truncated"], 0)
        self.assertEqual(data["repos_scanned"], 1)
        self.assertEqual(data["query"], "sqlite")
        first = data["results"][0]
        self.assertEqual({k: first[k] for k in ("work_id", "phase", "source", "worktree_path")},
                         {"work_id": "w1", "phase": "build", "source": "record", "worktree_path": str(self.ws.root)})
        self.assertEqual(set(first), {"recorded_at", "worktree_path", "work_id", "phase", "field", "text", "source",
                                      "ref"})

    def test_every_term_must_appear_case_insensitively_incl_work_id_and_phase(self):
        self.rec("s1", work_id="Auth-Rewrite", phase="Review", decisions=["Keep Tokens short", "keep cookies"])
        self.assertEqual(self.texts(self.found("--query", "keep TOKENS")), ["Keep Tokens short"])
        self.assertEqual(self.texts(self.found("--query", "keep cookies tokens")), [])
        both = self.found("--query", "auth review cookies")
        self.assertEqual(self.texts(both), ["keep cookies"])

    def test_only_the_newest_record_of_a_session_counts_but_snapshots_remain(self):
        self.rec("s1", decisions=["adopt zebra cache"])
        self.snap("s1", "e1")
        self.rec("s1", decisions=["drop it"])
        self.archive("old-work", decisions=["zebra archived decision"])
        data = self.found("--query", "zebra")
        self.assertEqual(sorted((r["source"], r["text"]) for r in data["results"]),
                         [("archive", "zebra archived decision"), ("snapshot", "adopt zebra cache")])
        self.assertEqual(self.texts(self.found("--query", "drop")), ["drop it"])

    def test_identical_work_field_and_text_keeps_the_newest(self):
        self.rec("s1", at="2026-01-01T00:00:00+00:00", decisions=["ship gamma now"])
        self.rec("s2", at="2026-02-01T00:00:00+00:00", decisions=["ship gamma now"])
        self.rec("s3", at="2026-03-01T00:00:00+00:00", work_id="other-work", decisions=["ship gamma now"])
        data = self.found("--query", "gamma")
        self.assertEqual([(r["work_id"], r["recorded_at"][:7]) for r in data["results"]],
                         [("other-work", "2026-03"), ("w1", "2026-02")])

    def test_ranks_by_occurrences_then_recency_and_limits(self):
        self.rec("s1", at="2026-01-01T00:00:00+00:00", work_id="a", decisions=["cache once"])
        self.rec("s2", at="2026-02-01T00:00:00+00:00", work_id="b", decisions=["cache cache cache"])
        self.rec("s3", at="2026-03-01T00:00:00+00:00", work_id="c", decisions=["cache twice cache"])
        self.rec("s4", at="2026-04-01T00:00:00+00:00", work_id="d", decisions=["cache last"])
        data = self.found("--query", "cache")
        self.assertEqual(self.texts(data), ["cache cache cache", "cache twice cache", "cache last", "cache once"])
        limited = self.found("--query", "cache", "--limit", "3")
        self.assertEqual((len(limited["results"]), limited["truncated"]), (3, 1))

    def test_default_limit_is_ten_and_over_fifty_is_refused(self):
        self.rec("s1", decisions=["topic %02d" % i for i in range(12)])
        data = self.found("--query", "topic")
        self.assertEqual((len(data["results"]), data["truncated"]), (10, 2))
        for bad in ("0", "51", "x"):
            self.assertEqual(self.search("--query", "topic", "--limit", bad), (1, {"error": "INVALID_LIMIT"}))
        self.assertEqual(self.found("--query", "topic", "--limit", "50")["truncated"], 0)

    def test_text_is_clipped_to_160_characters(self):
        self.rec("s1", decisions=["clipme " + "x" * 400])
        self.assertEqual(len(self.found("--query", "clipme")["results"][0]["text"]), 160)

    def test_since_filters_by_recorded_at(self):
        self.rec("s1", at="2026-01-15T00:00:00+00:00", work_id="old", decisions=["delta choice"])
        self.rec("s2", at="2026-03-15T00:00:00+00:00", work_id="new", decisions=["delta choice two"])
        data = self.found("--query", "delta", "--since", "2026-02-01")
        self.assertEqual([r["work_id"] for r in data["results"]], ["new"])
        self.assertEqual(self.search("--query", "delta", "--since", "last week"), (1, {"error": "INVALID_SINCE"}))

    def test_scope_is_the_workspace_repo_unless_all_repos(self):
        self.rec("s1", decisions=["epsilon here"])
        self.rec("s9", ws=self.other, work_id="elsewhere", decisions=["epsilon there"])
        mine = self.found("--query", "epsilon")
        self.assertEqual((self.texts(mine), mine["repos_scanned"]), (["epsilon here"], 1))
        everywhere = self.found("--query", "epsilon", "--all-repos")
        self.assertEqual((sorted(self.texts(everywhere)), everywhere["repos_scanned"]),
                         (["epsilon here", "epsilon there"], 2))

    def test_unreadable_and_tampered_files_are_skipped_and_counted(self):
        good = self.rec("s1", decisions=["omega survives"])
        directory = session_dir(self.ws, "s2")
        self.rec("s2", decisions=["omega tampered"])
        for path in (directory / "records").glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            data["payload"]["decisions"] = ["omega forged"]
            path.write_text(json.dumps(data), encoding="utf-8")
        directory = session_dir(self.ws, "s3")
        self.rec("s3", decisions=["omega broken"])
        for path in (directory / "records").glob("*.json"):
            path.write_text("{not json", encoding="utf-8")
        bad_archive = self.archive("w-bad", decisions=["omega archive"])
        bad_archive.write_text("[]", encoding="utf-8")
        data = self.found("--query", "omega")
        self.assertEqual(self.texts(data), ["omega survives"])
        self.assertEqual(data["skipped"], 3)
        self.assertEqual(good["revision"], current(self.ws, "s1")["revision"])

    def test_secret_like_strings_are_redacted_everywhere(self):
        key = "-----BEGIN RSA PRIVATE KEY-----"
        secrets = [GITHUB_TOKEN, "github_pat_" + "B2c3D4e5F6g7H8", "sk-" + "a1B2c3D4e5F6g7H8i9", "AKIA" + "ABCDEFGHIJKLMNOP",
                   key]
        self.rec("s1", decisions=["rotate " + secret for secret in secrets], nxt="rotate " + GITHUB_TOKEN)
        data = self.found("--query", "rotate " + GITHUB_TOKEN)
        self.assertEqual(data["query"], "rotate [REDACTED]")
        for result in data["results"]:
            self.assertEqual(result["text"], "rotate [REDACTED]")
        code, shown = self.search("--show", data["results"][0]["ref"])
        self.assertEqual(code, 0)
        self.assertNotIn(GITHUB_TOKEN, json.dumps(shown))
        everything = self.found("--query", "rotate")
        self.assertEqual(len(everything["results"]), 6)
        self.assertTrue(all(r["text"] == "rotate [REDACTED]" for r in everything["results"]))

    def test_sk_prefix_inside_a_word_is_not_redacted(self):
        from provost_runtime.search import redact
        self.assertEqual(redact("task-force-planning-2026 and disk-cleanup_routine_x"),
                         "task-force-planning-2026 and disk-cleanup_routine_x")
        key = "sk-" + "a1B2c3D4e5F6g7H8i9"
        self.assertEqual(redact("use " + key + ", (" + key + ")"), "use [REDACTED], ([REDACTED])")

    def test_show_resolves_a_ref_to_the_full_payload(self):
        self.rec("s1", decisions=["theta first"])
        self.snap("s1", "e1")
        self.rec("s1", decisions=["theta second"], blockers=["b1"], nxt="go")
        self.archive("w-arch", decisions=["theta archived"])
        results = self.found("--query", "theta")["results"]
        self.assertEqual(sorted(r["source"] for r in results), ["archive", "record", "snapshot"])
        for result in results:
            code, shown = self.search("--show", result["ref"])
            self.assertEqual(code, 0, shown)
            self.assertEqual((shown["ref"], shown["source"], shown["recorded_at"], shown["worktree_path"]),
                             (result["ref"], result["source"], result["recorded_at"], result["worktree_path"]))
            self.assertIn(result["text"], shown["payload"]["decisions"])
        record_ref = next(r["ref"] for r in results if r["source"] == "record")
        shown = self.search("--show", record_ref)[1]
        self.assertEqual(shown["payload"]["blockers"], ["b1"])
        self.assertEqual(shown["payload"]["next"], "go")

    def test_show_errors_and_scope(self):
        self.rec("s9", ws=self.other, decisions=["iota elsewhere"])
        ref = self.found("--query", "iota", "--all-repos")["results"][0]["ref"]
        self.assertEqual(self.search("--show", ref), (1, {"error": "REF_NOT_FOUND"}))
        self.assertEqual(self.search("--show", ref, "--all-repos")[0], 0)
        self.assertEqual(self.search("--show", "../../etc"), (1, {"error": "INVALID_REF"}))
        self.assertEqual(self.search("--show", ref.replace("r:", "r:0", 1)), (1, {"error": "INVALID_REF"}))
        self.assertEqual(self.search("--show", "r:" + "0" * 16 + ":" + "0" * 12 + ":" + "0" * 12),
                         (1, {"error": "REF_NOT_FOUND"}))

    def test_query_is_required_without_show(self):
        self.assertEqual(self.search(), (1, {"error": "QUERY_REQUIRED"}))
        self.assertEqual(self.search("--query", "   "), (1, {"error": "QUERY_REQUIRED"}))

    def test_search_on_an_empty_store_finds_nothing_and_creates_nothing(self):
        data = self.found("--query", "anything", "--all-repos")
        self.assertEqual((data["results"], data["repos_scanned"], data["skipped"]), ([], 0, 0))
        self.assertEqual(list(self.store_home.iterdir()), [])

    def test_search_and_show_leave_the_store_byte_identical(self):
        self.rec("s1", decisions=["kappa one"])
        self.snap("s1", "e1")
        self.archive("w-arch", decisions=["kappa archived"])
        (self.store_home / "v2" / "repos" / self.ws.repo_key[:16] / "archive" / ".pending-x").write_text("p")
        before = tree(self.store_home)
        results = self.found("--query", "kappa", "--all-repos")["results"]
        self.search("--show", results[0]["ref"], "--all-repos")
        self.search("--query", "kappa")
        self.assertEqual(tree(self.store_home), before)
        self.assertFalse((self.root / ".provost").exists())
