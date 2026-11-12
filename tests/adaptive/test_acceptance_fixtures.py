import json
import tempfile
import unittest
from pathlib import Path

from _support import REPO  # noqa: F401  (puts scripts/ on sys.path)

from provost_runtime import acceptance

# The cases the board mod's test reads too (mods/board/tests/board.test.ts): one input, one expected reading.
# This is the reference side. Not covered by cases: the 1 MB size cap, an unreadable file and staleness.
# A blank id or `closed` is pinned with ordinary whitespace only; str.strip() and the board's trim() differ
# on a few exotic characters and that is left alone.
CASES = Path(__file__).resolve().parents[2] / "mods" / "board" / "tests" / "fixtures" / "acceptance-cases.ts"


def load_cases():
    """The list inside `export default [...]` of the cases module (plain JSON between its brackets)."""
    text = CASES.read_text(encoding="utf-8")
    start = text.index("export default [") + len("export default ")
    return json.loads(text[start:text.rindex("]") + 1])


def reading(path):
    """The three readings of a file: malformed, closed (finished), or open with each gate's id, kind and status."""
    try:
        data = acceptance.load(path)
    except acceptance.Malformed:
        return {"reading": "malformed"}
    if "closed" in data:
        return {"reading": "closed"}
    return {"reading": "open",
            "gates": [{"id": g["id"], "kind": g["kind"], "status": g["status"]} for g in data["gates"]]}


class AcceptanceFixtureCases(unittest.TestCase):
    def test_every_case_reads_as_expected(self):
        cases = load_cases()
        self.assertTrue(cases)
        names = [case["name"] for case in cases]
        self.assertEqual(len(names), len(set(names)))
        with tempfile.TemporaryDirectory(prefix="provost-test-") as tmp:
            for index, case in enumerate(cases):
                with self.subTest(case["name"]):
                    path = Path(tmp) / ("case-%d.json" % index)
                    path.write_bytes(case["text"].encode("utf-8"))  # bytes, so a BOM stays a BOM
                    self.assertEqual(reading(path), case["expected"])

    def test_runtime_only_cases_say_why(self):
        cases = load_cases()
        marked = [case["name"] for case in cases if "runtimeOnly" in case]
        self.assertEqual(len(marked), 2, marked)
        for case in cases:
            if "runtimeOnly" in case:
                self.assertIsInstance(case["runtimeOnly"], str)
                self.assertTrue(case["runtimeOnly"].strip())


if __name__ == "__main__":
    unittest.main()
