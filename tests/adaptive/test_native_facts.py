import json
import shutil
from _support import FixtureCase, REPO

HOOK = REPO / "hooks" / "native-facts"
SESSION = "5d1c-ab12"
CWD = "C:\\Users\\x\\repo"


def event(tool, tool_input, *, session=SESSION, agent_type=None, name="PostToolUse", cwd=CWD):
    """A PostToolUse payload with the key order measured in Claude Code 2.1.291."""
    data = {"session_id": session, "transcript_path": "C:\\Users\\x\\t.jsonl", "cwd": cwd,
            "prompt_id": "p1", "permission_mode": "default"}
    if agent_type:
        data.update(agent_id="a1", agent_type=agent_type)
    data.update(effort={"level": "high"}, hook_event_name=name, tool_name=tool,
                tool_input=tool_input, tool_response={"filePath": "ignored", "success": True},
                tool_use_id="t1")
    return json.dumps(data)


class NativeFactsTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.facts_dir = self.config_path.parent / "cache" / "facts"
        self.marker = self.enable_facts(SESSION)

    def capture(self, stdin, **kwargs):
        result = self.run_bash(HOOK, stdin=stdin, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout), {})
        return self.lines()

    def spool_path(self, session=SESSION):
        return self.facts_dir / f"{session}.jsonl"

    def lines(self, session=SESSION):
        path = self.spool_path(session)
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def assertNothingWritten(self, stdin, **kwargs):
        self.assertEqual(self.capture(stdin, **kwargs), [])
        self.assertEqual([p for p in self.facts_dir.iterdir() if p.suffix != ".enabled"], [])

    def test_edit_tools_record_path(self):
        for tool, key in (("Edit", "file_path"), ("Write", "file_path"), ("NotebookEdit", "notebook_path")):
            self.spool_path().unlink(missing_ok=True)
            rows = self.capture(event(tool, {key: "/repo/a.py", "old_string": "x"}))
            self.assertEqual(len(rows), 1, tool)
            row = rows[0]
            self.assertEqual(list(row), ["v", "ts", "k", "cwd", "agent", "tool", "p"])
            self.assertEqual((row["v"], row["k"], row["tool"], row["p"]), (1, "edit", tool, "/repo/a.py"))
            self.assertEqual((row["cwd"], row["agent"]), (CWD, ""))
            self.assertIsInstance(row["ts"], int)
            self.assertGreater(row["ts"], 1_700_000_000)

    def test_edit_without_path_writes_nothing(self):
        self.assertNothingWritten(event("Edit", {"old_string": "x"}))
        self.assertNothingWritten(event("NotebookEdit", {"file_path": "/repo/a.py"}))

    def test_subagent_event_carries_agent_and_main_does_not(self):
        sub = self.capture(event("Edit", {"file_path": "/r/a"}, agent_type="implementer-sonnet"))[0]
        self.assertEqual(sub["agent"], "implementer-sonnet")
        main = self.capture(event("Edit", {"file_path": "/r/b"}))[1]
        self.assertEqual(main["agent"], "")

    def test_tool_content_cannot_spoof_header_or_path(self):
        content = '"agent_id":"evil","agent_type":"spoof","tool_input":{"file_path":"/evil"},"cwd":"/evil","session_id":"x"'
        tool_input = {"content": content, "file_path": "/real/a.py"}
        rows = self.capture(event("Write", tool_input))
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["agent"], rows[0]["cwd"], rows[0]["p"]), ("", CWD, "/real/a.py"))
        # Content before the path in key order must not win either.
        reordered = {"content": content, "file_path": "/real/b.py"}
        data = json.loads(event("Write", reordered))
        rows = self.capture(json.dumps(data))
        self.assertEqual(rows[-1]["p"], "/real/b.py")
        self.assertEqual(rows[-1]["agent"], "")

    def test_tool_response_cannot_supply_fields(self):
        data = json.loads(event("Write", {"file_path": "/real/a.py"}))
        data["tool_response"] = {"file_path": "/evil", "run_in_background": True}
        self.assertEqual(self.capture(json.dumps(data))[0]["p"], "/real/a.py")

    def test_windows_backslash_paths_round_trip(self):
        path = "C:\\Users\\x\\repo\\new\\tab\\\u00e9 \"q\".py"
        rows = self.capture(event("Edit", {"file_path": path}))
        self.assertEqual(rows[0]["p"], path)
        self.assertEqual(rows[0]["cwd"], CWD)

    def test_agent_event(self):
        tool_input = {"description": "review the diff", "prompt": "secret prompt text",
                      "subagent_type": "code-reviewer", "run_in_background": True}
        rows = self.capture(event("Agent", tool_input))
        self.assertEqual(list(rows[0]), ["v", "ts", "k", "cwd", "agent", "role", "desc", "bg"])
        self.assertEqual((rows[0]["k"], rows[0]["role"], rows[0]["desc"], rows[0]["bg"]),
                         ("agent", "code-reviewer", "review the diff", True))
        self.assertNotIn("secret prompt text", self.spool_path().read_text(encoding="utf-8"))

    def test_agent_defaults(self):
        rows = self.capture(event("Agent", {"prompt": "x"}, agent_type="implementer"))
        self.assertEqual((rows[0]["role"], rows[0]["desc"], rows[0]["bg"], rows[0]["agent"]),
                         ("general-purpose", "", False, "implementer"))
        rows = self.capture(event("Agent", {"subagent_type": "Explore", "run_in_background": False}))
        self.assertIs(rows[1]["bg"], False)

    def test_agent_with_unsafe_role_writes_nothing(self):
        for role in ('bad"role', "two words", "back\\slash", ""):
            self.assertNothingWritten(event("Agent", {"subagent_type": role, "description": "d"}))

    def test_description_is_clipped_and_stays_valid_json(self):
        # The clip is 120 characters of the raw (escaped) JSON text, then repaired.
        cases = [("a" * 300, "a" * 120),
                 ("a" * 119 + "\\", "a" * 119),                    # dangling backslash dropped
                 ("a" * 118 + "\\\\", "a" * 118 + "\\"),            # complete escaped backslash kept
                 ("a" * 119 + "\n", "a" * 119),                    # cut inside the \n escape
                 ("a" * 118 + "\u00e9", "a" * 118),                # cut right after \u
                 ("a" * 115 + "\u00e9", "a" * 115),                # cut inside \u00e9
                 ("a" * 114 + "\u00e9", "a" * 114 + "\u00e9"),      # exactly complete
                 ("a" * 114 + "\U0001f600", "a" * 114),            # lone high surrogate dropped
                 ("a" * 112 + "\U0001f600", "a" * 112),            # partial low escape, then the high one
                 ("a" * 113 + "\U0001f600", "a" * 113),
                 ("a" * 116 + "\\u1", "a" * 116 + "\\u1"),          # literal backslash-u text kept
                 ("short", "short")]
        for description, expected in cases:
            self.spool_path().unlink(missing_ok=True)
            rows = self.capture(event("Agent", {"subagent_type": "Explore", "description": description}))
            self.assertEqual(rows[0]["desc"], expected, description[-8:])
            rows[0]["desc"].encode("utf-8")  # no lone surrogate survives the repair

    def test_raw_non_ascii_description_stays_valid(self):
        data = json.loads(event("Agent", {"subagent_type": "Explore", "description": "\u00e9\u4e2d" * 100}))
        rows = self.capture(json.dumps(data, ensure_ascii=False))
        self.assertEqual(rows[0]["desc"], ("\u00e9\u4e2d" * 100)[:len(rows[0]["desc"])])
        self.assertGreater(len(rows[0]["desc"]), 0)

    def test_worktree_events(self):
        rows = self.capture(event("EnterWorktree", {"path": "C:\\wt\\one"}))
        self.assertEqual(list(rows[0]), ["v", "ts", "k", "cwd", "agent", "tool", "p"])
        self.assertEqual((rows[0]["k"], rows[0]["tool"], rows[0]["p"]), ("worktree", "EnterWorktree", "C:\\wt\\one"))
        rows = self.capture(event("EnterWorktree", {"name": "feature-x"}))
        self.assertEqual(rows[1]["p"], "feature-x")
        rows = self.capture(event("ExitWorktree", {"action": "keep"}))
        self.assertEqual((rows[2]["tool"], rows[2]["p"]), ("ExitWorktree", ""))

    def dirs(self, command, tool="Bash", cwd=CWD, **kwargs):
        self.spool_path().unlink(missing_ok=True)
        rows = self.capture(event(tool, {"command": command, "description": "d"}, cwd=cwd), **kwargs)
        for row in rows:
            self.assertEqual(list(row), ["v", "ts", "k", "cwd", "agent", "p"])
            self.assertEqual(row["k"], "dir")
        return [row["p"] for row in rows]

    def test_dir_extraction_forms(self):
        self.assertEqual(self.dirs("cd /tmp/a && ls"), ["/tmp/a"])
        self.assertEqual(self.dirs('cd "C:\\Program Files\\x"; ls'), ["C:\\Program Files\\x"])
        self.assertEqual(self.dirs("cd 'D:\\my dir' && make"), ["D:\\my dir"])
        self.assertEqual(self.dirs("git -C ../other status"), ["../other"])
        self.assertEqual(self.dirs('git -C "C:\\r p" log --oneline'), ["C:\\r p"])
        self.assertEqual(self.dirs("Set-Location C:\\Users\\x\\other; git status", tool="PowerShell"), ["C:\\Users\\x\\other"])
        self.assertEqual(self.dirs("Set-Location -Path 'D:\\a b'", tool="PowerShell"), ["D:\\a b"])
        self.assertEqual(self.dirs('Set-Location -LiteralPath "D:\\a b" | Out-Null', tool="PowerShell"), ["D:\\a b"])
        self.assertEqual(self.dirs("(cd sub/dir) && echo hi"), ["sub/dir"])
        self.assertEqual(self.dirs("echo x\ncd /second\nls"), ["/second"])

    def test_dir_extraction_limits(self):
        self.assertEqual(self.dirs("cd /a; cd /b && git -C /c log; cd /d"), ["/a", "/b", "/c"])
        self.assertEqual(self.dirs("cd - && cd '' && cd /ok"), ["/ok"])
        self.assertEqual(self.dirs("abcd /nope; git status; echo cd"), [])
        self.assertEqual(self.dirs("cd $(dirname x) && cd `pwd`"), [])

    def test_command_text_is_never_stored(self):
        command = 'export PASSWORD=hunter2; cd /work && curl -H "Authorization: hunter2" https://x/?t=hunter2'
        self.assertEqual(self.dirs(command), ["/work"])
        self.assertNotIn("hunter2", self.spool_path().read_text(encoding="utf-8"))
        self.spool_path().unlink()
        self.assertNothingWritten(event("Bash", {"command": "PASSWORD=hunter2 ./deploy.sh"}))

    def test_large_input_is_handled(self):
        content = "line \"quoted\" \\ text\n" * 20000
        rows = self.capture(event("Write", {"file_path": "/big/a.py", "content": content}))
        self.assertEqual(rows[0]["p"], "/big/a.py")

    def test_other_tools_write_nothing(self):
        for tool in ("Read", "Grep", "TaskCreate", "mcp__x__y"):
            self.assertNothingWritten(event(tool, {"file_path": "/a", "command": "cd /x"}))

    def test_other_events_write_nothing(self):
        for name in ("PreToolUse", "UserPromptSubmit", "SessionStart", "SubagentStop"):
            self.assertNothingWritten(event("Edit", {"file_path": "/a"}, name=name))

    def test_disabled_or_missing_config_writes_nothing(self):
        stdin = event("Edit", {"file_path": "/a"})
        self.config_path.write_text(json.dumps(dict(self.cfg, handoff=False)), encoding="utf-8")
        self.assertNothingWritten(stdin)
        self.config_path.write_text(json.dumps({k: v for k, v in self.cfg.items() if k != "handoff"}), encoding="utf-8")
        self.assertNothingWritten(stdin)
        self.config_path.unlink()
        self.assertNothingWritten(stdin)

    def test_pretty_printed_config_is_accepted(self):
        self.config_path.write_text(json.dumps(self.cfg, indent=2), encoding="utf-8")
        self.assertEqual(len(self.capture(event("Edit", {"file_path": "/a"}))), 1)

    def test_unsafe_session_id_writes_nothing(self):
        for session in ("", "../evil", "a/b", "a\\\\b", "a b", "a.b", "a:b", 'a"b'):
            self.assertNothingWritten(event("Edit", {"file_path": "/a"}, session=session))
        self.assertEqual(list(self.facts_dir.parent.iterdir()), [self.facts_dir])

    def test_session_without_marker_writes_and_reminds_nothing(self):
        self.marker.unlink()
        self.assertNothingWritten(event("Edit", {"file_path": "C:\\Users\\x\\repo\\a.py"}))
        self.assertNothingWritten(event("Agent", {"subagent_type": "implementer"}))
        (self.facts_dir / f"{SESSION}.pending-edit").touch()
        self.assertEqual(self.prompt(), {})
        self.assertEqual(self.flags(), ["pending-edit"])
        self.enable_facts("other-1")  # another session's marker does not enable this one
        self.capture(event("Edit", {"file_path": "/a"}))
        self.assertEqual(self.lines(), [])
        self.capture(event("Edit", {"file_path": "/a"}, session="other-1"))
        self.assertEqual(len(self.lines("other-1")), 1)

    def test_missing_session_id_writes_nothing(self):
        data = json.loads(event("Edit", {"file_path": "/a"}))
        del data["session_id"]
        self.assertNothingWritten(json.dumps(data))

    def test_missing_facts_dir_is_not_created(self):
        shutil.rmtree(self.facts_dir)
        result = self.run_bash(HOOK, stdin=event("Edit", {"file_path": "/a"}))
        self.assertEqual((result.returncode, json.loads(result.stdout), result.stderr), (0, {}, ""))
        self.assertFalse(self.facts_dir.exists())

    def test_unwritable_target_is_silent(self):
        self.spool_path().mkdir()
        result = self.run_bash(HOOK, stdin=event("Edit", {"file_path": "/a"}))
        self.assertEqual((result.returncode, json.loads(result.stdout), result.stderr), (0, {}, ""))

    def test_malformed_input_writes_nothing(self):
        for stdin in ("", "not json", "{", "{}", '{"hook_event_name":"PostToolUse"}',
                      '{"hook_event_name":"PostToolUse","tool_name":"Edit","session_id":"s1"}',
                      '{"session_id":"s1","hook_event_name":"PostToolUse","tool_name":"Edit","tool_input":'):
            self.assertNothingWritten(stdin)

    def test_appends_across_runs(self):
        self.capture(event("Edit", {"file_path": "/a"}))
        rows = self.capture(event("Write", {"file_path": "/b"}))
        self.assertEqual([row["p"] for row in rows], ["/a", "/b"])

    def test_no_python_is_launched(self):
        log = self.root / "python.log"
        bin_dir = self.root / "bin"
        for name in ("python", "python3", "py"):
            self.fake_interpreter(bin_dir, name, log)
        self.capture(event("Edit", {"file_path": "/a"}), path_dirs=[bin_dir])
        self.capture(event("Bash", {"command": "cd /x"}), path_dirs=[bin_dir])
        self.capture(event("Agent", {"subagent_type": "implementer"}), path_dirs=[bin_dir])
        result = self.run_bash(HOOK, stdin=json.dumps({"session_id": SESSION, "hook_event_name": "UserPromptSubmit",
                                                       "prompt": "x"}), path_dirs=[bin_dir])
        self.assertIn("hookSpecificOutput", json.loads(result.stdout))
        self.assertFalse(log.exists(), "native-facts must not launch an interpreter")

    def flags(self, session=SESSION):
        return sorted(p.name[len(session) + 1:] for p in self.facts_dir.glob(session + ".*")
                      if p.suffix not in (".jsonl", ".enabled"))

    def test_main_repo_edit_sets_pending_edit(self):
        for tool, key in (("Edit", "file_path"), ("Write", "file_path"), ("NotebookEdit", "notebook_path")):
            for flag in self.facts_dir.glob(SESSION + ".pending-*"):
                flag.unlink()
            self.capture(event(tool, {key: "C:\\Users\\x\\repo\\a.py"}))
            self.assertEqual(self.flags(), ["pending-edit"], tool)
            self.assertEqual((self.facts_dir / f"{SESSION}.pending-edit").stat().st_size, 0)

    def test_subagent_edit_does_not_set_pending_edit(self):
        self.capture(event("Edit", {"file_path": "/repo/a.py"}, agent_type="implementer"))
        data = json.loads(event("Write", {"file_path": "/repo/b.py"}))
        data = {"agent_id": "a1", **data}  # agent_id alone also marks a subagent
        self.capture(json.dumps(data))
        self.assertEqual(self.flags(), [])
        self.assertEqual(len(self.lines()), 2)

    def test_outside_repo_edits_do_not_set_pending_edit(self):
        from provost_runtime import facts
        outside = ["C:\\Users\\x\\.claude\\jobs\\j1\\brief.md", "C:/Users/x/.claude/jobs/j1/a.md",
                   "C:\\Users\\x\\.Claude\\Projects\\p\\memory\\m.md", "/c/Users/x/.claude/projects/p/a.md",
                   "C:\\Users\\X\\AppData\\Local\\Temp\\a.txt", "C:/Users/X/appdata/local/temp/b.txt",
                   "/tmp/a.py", "C:\\tmp\\x.py", "/TMP/y"]
        for path in outside:
            self.assertTrue(facts._outside(path), path)  # the hook mirrors facts.OUTSIDE_DEFAULT
            self.capture(event("Edit", {"file_path": path}))
            self.assertEqual(self.flags(), [], path)
        for path in ("C:\\Users\\x\\repo\\a.py", "/home/x/temperature/a.py", "C:/Users/x/.claude/settings.json"):
            self.assertFalse(facts._outside(path), path)
            (self.facts_dir / f"{SESSION}.pending-edit").unlink(missing_ok=True)
            self.capture(event("Edit", {"file_path": path}))
            self.assertEqual(self.flags(), ["pending-edit"], path)

    def test_writer_dispatch_sets_pending_dispatch(self):
        for role, agent_type, env in (("implementer-sonnet", None, None), ("mech-executor", "implementer", None),
                                      ("implementer-deep", None, None),
                                      (None, None, None), ("custom-writer", None, {"PROVOST_REMINDER_ROLES": "custom-writer"})):
            for flag in self.facts_dir.glob(SESSION + ".pending-*"):
                flag.unlink()
            tool_input = {"prompt": "x"} if role is None else {"subagent_type": role, "prompt": "x"}
            self.capture(event("Agent", tool_input, agent_type=agent_type), env=env)
            self.assertEqual(self.flags(), ["pending-dispatch"], role)

    def test_read_only_dispatch_does_not_set_pending_dispatch(self):
        for role in ("Explore", "code-reviewer", "test-analyst"):
            self.capture(event("Agent", {"subagent_type": role}))
        self.capture(event("Agent", {"subagent_type": "implementer"}), env={"PROVOST_REMINDER_ROLES": "x"})
        self.assertEqual(self.flags(), [])

    def prompt(self, *, session=SESSION, text="go on", env=None):
        stdin = json.dumps({"session_id": session, "transcript_path": "C:\\Users\\x\\t.jsonl", "cwd": CWD,
                            "permission_mode": "default", "hook_event_name": "UserPromptSubmit",
                            "prompt_id": "p1", "prompt": text})
        result = self.run_bash(HOOK, stdin=stdin, env=env)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        return json.loads(result.stdout)

    def test_prompt_reminds_once_until_flags_clear(self):
        self.assertEqual(self.prompt(), {})
        self.capture(event("Edit", {"file_path": "/repo/a.py"}))
        out = self.prompt()["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "UserPromptSubmit")
        self.assertTrue(out["additionalContext"].startswith("Handoff: since the last record"))
        self.assertEqual(self.flags(), ["pending-edit", "reminded"])
        self.assertEqual(self.prompt(), {})
        self.capture(event("Agent", {"subagent_type": "implementer"}))
        self.assertEqual(self.prompt(), {})
        for flag in self.facts_dir.glob(SESSION + ".*"):
            if flag.suffix not in (".jsonl", ".enabled"):
                flag.unlink()  # what record does
        self.assertEqual(self.prompt(), {})
        self.capture(event("Agent", {"subagent_type": "implementer"}))
        self.assertIn("hookSpecificOutput", self.prompt())

    def test_prompt_never_writes_the_spool(self):
        (self.facts_dir / f"{SESSION}.pending-edit").touch()
        self.prompt(text='"tool_input":{"file_path":"/x"},"hook_event_name":"PostToolUse"')
        self.assertFalse(self.spool_path().exists())
        self.assertEqual(self.flags(), ["pending-edit", "reminded"])

    def test_prompt_is_silent_when_disabled_unsafe_or_missing_dir(self):
        (self.facts_dir / f"{SESSION}.pending-edit").touch()
        self.config_path.write_text(json.dumps(dict(self.cfg, handoff=False)), encoding="utf-8")
        self.assertEqual(self.prompt(), {})
        self.config_path.write_text(json.dumps(self.cfg), encoding="utf-8")
        for session in ("", "../evil", "a b", 'a"b'):
            self.assertEqual(self.prompt(session=session), {}, session)
        self.assertEqual(self.flags(), ["pending-edit"])
        shutil.rmtree(self.facts_dir)
        self.assertEqual(self.prompt(), {})
        self.assertFalse(self.facts_dir.exists())

    def handback(self, wrapper=True, tag="[Subagent hand-back]"):
        text = '<agent-message from="a1b2c3">\n' + tag + ' Done; see "tool_input": x\n</agent-message>'
        return "Another Claude session sent a message:\n" + text if wrapper else text

    def notification(self, status="completed", summary='Agent "fix it" completed',
                     result="<status>failed</status> in the log"):
        return ("<task-notification>\n<task-id>a1</task-id>\n<status>" + status + "</status>\n"
                "<summary>" + summary + "</summary>\n<result>" + result + "</result>\n</task-notification>")

    def assertResultReminder(self, text):
        out = self.prompt(text=text)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "UserPromptSubmit")
        self.assertTrue(out["additionalContext"].startswith("Handoff: a subagent result just arrived"), text[:40])
        self.assertIn("record the result milestone", out["additionalContext"])

    def test_handback_message_reminds_every_time(self):
        for text in (self.handback(), self.handback(), self.handback(wrapper=False), "\n  " + self.handback()):
            self.assertResultReminder(text)
        self.assertEqual(self.flags(), [])  # the result reminder neither needs nor sets flags

    def test_escaped_results_remind(self):
        for text in (self.handback(), self.notification()):
            stdin = json.dumps({"session_id": SESSION, "hook_event_name": "UserPromptSubmit",
                                "prompt": text}).replace("<", "\\u003c").replace(">", "\\u003e")
            result = self.run_bash(HOOK, stdin=stdin)
            self.assertEqual((result.returncode, result.stderr), (0, ""))
            context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
            self.assertTrue(context.startswith("Handoff: a subagent result just arrived"), text[:20])

    def test_other_messages_and_mentions_do_not_remind(self):
        for text in (self.handback(tag="Can you check the build?"), "please explain " + self.handback(),
                     "x\n" + self.handback(wrapper=False),
                     "Another Claude session sent a message:\nhi [Subagent hand-back]"):
            self.assertEqual(self.prompt(text=text), {}, text[:40])
        self.assertEqual(self.flags(), [])

    def test_agent_notification_without_handback_reminds(self):
        # The first <status> decides: a later failed status inside the result does not.
        for text in (self.notification(), "\n" + self.notification()):
            self.assertResultReminder(text)

    def test_other_notifications_do_not_remind(self):
        pointer = "This agent's report was delivered to you as a message from \"a1b2\" (its SubagentHandback call)"
        for text in (self.notification(result=pointer),
                     self.notification(summary='Background command "Run tests" completed (exit code 0)'),
                     self.notification(summary="Background command done", result="<summary>Agent x</summary>"),
                     self.notification("failed"), self.notification("killed"),
                     self.notification("failed", result="<status>completed</status>"),
                     "<task-notification>\n<summary>Agent x</summary>\n</task-notification>",
                     "please explain " + self.notification(), "x\n" + self.notification()):
            self.assertEqual(self.prompt(text=text), {}, text[-60:])
        self.assertEqual(self.flags(), [])

    def test_result_and_pending_flag_combine_into_one_context(self):
        (self.facts_dir / f"{SESSION}.pending-dispatch").touch()
        out = self.prompt(text=self.handback())
        context = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("since the last record", context)
        self.assertIn("result milestone", context)
        self.assertEqual(self.flags(), ["pending-dispatch", "reminded"])
        context = self.prompt(text=self.handback())["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("since the last record", context)  # the flag part stays once per record
        self.assertEqual(self.prompt(), {})

    def test_result_without_marker_is_silent(self):
        self.marker.unlink()
        (self.facts_dir / f"{SESSION}.pending-edit").touch()
        self.assertEqual(self.prompt(text=self.handback()), {})
        self.assertEqual(self.prompt(text=self.notification()), {})
        self.assertEqual(self.flags(), ["pending-edit"])

    def test_dir_same_as_cwd_is_not_recorded(self):
        for command in ("cd C:/Users/x/repo && ls", "cd 'c:\\users\\X\\Repo\\' && ls",
                        "git -C /c/Users/x/repo status", 'cd "C:\\Users\\x\\repo/"',
                        "cd C:\\Users\\x\\repo && ls"):
            self.assertEqual(self.dirs(command), [], command)
        self.assertEqual(self.dirs("cd /d", cwd="D:\\"), [])
        self.assertEqual(self.dirs("cd /d/work/ && cd D:/other", cwd="d:/WORK"), ["D:/other"])
        self.assertEqual(self.dirs("cd C:/Users/x/repo2; cd sub"), ["C:/Users/x/repo2", "sub"])

    def test_runtime_hash_covers_the_facts_hook(self):
        from provost_runtime.cli import runtime_hash
        copy = self.root / "runtime-copy"
        shutil.copytree(REPO / "hooks", copy / "hooks")
        base = runtime_hash(copy)
        (copy / "hooks" / "native-facts").unlink()
        self.assertNotEqual(runtime_hash(copy), base)
