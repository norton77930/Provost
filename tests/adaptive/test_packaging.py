import json
import unittest
from _support import REPO

class PackagingTests(unittest.TestCase):
    def test_entrypoints_and_guides_ship(self):
        for path in ("hooks/runtime", "hooks/handoff-event", "skills/handoff/SKILL.md",
                     "docs/compatibility/spec-kit.md", "docs/compatibility/openspec.md",
                     "docs/compatibility/matt-workflow.md", "docs/examples/adaptive-handoff-pilot.md"):
            self.assertTrue((REPO / path).is_file(), path)
        for name in ("README.md", "README.zh-TW.md"):
            text = (REPO / name).read_text(encoding="utf-8")
            for term in ("adaptive-implementer", "Python", "adaptive-handoff-pilot.md"):
                self.assertIn(term, text)

    def test_hook_contract(self):
        hooks = json.loads((REPO / "hooks/hooks.json").read_text())["hooks"]
        self.assertEqual(set(hooks), {"SessionStart", "PreCompact", "PostCompact"})
        for name in ("SessionStart", "PreCompact", "PostCompact"):
            hook = hooks[name][0]["hooks"][0]
            self.assertFalse(hook["async"])
            self.assertEqual(hook["timeout"], 30)
            self.assertTrue(hook["command"].startswith('bash "${CLAUDE_PLUGIN_ROOT}/hooks/'), hook["command"])
