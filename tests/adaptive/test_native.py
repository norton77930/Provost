import json
import os
import subprocess
import sys
from _support import FixtureCase, REPO
from provost_runtime.hooks import handle_event

class NativeTests(FixtureCase):
    def cli(self, *args, data=None):
        env = dict(os.environ, PYTHONPATH=str(REPO / "scripts"), PROVOST_CONFIG=str(self.config_path),
                   PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
        return subprocess.run([sys.executable, "-m", "provost_runtime.cli", *args],
                              input=json.dumps(data) if data is not None else "",
                              capture_output=True, text=True, encoding="utf-8", env=env, timeout=30)
    def event(self, name="SessionStart", source="startup", session="s1"):
        return dict(hook_event_name=name, source=source, session_id=session, cwd=str(self.root))
    def context(self, result):
        return result.get("hookSpecificOutput", {}).get("additionalContext", "")

    def test_native_disabled_is_noop(self):
        for cfg in (None, {**self.cfg, "mode": "legacy"}, {**self.cfg, "workspace_roots": []}):
            if cfg is None:
                self.config_path.unlink(missing_ok=True)
            else:
                self.config_path.write_text(json.dumps(cfg))
            r=self.cli("native-hook",data=self.event())
            self.assertEqual(r.returncode,0,r.stdout+r.stderr)
            self.assertEqual(json.loads(r.stdout),{})
        self.assertNoStore()

    def test_native_invalid_config_warns_without_legacy_policy(self):
        self.config_path.write_text("{bad")
        r=self.cli("native-hook",data=self.event())
        self.assertEqual(r.returncode,0)
        d=json.loads(r.stdout)
        self.assertIn("systemMessage",d)
        self.assertEqual(self.context(d),"")
        self.assertNotIn("legacy",json.dumps(d).lower())

    def test_native_startup_gives_usable_entrypoint_not_second_policy(self):
        r=self.cli("native-hook",data=self.event())
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        text=self.context(json.loads(r.stdout))
        self.assertIn("status",text)
        self.assertIn("--expected",text)
        self.assertIn("native-handoff.md",text)
        self.assertIn("A worktree and its main checkout share one handoff store.",text)
        self.assertNotIn("identity mid-session",text)
        self.assertNotIn("provost:adaptive-implementer",text)
        self.assertNotIn("## Dispatch",text)
        self.assertLess(len(text),4000)

    def test_status_is_readonly_and_requires_binding(self):
        args=("status","--workspace",str(self.root),"--session","s1")
        r=self.cli(*args)
        self.assertEqual(json.loads(r.stdout)["error"],"SESSION_NOT_BOUND")
        self.assertNoStore()
        self.cli("native-hook",data=self.event())
        before={str(p):p.read_bytes() for p in self.store_home.rglob("*") if p.is_file()}
        self.assertTrue(before)
        r=self.cli(*args);d=json.loads(r.stdout)
        self.assertEqual(r.returncode,0,r.stdout)
        self.assertIsNone(d["expected"])
        self.assertIsNone(d["checkpoint"])
        self.assertIn("suggested_skills",d["template"])
        after={str(p):p.read_bytes() for p in self.store_home.rglob("*") if p.is_file()}
        self.assertEqual(before,after)

    def test_relocated_roundtrip_and_explicit_adoption(self):
        self.cli("native-hook",data=self.event())
        args=("--workspace",str(self.root),"--session","s1")
        status=json.loads(self.cli("status",*args).stdout)
        pl=status["template"];pl.update(work_id="native-fixture",phase="approved",next="verify slice")
        r=self.cli("record",*args,"--expected","null","--request-id","r1",data=pl)
        self.assertEqual(r.returncode,0,r.stdout)
        rev=json.loads(r.stdout)["revision"]
        self.assertEqual(json.loads(self.cli("status",*args).stdout)["expected"],rev)
        compact=self.cli("native-hook",data=self.event("PreCompact"))
        self.assertEqual(compact.returncode,0)
        listed=json.loads(self.cli("list","--workspace",str(self.root)).stdout)
        self.assertEqual(len(listed),1)
        recovered=self.cli("native-hook",data=self.event(source="compact"))
        self.assertIn("verify slice",self.context(json.loads(recovered.stdout)))
        fresh=self.cli("native-hook",data=self.event(session="s2"))
        self.assertNotIn("verify slice",self.context(json.loads(fresh.stdout)))
        r=self.cli("resume","--workspace",str(self.root),"--session","s2","--from",listed[0]["snapshot_id"])
        self.assertEqual(r.returncode,0,r.stdout)
        self.assertEqual(json.loads(r.stdout)["adopted_from"],listed[0]["snapshot_id"])

    def test_guidance_points_record_at_input_file_not_stdin(self):
        text=self.context(handle_event(self.event(),self.config_path,native=True))
        self.assertIn("--input <payload-file>",text)
        self.assertNotIn("stdin",text)

    def test_operations_guide_has_single_line_record_and_valid_example(self):
        guide=(REPO/"docs"/"native-handoff.md").read_text(encoding="utf-8")
        line=next(l for l in guide.splitlines() if " record --workspace" in l)
        self.assertIn('--input "<payload-file>"',line)
        self.assertFalse(line.rstrip().endswith("\\"))
        self.assertNotIn(" < ",line)
        flat=" ".join(guide.split())
        self.assertIn("single plain command",flat)
        for word in ("heredoc","pipe","command substitution","env-var prefix","line continuation"):
            self.assertIn(word,flat)
        block=guide.split("```json",1)[1].split("```",1)[0]
        example=json.loads(block)
        self.assertEqual({s["kind"] for s in example["sources"]},{"file","url"})
        self.assertEqual((len(example["assignments"]),len(example["evidence"])),(1,1))
        from provost_runtime.store import record
        self.assertEqual(record(self.root,"s1",example,None,"doc-example")["payload"],example)

    def test_plugin_adapter_keeps_original_policy(self):
        text=self.context(handle_event(self.event(),self.config_path))
        self.assertIn("provost:adaptive-implementer",text)
