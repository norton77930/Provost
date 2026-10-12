import json
import re
import unittest
from _support import REPO

# Constructs newer than bash 3.2 (macOS /bin/bash), checked outside full-line comments.
BASH4_ONLY = {
    "case modification ${x,,} ${x^^}": r"\$\{[#!]?[A-Za-z_][A-Za-z0-9_]*(\[[^]]*\])?[,^]",
    "transformation ${x@Q}": r"\$\{[^}]*@[QEPAaKkUuL]\}",
    "mapfile/readarray": r"\b(mapfile|readarray)\b",
    "associative array or nameref": r"\b(declare|local|typeset)\s+-[A-Za-z]*[An]",
    "|& pipe": r"(?<!\|)\|&",
    "&>> append": r"&>>",
    "case fallthrough ;& ;;&": r";;?&(\s|$)",
    "negative array index": r"\$\{[A-Za-z_][A-Za-z0-9_]*\[-",
    "bash 4+ variable": r"\b(EPOCHSECONDS|EPOCHREALTIME|BASHPID|BASH_ARGV0)\b",
    "bash 4+ shopt": r"\bshopt\s+-s\s+(globstar|lastpipe|direxpand|globasciiranges|inherit_errexit)",
    "[[ -v ]]": r"\[\[\s+(!\s+)?-v\s",
    "coproc": r"\bcoproc\b",
    "brace range step": r"\{-?\w+\.\.-?\w+\.\.",
}

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

    def test_hooks_run_on_bash_3_2(self):
        scripts = sorted(p for p in (REPO / "hooks").iterdir() if p.is_file() and p.suffix != ".json")
        self.assertIn("native-facts", [p.name for p in scripts])
        found = []
        for path in scripts:
            lines = path.read_text(encoding="utf-8").splitlines()
            for number, line in enumerate(lines, 1):
                if line.lstrip().startswith("#"):
                    continue
                found += ["%s:%d %s" % (path.name, number, name)
                          for name, pattern in BASH4_ONLY.items() if re.search(pattern, line)]
                # printf %(...)T needs bash 4.2: only behind a BASH_VERSINFO check, with a fallback.
                if re.search(r"%\([^)]*\)T", line) and "BASH_VERSINFO" not in lines[number - 2]:
                    found.append("%s:%d unguarded printf %%(...)T" % (path.name, number))
        self.assertEqual(found, [])

    def test_hook_contract(self):
        hooks = json.loads((REPO / "hooks/hooks.json").read_text())["hooks"]
        self.assertEqual(set(hooks), {"SessionStart", "PreCompact", "PostCompact"})
        for name in ("SessionStart", "PreCompact", "PostCompact"):
            hook = hooks[name][0]["hooks"][0]
            self.assertFalse(hook["async"])
            self.assertEqual(hook["timeout"], 30)
            self.assertTrue(hook["command"].startswith('bash "${CLAUDE_PLUGIN_ROOT}/hooks/'), hook["command"])
