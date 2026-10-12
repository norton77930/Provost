import json
import unittest
from _support import REPO

class WorkflowContracts(unittest.TestCase):
    def test_scenarios_have_sources_and_expected_decisions(self):
        base = REPO / "tests/adaptive/fixtures/workflows"
        cases = json.loads((base / "cases.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(cases), 18)
        self.assertEqual({c["workflow"] for c in cases}, {"spec-kit", "openspec", "matt", "shared"})
        for case in cases:
            self.assertTrue((base / case["source"]).is_file())
            self.assertTrue(case["expected"])
            self.assertTrue(case["reason"])
            self.assertIn(case["id"], (base / case["source"]).read_text(encoding="utf-8"))
