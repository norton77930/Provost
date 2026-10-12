import re
from _support import FixtureCase, REPO


class PolicyContractTests(FixtureCase):
    def test_opt_in_role_model_effort_and_bounded_tools(self):
        text = (REPO / "agents/adaptive-implementer.md").read_text(encoding="utf-8")
        header = text.split("---")[1]
        fields = dict(re.findall(r"^(\w+): (.+)$", header, re.M))
        self.assertEqual(fields["model"], "opus")
        self.assertEqual(fields["effort"], "high")
        self.assertNotIn("Agent", fields["tools"].split(", "))
        self.assertEqual(fields["name"], "adaptive-implementer")

    def test_legacy_model_defaults_remain_unchanged(self):
        for role, model in [("implementer", "sonnet"), ("implementer-deep", "opus")]:
            text = (REPO / ("agents/" + role + ".md")).read_text(encoding="utf-8")
            self.assertIn("model: " + model, text.split("---")[1])
