"""Search and --show over the decision log, driven through the search CLI in an isolated store."""
import contextlib
import hashlib
import io
import json
import os
import subprocess
from unittest import mock
from _support import BASH, FixtureCase, REPO
from provost_runtime import cli
from provost_runtime.store import as_workspace, record

GITHUB_TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0"


def tree(root):
    return {p.relative_to(root).as_posix(): (p.read_bytes() if p.is_file() else None) for p in sorted(root.rglob("*"))}


class SearchLogTests(FixtureCase):
    def setUp(self):
        super().setUp()
        (self.root / "other").mkdir()
        self.ws = as_workspace(self.root)
        self.other = as_workspace(self.root / "other")
        self.heads = {}
        self.requests = 0

    def write(self, work, decisions=(), evidence=(), session="s1", ws=None, **card):
        """Record a state card for work with new log entries; returns the record result."""
        ws = ws or self.ws
        payload = dict(self.payload(card.pop("next", "go")), work_id=work, **card)
        payload.update(new_decisions=list(decisions), new_evidence=list(evidence))
        key = (ws.repo_key, session, work)
        self.requests += 1
        out = record(ws, session, payload, self.heads.get(key), "req-%d" % self.requests)
        self.heads[key] = out["revision"]
        return out

    def log_path(self, work, ws=None):
        ws = ws or self.ws
        return (self.store_home / "v2" / "repos" / ws.repo_key[:16] / "logs"
                / (hashlib.sha256(work.encode()).hexdigest() + ".jsonl"))

    def retime(self, work, times, ws=None):
        """Rewrite the `at` of the work's entries: {entry id: iso time}."""
        path = self.log_path(work, ws)
        lines = path.read_text(encoding="utf-8").split("\n")
        for i, line in enumerate(lines[1:], 1):
            if line:
                entry = json.loads(line)
                if entry["id"] in times:
                    entry["at"] = times[entry["id"]]
                    lines[i] = json.dumps(entry)
        path.write_text("\n".join(lines), encoding="utf-8")

    def search(self, *args):
        out = io.StringIO()
        with mock.patch.dict(os.environ, PROVOST_CONFIG=str(self.config_path)), contextlib.redirect_stdout(out):
            code = cli.main(["search", "--workspace", str(self.root), *args])
        return code, json.loads(out.getvalue())

    def found(self, *args):
        code, data = self.search(*args)
        self.assertEqual(code, 0, data)
        return data

    def ids(self, data):
        return [(r["work_id"], r["id"]) for r in data["results"]]

    def test_a_decision_from_an_older_narrow_record_is_found_in_the_log(self):
        self.write("hook-perf", decisions=[{"text": "Replace the suffix strip with an anchored regex because "
                                                    "the loop was quadratic"}])
        self.write("hook-perf", next="narrow last record about something else")
        self.write("refactor", decisions=[{"text": "Name the cut key instead of a literal 16"}])
        data = self.found("--query", "suffix")
        self.assertEqual(self.ids(data), [("hook-perf", "D1")])
        hit = data["results"][0]
        self.assertEqual((hit["kind"], hit["field"], hit["source"], hit["ref"], hit["superseded_by"]),
                         ("decision", "decision", "log", "hook-perf:D1", None))
        self.assertRegex(hit["date"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertTrue(hit["recorded_at"].startswith(hit["date"]))
        self.assertIn("anchored regex", hit["text"])
        # Terms may also match the work_id, as for record items.
        self.assertEqual(self.ids(self.found("--query", "refactor cut")), [("refactor", "D1")])

    def test_superseded_decisions_carry_the_replacement_and_evidence_its_chains_latest_state(self):
        first = self.write("w", decisions=[{"text": "cache sessions in memory"}],
                           evidence=[{"claim": "cache bench", "state": "fail", "ref": "b.py"}])
        self.write("w", decisions=[{"text": "cache sessions on disk instead", "supersedes": "D1"}],
                   evidence=[{"claim": "cache bench", "state": "pass-observed", "ref": "b.py", "updates": "E1"}])
        self.assertEqual(first["log_entries"], ["D1", "E1"])
        by_id = {r["id"]: r for r in self.found("--query", "cache")["results"]}
        self.assertEqual(set(by_id), {"D1", "D2", "E1", "E2"})
        self.assertEqual((by_id["D1"]["superseded_by"], by_id["D2"]["superseded_by"]), ("D2", None))
        for ident in ("E1", "E2"):
            self.assertEqual((by_id[ident]["state"], by_id[ident]["latest"]), ("pass-observed", "E2"))
        self.assertNotIn("state", by_id["D1"])

    def test_show_returns_the_chain_in_both_directions_and_two_neighbours_each_side(self):
        self.write("w", decisions=[{"text": "decision one"}, {"text": "decision two"}],
                   evidence=[{"claim": "claim one", "state": "unverified", "ref": "r"}])
        self.write("w", decisions=[{"text": "decision three replaces two", "supersedes": "D2"},
                                   {"text": "decision four"}],
                   evidence=[{"claim": "claim one", "state": "pass-observed", "ref": "r", "updates": "E1"}])
        self.write("w", decisions=[{"text": "decision five replaces three", "supersedes": "D3"}])
        # Time order differs from ids: E1 happened first, D4 last of the second record.
        self.retime("w", {"E1": "2026-01-01T00:00:00+00:00", "D1": "2026-01-02T00:00:00+00:00",
                          "D2": "2026-01-03T00:00:00+00:00", "D3": "2026-01-04T00:00:00+00:00",
                          "D4": "2026-01-05T00:00:00+00:00", "E2": "2026-01-06T00:00:00+00:00",
                          "D5": "2026-01-07T00:00:00+00:00"})
        shown = self.found("--show", "w:D3")
        self.assertEqual((shown["source"], shown["work_id"], shown["ref"]), ("log", "w", "w:D3"))
        self.assertEqual(shown["entry"]["id"], "D3")
        self.assertEqual(shown["entry"]["text"], "decision three replaces two")
        self.assertEqual([e["id"] for e in shown["chain"]], ["D2", "D3", "D5"])
        self.assertEqual(shown["superseded_by"], "D5")
        self.assertEqual([e["id"] for e in shown["before"]], ["D1", "D2"])
        self.assertEqual([e["id"] for e in shown["after"]], ["D4", "E2"])
        self.assertEqual(shown["dangling"], [])
        evidence = self.found("--show", "w:E1")
        self.assertEqual([e["id"] for e in evidence["chain"]], ["E1", "E2"])
        self.assertEqual((evidence["latest"], evidence["state"]), ("E2", "pass-observed"))
        self.assertEqual([e["id"] for e in evidence["before"]], [])
        self.assertEqual([e["id"] for e in evidence["after"]], ["D1", "D2"])
        edge = self.found("--show", "w:D5")
        self.assertEqual([e["id"] for e in edge["before"]], ["D4", "E2"])
        self.assertEqual(edge["after"], [])

    def test_show_ref_syntax_errors_and_scope(self):
        self.write("a:b", decisions=[{"text": "colon work id"}])
        self.assertEqual(self.found("--show", "a:b:D1")["work_id"], "a:b")
        for ref, error in (("a:b:D2", "REF_NOT_FOUND"), ("missing:D1", "REF_NOT_FOUND"), ("a:b:X1", "INVALID_REF"),
                           (":D1", "INVALID_REF"), ("D1", "INVALID_REF"), ("a:b:D0", "INVALID_REF")):
            self.assertEqual(self.search("--show", ref), (1, {"error": error}), ref)
        self.write("elsewhere", decisions=[{"text": "iota elsewhere"}], ws=self.other)
        self.assertEqual(self.search("--show", "elsewhere:D1"), (1, {"error": "REF_NOT_FOUND"}))
        result = self.found("--query", "iota", "--all-repos")["results"][0]
        prefix = "@" + self.other.repo_key[:16] + ":"
        self.assertEqual(result["ref"], prefix + "elsewhere:D1")
        self.assertEqual(self.search("--show", result["ref"]), (1, {"error": "REF_NOT_FOUND"}))
        self.assertEqual(self.found("--show", result["ref"], "--all-repos")["entry"]["text"], "iota elsewhere")

    def test_filters_work_kind_and_since(self):
        self.write("alpha", decisions=[{"text": "zeta choice alpha"}],
                   evidence=[{"claim": "zeta check alpha", "state": "fail", "ref": "r"}])
        self.write("beta", decisions=[{"text": "zeta choice beta"}])
        self.retime("alpha", {"D1": "2026-01-10T00:00:00+00:00", "E1": "2026-03-10T00:00:00+00:00"})
        self.retime("beta", {"D1": "2026-02-10T00:00:00+00:00"})
        self.assertEqual(sorted(self.ids(self.found("--query", "zeta"))),
                         [("alpha", "D1"), ("alpha", "E1"), ("beta", "D1")])
        self.assertEqual(self.ids(self.found("--query", "zeta", "--work", "alpha", "--kind", "decision")),
                         [("alpha", "D1")])
        self.assertEqual(self.ids(self.found("--query", "zeta", "--kind", "evidence")), [("alpha", "E1")])
        self.assertEqual(self.ids(self.found("--query", "zeta", "--since", "2026-02-01")),
                         [("alpha", "E1"), ("beta", "D1")])
        self.assertEqual(self.found("--query", "zeta", "--work", "none")["results"], [])
        self.assertEqual(self.search("--query", "zeta", "--kind", "blocker"), (1, {"error": "INVALID_KIND"}))
        self.assertEqual(self.search("--query", "zeta", "--work", ""), (1, {"error": "INVALID_ARGUMENTS"}))

    def test_filters_also_apply_to_older_record_items(self):
        self.record_v1(self.ws, "old", dict(self.payload_v1(decisions=["theta old decision"]), work_id="legacy"),
                       None, "r1")
        self.write("fresh", decisions=[{"text": "theta new decision"}])
        self.assertEqual(sorted(r["work_id"] for r in self.found("--query", "theta")["results"]), ["fresh", "legacy"])
        self.assertEqual([r["work_id"] for r in self.found("--query", "theta", "--work", "legacy")["results"]],
                         ["legacy"])
        self.assertEqual(self.found("--query", "collect", "--work", "legacy", "--kind", "decision")["results"], [])
        self.assertEqual([r["field"] for r in self.found("--query", "collect", "--work", "legacy")["results"]],
                         ["next"])

    def test_unreadable_foreign_and_mismatched_logs_are_skipped_and_counted(self):
        self.write("good", decisions=[{"text": "sigma survives"}])
        self.write("torn", decisions=[{"text": "sigma torn tail kept"}])
        with self.log_path("torn").open("ab") as stream:
            stream.write(b'{"id": "D2", "kind": "dec')  # a crash mid-append: not skipped, not a line
        self.write("broken", decisions=[{"text": "sigma broken"}])
        self.log_path("broken").write_text(self.log_path("broken").read_text(encoding="utf-8") + "not json\n",
                                           encoding="utf-8")
        self.write("alien", decisions=[{"text": "sigma foreign"}], ws=self.other)
        foreign = self.log_path("alien", self.other)
        foreign.replace(self.log_path("alien"))  # a log of another repo under this repo's logs/
        self.write("renamed", decisions=[{"text": "sigma renamed"}])
        self.log_path("renamed").rename(self.log_path("renamed").with_name("f" * 64 + ".jsonl"))
        (self.log_path("good").parent / "notes.txt").write_text("ignored")
        data = self.found("--query", "sigma")
        self.assertEqual(sorted(self.ids(data)), [("good", "D1"), ("torn", "D1")])
        self.assertEqual(data["skipped"], 3)

    def test_dangling_and_cross_kind_targets_are_tolerated(self):
        self.write("w", decisions=[{"text": "kappa base decision"}],
                   evidence=[{"claim": "kappa base claim", "state": "fail", "ref": "r"}])
        path = self.log_path("w")
        extra = [{"id": "D2", "kind": "decision", "at": "2026-02-01T00:00:00+00:00", "session": "s1",
                  "text": "kappa supersedes nothing real", "supersedes": "D9"},
                 {"id": "D3", "kind": "decision", "at": "2026-02-02T00:00:00+00:00", "session": "s1",
                  "text": "kappa supersedes evidence", "supersedes": "E1"},
                 {"id": "E2", "kind": "evidence", "at": "2026-02-03T00:00:00+00:00", "session": "s1",
                  "claim": "kappa updates a decision", "state": "pass-observed", "ref": "r", "updates": "D1"}]
        path.write_text(path.read_text(encoding="utf-8") + "".join(json.dumps(e) + "\n" for e in extra),
                        encoding="utf-8")
        by_id = {r["id"]: r for r in self.found("--query", "kappa")["results"]}
        self.assertEqual(set(by_id), {"D1", "D2", "D3", "E1", "E2"})
        self.assertEqual(by_id["E1"]["superseded_by"], "D3")
        shown = self.found("--show", "w:D2")
        self.assertEqual((shown["entry"]["id"], shown["dangling"]), ("D2", ["D9"]))
        self.assertEqual([e["id"] for e in shown["chain"]], ["D2"])
        cross = self.found("--show", "w:D3")
        self.assertEqual(sorted(e["id"] for e in cross["chain"]), ["D3", "E1"])
        self.assertEqual(cross["dangling"], [])
        self.assertEqual(sorted(e["id"] for e in self.found("--show", "w:D1")["chain"]), ["D1", "E2"])

    def test_a_multi_work_session_contributes_its_newest_card_per_work(self):
        self.write("one", blockers=["rho blocker one"], next="rho next one")
        self.write("two", blockers=["rho blocker two"], next="rho next two")
        self.write("one", blockers=["rho blocker one again"], next="rho next one again")
        data = self.found("--query", "rho", "--limit", "50")
        self.assertEqual(sorted((r["work_id"], r["text"]) for r in data["results"]),
                         [("one", "rho blocker one again"), ("one", "rho next one again"),
                          ("two", "rho blocker two"), ("two", "rho next two")])
        self.assertEqual([r["work_id"] for r in self.found("--query", "rho", "--work", "two")["results"]],
                         ["two", "two"])

    def test_a_log_entry_shadows_the_identical_older_record_item(self):
        self.record_v1(self.ws, "old", dict(self.payload_v1(decisions=["upsilon migrated text"]), work_id="w"),
                       None, "r1")
        self.write("w", decisions=[{"text": "upsilon migrated text"}], session="new")
        data = self.found("--query", "upsilon")
        self.assertEqual([(r["source"], r.get("id")) for r in data["results"]], [("log", "D1")])

    def test_output_is_redacted_and_the_store_is_left_untouched(self):
        self.write("w", decisions=[{"text": "rotate " + GITHUB_TOKEN}],
                   evidence=[{"claim": "rotate " + GITHUB_TOKEN, "state": "fail", "ref": "r"}])
        before = tree(self.store_home)
        data = self.found("--query", "rotate")
        self.assertEqual(len(data["results"]), 2)
        self.assertNotIn(GITHUB_TOKEN, json.dumps(data))
        self.assertNotIn(GITHUB_TOKEN, json.dumps(self.found("--show", "w:D1")))
        self.assertNotIn(GITHUB_TOKEN, json.dumps(self.found("--show", "w:E1")))
        self.assertEqual(tree(self.store_home), before)

    def test_text_is_clipped_in_results_but_full_in_show(self):
        self.write("w", decisions=[{"text": "clipme " + "x" * 400}])
        self.assertEqual(len(self.found("--query", "clipme")["results"][0]["text"]), 160)
        self.assertEqual(len(self.found("--show", "w:D1")["entry"]["text"]), 407)

    def test_search_over_the_log_runs_through_the_runtime_wrapper(self):
        self.write("w", decisions=[{"text": "wrapper finds this decision"}])
        env = dict(os.environ, PROVOST_CONFIG=str(self.config_path), PYTHONDONTWRITEBYTECODE="1")
        env.pop("PROVOST_PYTHON", None)
        result = subprocess.run([BASH, str(REPO / "hooks" / "runtime"), "search", "--workspace", str(self.root),
                                 "--query", "wrapper", "--kind", "decision"], capture_output=True, text=True,
                                encoding="utf-8", env=env, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(json.loads(result.stdout)["results"][0]["ref"], "w:D1")

    def test_a_work_id_starting_with_at_round_trips_through_its_ref(self):
        self.write("@odd", decisions=[{"text": "omicron under an at work id"}])
        ref = self.found("--query", "omicron")["results"][0]["ref"]
        self.assertEqual(ref, "@%s:@odd:D1" % self.ws.repo_key[:16])
        shown = self.found("--show", ref)
        self.assertEqual((shown["work_id"], shown["entry"]["id"]), ("@odd", "D1"))

    def test_work_filter_reads_only_the_named_log(self):
        self.write("good", decisions=[{"text": "pi good decision"}])
        self.write("broken", decisions=[{"text": "pi broken decision"}])
        self.log_path("broken").write_text("not json\n", encoding="utf-8")
        named = self.found("--query", "pi", "--work", "good")
        self.assertEqual((self.ids(named), named["skipped"]), ([("good", "D1")], 0))
        self.assertEqual(self.found("--query", "pi")["skipped"], 1)
        missing = self.found("--query", "pi", "--work", "absent")
        self.assertEqual((missing["results"], missing["skipped"]), ([], 0))

    def test_date_is_the_utc_date(self):
        self.write("w", decisions=[{"text": "tau late evening decision"}])
        self.retime("w", {"D1": "2026-03-01T23:30:00-05:00"})
        hit = self.found("--query", "tau")["results"][0]
        self.assertEqual((hit["recorded_at"], hit["date"]), ("2026-03-01T23:30:00-05:00", "2026-03-02"))

    def test_evidence_state_is_the_newest_entry_with_the_same_claim_even_across_unlinked_chains(self):
        self.write("w", evidence=[{"claim": "phi suite passes", "state": "pass-observed", "ref": "r"}])
        self.write("w", evidence=[{"claim": "phi suite passes", "state": "fail", "ref": "r"},
                                  {"claim": "phi other claim", "state": "unverified", "ref": "r"}])
        self.retime("w", {"E1": "2026-01-01T00:00:00+00:00", "E2": "2026-02-01T00:00:00+00:00",
                          "E3": "2026-03-01T00:00:00+00:00"})
        by_id = {r["id"]: r for r in self.found("--query", "phi")["results"]}
        for ident in ("E1", "E2"):
            self.assertEqual((by_id[ident]["state"], by_id[ident]["latest"]), ("fail", "E2"))
        self.assertEqual((by_id["E3"]["state"], by_id["E3"]["latest"]), ("unverified", "E3"))
        shown = self.found("--show", "w:E1")
        self.assertEqual((shown["state"], shown["latest"]), ("fail", "E2"))
        self.assertEqual([e["id"] for e in shown["chain"]], ["E1"])

    def test_show_of_a_foreign_repo_log_is_refused(self):
        self.write("alien", decisions=[{"text": "chi foreign"}], ws=self.other)
        self.log_path("alien").parent.mkdir(parents=True, exist_ok=True)
        self.log_path("alien", self.other).replace(self.log_path("alien"))
        self.assertEqual(self.search("--show", "alien:D1"), (1, {"error": "FOREIGN_REPO"}))
