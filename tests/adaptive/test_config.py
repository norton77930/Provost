import json
from _support import FixtureCase
from provost_runtime.config import ConfigError, load_config, resolve_mode, workspace_key


class ConfigTests(FixtureCase):
    def test_absent_config_is_legacy_and_read_only(self):
        self.assertEqual(resolve_mode(load_config(self.root / "missing.json"), self.root), "legacy")
        self.assertNoStore()

    def test_exact_workspace_not_prefix_or_worktree(self):
        self.assertEqual(resolve_mode(self.cfg, self.root), "adaptive")
        self.assertEqual(resolve_mode(self.cfg, self.root / "worktree"), "legacy")
        self.assertEqual(resolve_mode(self.cfg, self.root.with_name(self.root.name + "-copy")), "legacy")

    def test_all_workspaces_requires_boolean(self):
        self.cfg["all_workspaces"] = True
        self.assertEqual(resolve_mode(self.cfg, self.root / "other"), "adaptive")
        self.cfg["all_workspaces"] = "false"
        self.config_path.write_text(json.dumps(self.cfg))
        with self.assertRaises(ConfigError):
            load_config(self.config_path)

    def test_exclude_roots_win_over_all_workspaces_and_exact_allowlist(self):
        self.cfg["exclude_roots"] = [str(self.root)]
        self.assertEqual(resolve_mode(self.cfg, self.root), "legacy")  # exact workspace_roots match
        self.cfg["workspace_roots"] = []
        self.cfg["all_workspaces"] = True
        self.assertEqual(resolve_mode(self.cfg, self.root), "legacy")
        self.assertEqual(resolve_mode(self.cfg, self.root.with_name(self.root.name + "-copy")), "adaptive")

    def test_exclude_roots_match_nested_paths_not_sibling_prefixes(self):
        self.cfg.update(all_workspaces=True, exclude_roots=[str(self.root)])
        self.assertEqual(resolve_mode(self.cfg, self.root / "a" / "b"), "legacy")
        self.assertEqual(resolve_mode(self.cfg, self.root.with_name(self.root.name + "2")), "adaptive")
        self.assertEqual(resolve_mode(self.cfg, self.root.parent), "adaptive")

    def test_exclude_roots_cover_main_checkout_of_a_worktree(self):
        other = self.root.with_name(self.root.name + "-wt")
        self.cfg.update(all_workspaces=True, exclude_roots=[str(self.root)])
        self.assertEqual(resolve_mode(self.cfg, other, self.root), "legacy")
        self.assertEqual(resolve_mode(self.cfg, other, other), "adaptive")

    def test_exclude_roots_match_spelling_variants(self):
        import os
        text = str(self.root)
        variants = {"trailing separator": text + os.sep, "forward slashes": self.root.as_posix(),
                    "forward slashes, trailing": self.root.as_posix() + "/"}
        if os.name == "nt":
            variants["different case"] = text.swapcase()
        for label, value in variants.items():
            with self.subTest(label, value=value):
                self.cfg.update(all_workspaces=True, exclude_roots=[value])
                self.assertEqual(resolve_mode(self.cfg, self.root), "legacy")
                self.assertEqual(resolve_mode(self.cfg, self.root / "nested"), "legacy")
                self.assertEqual(resolve_mode(self.cfg, self.root.with_name(self.root.name + "2")), "adaptive")

    def test_exclude_roots_validated_and_optional(self):
        for value in ("C:/tmp", ["relative/path"], [1], {"a": "b"}):
            cfg = dict(self.cfg, exclude_roots=value)
            self.config_path.write_text(json.dumps(cfg))
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "INVALID_EXCLUDE_ROOTS"):
                load_config(self.config_path)
        self.config_path.write_text(json.dumps(self.cfg))
        self.assertEqual(load_config(self.config_path)["exclude_roots"], [])
        self.config_path.write_text(json.dumps(dict(self.cfg, exclude_roots=[str(self.root / "x")])))
        self.assertEqual(resolve_mode(load_config(self.config_path), self.root), "adaptive")

    def test_invalid_schema_types_and_writer_limits(self):
        for key, value in [("schema_version", 99), ("max_writers", True),
                           ("burst_writers", 4), ("max_writers", 3),
                           ("workspace_roots", ["relative/path"]), ("unknown", "value")]:
            cfg = dict(self.cfg)
            cfg[key] = value
            if key == "max_writers" and value == 3:
                cfg["burst_writers"] = 2
            self.config_path.write_text(json.dumps(cfg))
            with self.subTest(key=key, value=value), self.assertRaises(ConfigError):
                load_config(self.config_path)

    def test_invalid_json_and_unicode_identity(self):
        self.config_path.write_text("{bad")
        with self.assertRaises(ConfigError):
            load_config(self.config_path)
        self.assertEqual(workspace_key(self.root / "中文 空格"), workspace_key(self.root / "中文 空格" / "."))
        self.assertNotEqual(workspace_key(self.root), workspace_key(self.root / "中文 空格"))
