import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))


class FixtureCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="provost-test-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.config_path = self.root / "user-config.json"
        self.cfg = {"schema_version": 1, "mode": "adaptive",
                    "workspace_roots": [str(self.root)], "all_workspaces": False,
                    "max_writers": 2, "burst_writers": 3, "handoff": True}
        self.config_path.write_text(json.dumps(self.cfg), encoding="utf-8")

    def payload(self, next_step="collect result"):
        return {"work_id": "fixture", "phase": "implementation", "sources": [],
                "assignments": [], "decisions": [], "blockers": [], "evidence": [],
                "next": next_step, "suggested_skills": []}

    def seed_snapshot(self):
        from provost_runtime.store import record, snapshot
        record(self.root, "s1", self.payload(), None, "r1")
        return snapshot(self.root, "s1", "e1")
