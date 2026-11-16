import io
import json
import os
import sys
import unittest
from pathlib import Path

from _support import REPO, FixtureCase

from provost_runtime import outward_gate as gate

HOOK = REPO / "hooks" / "outward-gate"


def human(text):
    return {"type": "user", "origin": {"kind": "human"}, "message": {"role": "user", "content": text}}


def queued(text):
    return {"type": "attachment", "attachment": {"type": "queued_command", "prompt": text,
                                                 "origin": {"kind": "human"}}}


def peer(text):
    return {"type": "user", "isMeta": True, "origin": {"kind": "peer", "name": "general-purpose"},
            "message": {"role": "user", "content": text}}


def notice(text):
    return {"type": "user", "origin": {"kind": "task-notification"}, "message": {"role": "user", "content": text}}


def summary(text):
    return {"type": "user", "isCompactSummary": True, "message": {"role": "user", "content": text}}


def tool_result(text):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": text}]}}


def assistant(text):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


class OutwardActionsTests(unittest.TestCase):
    """Which commands are outward actions (the user-approved list, 2026-11-15)."""

    def assertOutward(self, command, label=None, cwd=None):
        actions = gate.outward_actions(command, cwd)
        self.assertTrue(actions, f"{command!r} should be outward")
        if label:
            self.assertIn(label, " ".join(actions))

    def assertInward(self, command, cwd=None):
        self.assertEqual(gate.outward_actions(command, cwd), [], command)

    def test_listed_commands_are_outward(self):
        cases = {
            "gh pr merge 140 --squash": "gh pr merge",
            "gh pr merge": "gh pr merge",
            "gh release create v1.2.0 --notes x": "gh release create",
            "npm publish --access public": "npm publish",
            "git push --force origin feature": "force",
            "git push -f": "force",
            "git push -uf origin x": "force",
            "git push --force-with-lease=main:abc origin x": "force",
            "git push origin +feature": "force",
            "git push origin --delete old": "delete",
            "git push origin -d old": "delete",
            "git push origin :old": "delete",
            "git push --tags": "tags",
            "git push --follow-tags origin x": "tags",
            "git push origin tag v1.0": "tags",
            "git push origin refs/tags/v1.0": "tags",
            "git push origin main": "to main",
            "git push origin HEAD:master": "to master",
            "git push origin feature:refs/heads/main": "to main",
            "git push --all origin": "all branches",
            "git push --mirror backup": "all branches",
            "git branch -D old": "git branch -D",
            "git branch --delete --force old": "git branch -D",
            "git branch -df old": "git branch -D",
        }
        for command, label in cases.items():
            with self.subTest(command):
                self.assertOutward(command, label)

    def test_routine_commands_are_not(self):
        for command in ("git status", "git push origin feature", "git push -u origin worktree-x",
                        "git push origin HEAD:refs/heads/pr/12", "git branch -d merged", "git branch --show-current",
                        "git log --merges", "gh pr create --draft --title x", "gh pr view 12", "gh release list",
                        "gh pr checks 12", "npm test", "npm install publish-utils", "echo push merge release",
                        "grep -rn branch src", "git commit -m 'git push --force later'",
                        'git commit -m "a; gh pr merge 1" && git status', "git push --repo origin feature",
                        "git push -o ci.skip origin feature", ""):
            with self.subTest(command):
                self.assertInward(command)

    def test_compound_and_wrapped_commands(self):
        for command in ("cd repo && gh pr merge 3", "git status; git push -f", "ls | xargs git push --force",
                        "FOO=1 git push -f", "env A=1 B=2 git push -f", "timeout 30 git push -f",
                        "sudo -E npm publish", "git -C ../other push --force", "git -c x.y=z push -f",
                        "git --no-pager push -f", "bash -c 'git push -f'", "sh -c \"gh pr merge 2\"",
                        "pwsh -NoProfile -Command gh pr merge 2", 'powershell -c "git push -f"',
                        'eval "git push --force"', "echo $(git push -f)", 'echo "$(gh pr merge 1)"',
                        "echo `git push -f`", "C:/Program\\ Files/Git/bin/git.exe push -f",
                        "& gh.exe pr merge 5", "git push -f 2>&1 | tee log", "(cd x; npm publish)",
                        "git push 'unbalanced -f"):
            with self.subTest(command):
                self.assertOutward(command)

    def test_bare_push_uses_the_checked_out_branch(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / ".git").mkdir(parents=True)
            (repo / "sub").mkdir()
            head = repo / ".git" / "HEAD"
            head.write_text("ref: refs/heads/main\n", encoding="utf-8")
            self.assertOutward("git push", "to main", cwd=str(repo / "sub"))
            self.assertOutward("git push origin", "to main", cwd=str(repo))
            self.assertOutward("git push origin HEAD", "to main", cwd=str(repo))
            self.assertOutward("cd repo && git push", "to main", cwd=tmp)
            self.assertOutward("git -C repo push", "to main", cwd=tmp)
            head.write_text("ref: refs/heads/feature\n", encoding="utf-8")
            self.assertInward("git push", cwd=str(repo))
            self.assertInward("git push origin HEAD", cwd=str(repo))
            # A linked worktree: .git is a file naming the git dir.
            wt, gitdir = Path(tmp) / "wt", Path(tmp) / "gitdirs" / "wt"
            wt.mkdir()
            gitdir.mkdir(parents=True)
            (wt / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
            (gitdir / "HEAD").write_text("ref: refs/heads/master\n", encoding="utf-8")
            self.assertOutward("git push", "to master", cwd=str(wt))
            (gitdir / "HEAD").write_text("0123456789abcdef0123456789abcdef01234567\n", encoding="utf-8")
            self.assertInward("git push", cwd=str(wt))  # detached HEAD
            self.assertInward("git push", cwd=str(Path(tmp) / "missing"))
            self.assertInward("git push", cwd=None)

    # Review findings of 2026-11-16 (I1-I5, Nit 3): each was a command the first version let through.

    def test_newlines_end_commands(self):
        for command, label in (("git status\ngit push --force", "force"), ("cd repo\ngh pr merge 1", "gh pr merge"),
                               ("git status\r\nnpm publish", "npm publish"), ("git push \\\n  --force", "force"),
                               ("git push `\n  --force", "force")):
            with self.subTest(command):
                self.assertOutward(command, label)

    def test_command_words_anywhere_in_a_simple_command(self):
        for command, label in (("if gh pr checks 140; then gh pr merge 140 --squash; fi", "gh pr merge"),
                               ("for b in a b; do git push origin --delete $b; done", "delete"),
                               ("gh pr checks 1 && { gh pr merge 1; }", "gh pr merge"),
                               ("if ($ok) { gh pr merge 140 }", "gh pr merge"),
                               ("git branch | xargs -I {} git branch -D {}", "git branch -D"),
                               ("sudo -u me git push -f", "force"), ("nice -n 5 npm publish", "npm publish")):
            with self.subTest(command):
                self.assertOutward(command, label)

    def test_gh_flag_values_between_the_words(self):
        self.assertOutward("gh pr -R owner/repo merge 5", "gh pr merge")
        self.assertOutward("gh release --repo o/r create v1", "gh release create")
        self.assertInward("gh pr create --title merge")

    def test_substitutions_anywhere(self):
        # Review round 2, I2: glued to a word, inside a bash -c string, a backtick inside double quotes.
        for command, label in (("git push origin HEAD:$(git branch --show-current) --force", "force"),
                               ('bash -c "git push origin $(git branch --show-current) --force"', "force"),
                               ("git push origin feature:$(echo main)", "computed refspec"),
                               ("git push origin v$(cat VERSION)", "computed refspec"),
                               ('echo "`gh pr merge 5`"', "gh pr merge"), ('cat <<< "$(gh pr merge 5)"', "gh pr merge"),
                               ("echo $(echo $(npm publish))", "npm publish")):
            with self.subTest(command):
                self.assertOutward(command, label)

    def test_powershell_braces_without_spaces(self):
        for command, label in (("if ($ok) {gh pr merge 140}", "gh pr merge"),
                               ("if ($ok) { git push origin main}", "to main"),
                               ("1..2 | % {git push --force}", "force")):
            with self.subTest(command):
                self.assertTrue(" ".join(gate.outward_actions(command, powershell=True)).count(label), command)

    def test_heredoc_bodies_fed_to_cat_are_data(self):
        commit = "git commit -m \"$(cat <<'EOF'\nDocs: explain git push --force and gh pr merge\nEOF\n)\""
        self.assertInward(commit)
        self.assertInward("gh pr create --draft --body \"$(cat <<EOF\nrun npm publish later\nEOF\n)\"")
        self.assertOutward("bash <<'EOF'\ngit push --force\nEOF", "force")
        self.assertOutward(commit + " && git push -f", "force")

    def test_redirects_and_substitutions_keep_the_arguments(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            main_repo, feature = Path(tmp) / "mainrepo", Path(tmp) / "feat"
            for repo, branch in ((main_repo, "main"), (feature, "feature")):
                (repo / ".git").mkdir(parents=True)
                (repo / ".git" / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")
            self.assertOutward("git push 2>&1 | tail -3", "to main", cwd=str(main_repo))
            self.assertOutward("git push origin 2>/dev/null", "to main", cwd=str(main_repo))
            self.assertOutward("git push origin > log.txt", "to main", cwd=str(main_repo))
            self.assertOutward('git push origin "$(git branch --show-current)" --force', "force", cwd=str(feature))
            self.assertOutward("git push origin $(git branch --show-current) --force", "force", cwd=str(feature))
            # A computed destination may be anything, main included; the current-branch idioms read as HEAD.
            self.assertOutward("git push origin $(git config x.target)", "computed refspec", cwd=str(feature))
            for idiom in ("git branch --show-current", "git rev-parse --abbrev-ref HEAD", "git symbolic-ref --short HEAD"):
                with self.subTest(idiom):
                    self.assertInward(f"git push -u origin $({idiom})", cwd=str(feature))
                    self.assertOutward(f"git push -u origin $({idiom})", "to main", cwd=str(main_repo))
            self.assertInward("git push origin feature 2>&1 | tail -3", cwd=str(main_repo))
            self.assertOutward("git push origin *> $null", "to main", cwd=str(main_repo))

    def test_directories_named_by_the_command(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / ".git").mkdir(parents=True)
            (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            self.assertOutward(f'cd "{repo}"; git push', "to main", cwd=None)
            self.assertOutward(f'git -C "{repo}" push', "to main", cwd=None)
            self.assertEqual(gate.outward_actions(f"cd {repo}; git push", powershell=True), ["git push (to main)"])
            # A repository named by cd or -C whose branch cannot be read might be on main.
            self.assertOutward("cd missing && git push", "branch unknown", cwd=tmp)
            self.assertOutward("git -C missing push origin HEAD", "branch unknown", cwd=tmp)
            # Review round 2, I4: a directory the command does not spell out.
            (Path(tmp) / ".git").mkdir()
            (Path(tmp) / ".git" / "HEAD").write_text("ref: refs/heads/feature\n", encoding="utf-8")
            for command in ('cd "$MAIN" && git push', 'git -C "$(git rev-parse --git-common-dir)/.." push',
                            "cd - && git push", "cd $(dirname x) && git push"):
                with self.subTest(command):
                    self.assertOutward(command, "branch unknown", cwd=tmp)
            self.assertEqual(gate.outward_actions("cd $env:MAIN; git push", tmp, powershell=True),
                             ["git push (branch unknown)", "unreadable command (variable)"])
            self.assertEqual(gate.outward_actions(f"Push-Location {repo}; git push", tmp, powershell=True),
                             ["git push (to main)"])
            self.assertInward("git push", cwd=tmp)
            if os.name == "nt":
                bash_path = "/" + str(repo)[0].lower() + str(repo)[2:].replace("\\", "/")
                self.assertOutward(f"git -C {bash_path} push", "to main", cwd="C:\\")
                self.assertOutward(f"cd {repo}; git push", "git push", cwd="C:\\")
        self.assertOutward("C:\\tools\\gh.exe pr merge 5", "gh pr merge")
        self.assertEqual(gate.outward_actions("C:\\tools\\gh.exe pr merge 5", powershell=True), ["gh pr merge"])


class UnreadableCommandTests(unittest.TestCase):
    """Review round 3 (2026-11-16): a candidate command the parser cannot read with certainty asks."""

    def assertUnreadable(self, command, what, also=None, cwd=None, powershell=False):
        actions = gate.outward_actions(command, cwd, powershell=powershell)
        self.assertIn(f"unreadable command ({what})", actions, command)
        if also:
            self.assertIn(also, " ".join(actions), command)

    def feature_repo(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for repo, branch in ((root, "feature"), (root / "mainrepo", "main")):
            (repo / ".git").mkdir(parents=True)
            (repo / ".git" / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")
        return root

    def test_heredoc_terminators_it_cannot_find(self):
        # I1, verbatim: a delimiter the old regex cut short, and a terminator glued to ")".
        self.assertUnreadable("git commit -F - <<END-MSG\nx\nEND-MSG\ngit push --force", "heredoc", "force")
        self.assertUnreadable("git commit -m \"$(cat <<'EOF'\nx\nEOF)\"\ngit push --force", "heredoc", "force")
        self.assertUnreadable('git commit -F - "$(cat <<END-MSG\nx\nEND-MSG\n)"\ngit push --force', "heredoc", "force")
        for command in ("cat <<EOF\nx\ngit push --force", "cat <<EOF\nx\nEOF \ngit push --force",
                        "cat <<-EOF\nx\n  EOF\ngit push --force", "cat <<'EOF\"\nx\nEOF\ngit push --force",
                        "cat <<E'O'F\nx\nEOF\ngit push --force", "cat <<\nx\ngit push --force"):
            with self.subTest(command):
                self.assertUnreadable(command, "heredoc", "force")

    def test_heredocs_it_can_read(self):
        self.assertEqual(gate.outward_actions("cat <<-EOF\n\tgit push --force\n\tEOF"), [])
        self.assertEqual(gate.outward_actions('cat <<"EOF"\ngit push -f\nEOF\ngit status'), [])
        # Review round 4: only cat <<WORD is plain; the second heredoc of cat <<A <<B is not.
        self.assertEqual(gate.outward_actions("cat <<A <<B\ngit push -f\nA\necho\nB\ngit status"),
                         ["unreadable command (heredoc)"])
        self.assertEqual(gate.outward_actions("bash <<'EOF'\ngit push --force\nEOF"),
                         ["git push (force)", "unreadable command (heredoc)", "unreadable command (shell input)"])
        self.assertEqual(gate.outward_actions("cat <<< 'git push -f' && git status"),
                         ["unreadable command (here-string)"])

    def test_powershell_braced_variables(self):
        # I2, verbatim: the brace padding split ${...} and lost the command.
        self.assertUnreadable("git -C ${repo} push --force", "${...}", "force", powershell=True)
        self.assertUnreadable("cd ${env:MAIN}; git push", "${...}", "branch unknown", powershell=True)
        self.assertUnreadable('pwsh -Command "git -C ${r} push -f"', "${...}", "force")
        self.assertEqual(gate.outward_actions("git push origin ${x}"),
                         ["git push (computed refspec)", "unreadable command (variable)"])

    def test_nested_and_unclosed_substitutions(self):
        self.assertUnreadable("echo $(echo $(npm publish))", "nested substitution", "npm publish")
        self.assertUnreadable("echo $(echo `git push -f`)", "nested substitution", "force")
        self.assertUnreadable("echo `echo $(git push origin feature)`", "nested substitution")
        self.assertUnreadable("echo $(git push origin feature", "unclosed substitution")
        self.assertUnreadable("echo `git push origin feature", "unclosed substitution")
        self.assertEqual(gate.outward_actions("echo $(ls"), [])  # no keyword: nothing to ask about

    def test_too_deep(self):
        self.assertUnreadable("eval eval eval eval eval eval git push origin feature", "too deep")
        self.assertUnreadable("echo $(echo $(echo $(echo $(echo $(echo $(git push origin feature))))))",
                              "too deep")

    def test_bounded_work(self):
        # Coordinator follow-up: a hook timeout would let the command run, so no command may take long.
        import time
        for command in ("eval " * 30 + "git push -f", "a;" * 5000 + "git push -f",
                        "bash -c " + "sh -c " * 20 + "x " * 3000 + "git push -f",
                        "a;" * 400000 + "git push -f", "$(" * 2000 + "git push -f" + ")" * 2000,
                        "x <<A " * 50000 + "\ngit push --force", "x <<A " * 8000 + "\ngit push --force"):
            with self.subTest(command[:40], length=len(command)):
                start = time.perf_counter()
                actions = gate.outward_actions(command)
                self.assertLess(time.perf_counter() - start, 1.0)
                self.assertTrue(any(a.startswith("unreadable command") or "force" in a for a in actions), actions)
        self.assertIn("unreadable command (too long)", gate.outward_actions("a;" * 400000 + "git push -f"))

    def test_powershell_backtick_escapes(self):
        self.assertUnreadable("g`it push -f", "escape", powershell=True)
        self.assertUnreadable("git push origin ma`in", "escape", powershell=True)
        self.assertEqual(gate.outward_actions("git push `\n  origin feature", powershell=True), [])

    def test_unbalanced_quotes(self):
        self.assertUnreadable("git push 'unbalanced -f", "quotes", "force")

    def test_push_with_a_computed_remote_or_refspec(self):
        root = str(self.feature_repo())
        for command in ("git push origin $BRANCH", "git push $REMOTE", "git push $(echo origin main)",
                        "git push origin %B%", "git push origin `echo main`", "git push origin feat${x}"):
            with self.subTest(command):
                self.assertIn("computed refspec", " ".join(gate.outward_actions(command, root)))
        self.assertIn("computed refspec", " ".join(gate.outward_actions("git push origin $env:B", root, powershell=True)))
        self.assertEqual(gate.outward_actions("git push -u origin $(git branch --show-current)", root), [])

    def test_directories_after_an_unresolved_one(self):
        root = self.feature_repo()
        posix = root.as_posix()  # forward slashes: a Bash backslash is also read as an escape
        for command in ('cd "$MAIN"; cd sub; git push', 'cd "$MAIN" && cd sub && git push origin HEAD',
                        'git -C "$X" -C sub push', f"pushd {posix}/feat; popd; git push",
                        "cd $(dirname x)/sub && git push"):
            with self.subTest(command):
                self.assertIn("git push (branch unknown)", gate.outward_actions(command, str(root)))
        self.assertIn("git push (branch unknown)",
                      gate.outward_actions("Push-Location x; Pop-Location; git push", str(root), powershell=True))
        self.assertEqual(gate.outward_actions(f'cd -; cd {posix}; git push', str(root)), [])
        self.assertEqual(gate.outward_actions(f'cd "$MAIN"; cd {posix}/mainrepo; git push', str(root)),
                         ["git push (to main)", "unreadable command (variable)"])

    def test_routine_commands_still_pass(self):
        root = str(self.feature_repo())
        commit = "git commit -m \"$(cat <<'EOF'\nDocs: explain git push --force and gh pr merge\nEOF\n)\""
        for command in ("git status", "git log --oneline -5", "git push", "git push -u origin HEAD",
                        "git push -u origin feature-x", "gh pr create --title merge", commit,
                        commit + " && git push -u origin HEAD", "gh pr view 141", "npm test",
                        "git push -u origin $(git branch --show-current)"):
            with self.subTest(command):
                self.assertEqual(gate.outward_actions(command, root), [])
                self.assertEqual(gate.outward_actions(command, root, powershell=True), [])


class PlainCommandTests(unittest.TestCase):
    """Review round 4 (2026-11-16): a candidate command passes silently only when it is plain."""

    def setUp(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for name, branch in (("main", "main"), ("feat", "feature")):
            (self.root / name / ".git").mkdir(parents=True)
            (self.root / name / ".git" / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")
        self.main, self.feat = str(self.root / "main"), str(self.root / "feat")

    def assertUnreadable(self, command, what, also=None, cwd=None, powershell=False):
        actions = gate.outward_actions(command, cwd, powershell=powershell)
        self.assertIn(f"unreadable command ({what})", actions, command)
        if also:
            self.assertIn(also, " ".join(actions), command)

    def test_comments(self):
        # I1a: the apostrophes pair up across the lines, so only the label shows; it asks either way.
        self.assertUnreadable("# don't force\ngit push --force\n# that's it", "comment")
        self.assertUnreadable("gh pr merge 141 # squash", "comment", "gh pr merge")
        self.assertUnreadable("git status;# x\ngit push origin feature", "comment")
        self.assertUnreadable("# x\ngit push origin feature", "comment", powershell=True)
        self.assertUnreadable("<# x #> git push origin feature", "comment", powershell=True)
        self.assertEqual(gate.outward_actions('git commit -m "fix #12" && git push', self.feat), [])
        self.assertEqual(gate.outward_actions("git commit -m 'fix #12' && git push a#b", self.feat), [])

    def test_heredocs_outside_the_standard_form(self):
        self.assertUnreadable("cat > a.txt <<'EOF'\nDon't edit\nEOF\ngit push --force\n# it's done",
                              "heredoc")  # I1b
        self.assertUnreadable("git commit -F - <<'EOF'\nx\nEOF\ngit push origin feature", "heredoc")
        self.assertUnreadable("cat <<< 'x' && git push origin feature", "here-string")
        self.assertUnreadable("@'\nx\n'@ | Set-Content a; git push origin feature", "here-string", powershell=True)
        self.assertUnreadable('$m = @"\nx\n"@; git push origin feature', "here-string", powershell=True)

    def test_substitutions(self):
        self.assertUnreadable('echo "$(echo "(")" ; git push -f ; echo ")"', "substitution")  # I1c
        self.assertUnreadable("X=$(cd ../feat && pwd); git push", "substitution", cwd=self.main)  # I2b
        self.assertUnreadable('GH=$(command -v gh); "$GH" pr merge 141 --squash', "substitution")  # I3b
        self.assertUnreadable("echo `date` && git push origin feature", "substitution")
        self.assertUnreadable("git push origin $(git config x.target)", "substitution", "computed refspec")
        # The plain forms: the current-branch idioms and a cat heredoc, and an escaped backtick (a literal).
        for command in ("git push -u origin $(git branch --show-current)",
                        'git push -u origin "$(git rev-parse --abbrev-ref HEAD)"',
                        "git commit -m \"Fix \\`push\\` docs\" && git push"):
            with self.subTest(command):
                self.assertEqual(gate.outward_actions(command, self.feat), [])

    def test_subshells(self):
        self.assertUnreadable("(cd ../feat && git status); git push", "subshell", cwd=self.main)  # I2a
        self.assertUnreadable("(git push origin feature)", "subshell")
        self.assertUnreadable("if ($x) { Set-Location ../feat }; git push", "subshell", cwd=self.main,
                              powershell=True)
        self.assertUnreadable("& { cd ../feat }; git push", "subshell", cwd=self.main, powershell=True)
        self.assertEqual(gate.outward_actions("Get-Date; git push", self.feat, powershell=True), [])
        self.assertNotIn("unreadable command (subshell)",
                         gate.outward_actions("if ($ok) { gh pr merge 1 }", powershell=True))
        self.assertEqual(gate.outward_actions('git commit -m "a (b)" && git push', self.feat), [])

    def test_variables_and_splats(self):
        self.assertUnreadable("$gh = 'C:\\Program Files\\GitHub CLI\\gh.exe'; & $gh pr merge 141 --squash",
                              "variable", powershell=True)  # I3a
        self.assertUnreadable("git push @a", "variable", cwd=self.feat, powershell=True)  # I3c
        self.assertUnreadable("& gh pr merge 141", "variable", "gh pr merge", powershell=True)
        self.assertUnreadable("git push origin $R", "variable", "computed refspec", cwd=self.feat)
        self.assertUnreadable('git push origin "$R"', "variable", cwd=self.feat)
        self.assertEqual(gate.outward_actions("git commit -m 'costs $5' && git push", self.feat), [])
        self.assertEqual(gate.outward_actions("git push 2>&1 | Select-Object -Last 3", self.feat, powershell=True), [])
        self.assertEqual(gate.outward_actions("git push origin {feature,main}"),
                         ["unreadable command (brace expansion)"])
        for command in ("git push origin mai?", "git push origin 'refs/heads/*'", "git push or?gin main", "git push origin [m]ain"):
            with self.subTest(command):
                self.assertIn("computed refspec", " ".join(gate.outward_actions(command, self.feat)))

    def test_continuations(self):
        self.assertUnreadable("git push origin ma\\\nin", "continuation", "to main", cwd=self.feat)  # I5a
        self.assertUnreadable("git push --for\\\nce origin feature", "continuation", "force", cwd=self.feat)  # I5b
        self.assertUnreadable("git push origin ma`\nin", "continuation", cwd=self.feat, powershell=True)
        self.assertEqual(gate.outward_actions("git push \\\n  origin feature", self.feat), [])
        self.assertEqual(gate.outward_actions("git push `\n  origin feature", self.feat, powershell=True), [])

    def test_shell_input(self):
        for command, powershell in (('echo "git push --force" | bash', False), ("bash <<< 'git push'", False),
                                    ("bash deploy.sh && git push origin feature", False),
                                    ("'git push -f' | iex", True), ("pwsh -ec abc; git push origin feature", True),
                                    ("cmd < x.txt & git push origin feature", False)):
            with self.subTest(command):
                self.assertUnreadable(command, "shell input", powershell=powershell)
        for command in ("bash -c 'git status' && git push", "bash -lc 'git status' && git push",
                        'pwsh -NoProfile -Command "git status"; git push', "eval 'git status'; git push"):
            with self.subTest(command):
                self.assertEqual(gate.outward_actions(command, self.feat), [])

    def test_abbreviated_long_options(self):
        for command, label in (("git push --tag", "tags"), ("git push origin --del old", "delete"),
                               ("git push --mirr backup", "all branches"), ("git push --al origin", "all branches"),
                               ("git push --force-with-l origin x", "force"), ("git push --forc origin x", "force"),
                               ("git push --follow origin x", "tags"), ("git push --bra origin", "all branches"),
                               ("git branch --del --force x", "git branch -D"), ("git branch --d --forc x", "git branch -D")):
            with self.subTest(command):
                self.assertIn(label, " ".join(gate.outward_actions(command, self.feat)))
        for command in ("git push --dry-run origin feature", "git push --set-upstream origin feature",
                        "git branch --format=%(refname) --delete x", "git push --no-verify origin feature"):
            with self.subTest(command):
                actions = gate.outward_actions(command, self.feat)
                self.assertEqual([a for a in actions if a.startswith("git ")], [], actions)
        self.assertEqual(gate.outward_actions("git push --dry-run origin feature", self.feat), [])

    def test_routine_flow_stays_silent(self):
        commit = "git commit -m \"$(cat <<'EOF'\nDocs: explain git push --force and gh pr merge\n\nCo-Authored-By: x\nEOF\n)\""
        body = "gh pr create --title \"x\" --body \"$(cat <<'EOF'\n## Summary\n- merge #12 first\nEOF\n)\""
        for command in ("git status", "git push", "git push -u origin HEAD", "git push -u origin feature-x",
                        "git push -u origin $(git branch --show-current)",
                        'git push -u origin "$(git branch --show-current)"', commit, commit + " && git push", body,
                        "gh pr create --title merge", "gh pr view 141", "git branch --show-current",
                        "git log --oneline main..HEAD"):
            with self.subTest(command):
                self.assertEqual(gate.outward_actions(command, self.feat), [])
        for command in ("git status", "git push", "git push -u origin HEAD", "gh pr view 141",
                        "git branch --show-current", "git push 2>&1 | Out-Null"):
            with self.subTest(command, powershell=True):
                self.assertEqual(gate.outward_actions(command, self.feat, powershell=True), [])


class AuthorizesTests(unittest.TestCase):
    """Review round 3 (2026-11-16): only a whole message shaped as a go-ahead counts."""

    def test_whole_message_shape(self):
        for text in ("授權", "授權。", "好，授權", "好 授權", "OK 授權", "我授權", "好，我授權", "授權 merge #141",
                     "授權 merge PR #141", "授權合併並安裝", "授權 release v1.2.0", "授權 merge #140 並安裝",
                     "  授權！ ", "ok, 授權 push", "授權 merge 和 install", "授權!"):
            with self.subTest(text):
                self.assertTrue(gate.authorizes(text))
        for text in ("授權取消", "授權撤回，先不要 merge", "授權等 CI 過了再說", "授權是什麼意思", "授權了嗎？",
                     "授權?", "不授權", "取消授權", "先不要 merge，等我授權", "等我授權", "之後再授權",
                     "OAuth 授權流程壞了", "「授權」是什麼", "授權 don't merge", "授權 merge 嗎", "授權\nmerge",
                     "我授權你 merge #140", "中文", "", None, 3, "好好好授權", "授權 merged",
                     "授權 1" + "1" * 300 + "x"):
            with self.subTest(text):
                self.assertFalse(gate.authorizes(text))

    def test_words(self):
        for text in ("授權", "授權 merge", "好，授權", "授權。"):
            with self.subTest(text):
                self.assertTrue(gate.authorizes(text))
        # A 授權 inside a longer sentence no longer counts (review round 3).
        self.assertFalse(gate.authorizes("我授權你 merge #140"))
        for text in ("", "好", "merge 吧", "不授權", "先不要授權", "沒有授權", "未經授權", "別授權",
                     "要授權嗎", "需要授權?", "授權 merge，但不授權 release", None, "授", "授 權",
                     # review finding I2, 2026-11-16
                     "授權了嗎？", "取消授權", "不可以授權", "我沒有要授權", "還不能授權",
                     "你剛剛為什麼沒等我說「授權」就 merge？", "是否授權 release", "授權呢", "撤回授權",
                     "「授權」是什麼意思", '他說 "授權"',
                     # review round 2, I1: deferrals, conditions and nouns
                     "先不要 merge，等我授權", "等我說授權再 merge", "之後我會授權，先不要動", "授權之前先跑測試",
                     "OAuth 授權流程壞了", "merge 前要等我授權", "看一下授權條款", "授權後再 release"):
            with self.subTest(text):
                self.assertFalse(gate.authorizes(text))

    def test_a_quoted_mention_beside_a_real_word(self):
        # Review round 3: text around the go-ahead makes the whole message not count.
        self.assertFalse(gate.authorizes("我知道「授權」的規則了，授權"))
        self.assertFalse(gate.authorizes("授權 merge。下一題"))
        for text in ("OK 授權", "好的，授權 merge 和 install", "可以授權", "我授權", "那就授權"):
            with self.subTest(text):
                self.assertTrue(gate.authorizes(text))
        for text in ("那就授權吧", "授權給你 merge"):
            with self.subTest(text):
                self.assertFalse(gate.authorizes(text))


class TranscriptTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "t.jsonl"

    def write(self, *entries, raw_tail=""):
        self.path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries) + raw_tail,
                             encoding="utf-8")
        return str(self.path)

    def test_newest_human_entry_wins(self):
        path = self.write(human("授權"), assistant("ok"), tool_result("done"), human("下一題"))
        self.assertEqual(gate.last_human_text(path), "下一題")
        path = self.write(human("先做"), assistant("ok"), queued("授權 merge"), tool_result("x"))
        self.assertEqual(gate.last_human_text(path), "授權 merge")

    def test_other_origins_never_count(self):
        path = self.write(human("先不要動"), peer("授權"), notice("授權"), summary("授權"), tool_result("授權"),
                          assistant("授權"))
        self.assertEqual(gate.last_human_text(path), "先不要動")
        self.assertIsNone(gate.last_human_text(self.write(peer("授權"), summary("授權"))))
        # Marked human but from a sidechain or a compact summary: not a message of the person's.
        sidechain = {**human("授權"), "isSidechain": True}
        compacted = {**human("授權"), "isCompactSummary": True}
        self.assertEqual(gate.last_human_text(self.write(human("等等"), sidechain, compacted)), "等等")

    def test_list_content_with_text_and_image(self):
        entry = human([{"type": "image", "source": {}}, {"type": "text", "text": "授權"}])
        self.assertEqual(gate.last_human_text(self.write(entry)), "授權")

    def test_lines_across_chunks_and_a_broken_tail(self):
        big = tool_result("x" * (3 * gate.CHUNK))
        path = self.write(human("授權"), big, assistant("y" * 1000), raw_tail='{"type": "user", "origin": {"kind"')
        self.assertEqual(gate.last_human_text(path), "授權")

    def test_limit(self):
        path = self.write(human("授權"), tool_result("x" * (2 * gate.CHUNK)))
        self.assertIsNone(gate.last_human_text(path, limit=gate.CHUNK))


class DecideTests(TranscriptTests):
    def event(self, command, transcript, **extra):
        return {"session_id": "s", "transcript_path": transcript, "cwd": str(self.path.parent),
                "hook_event_name": "PreToolUse", "tool_name": extra.pop("tool", "Bash"),
                "tool_input": {"command": command, "description": "d"}, "tool_use_id": "u", **extra}

    def assertAsk(self, result, *words):
        output = result["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "ask")
        self.assertEqual(output["hookEventName"], "PreToolUse")
        for word in words:
            self.assertIn(word, output["permissionDecisionReason"])

    def test_asks_without_authorization(self):
        path = self.write(human("merge 吧"))
        self.assertAsk(gate.decide(self.event("gh pr merge 1", path)), "gh pr merge", "授權")
        # Review round 4: the reason names the go-ahead shape, not just the word.
        self.assertAsk(gate.decide(self.event("gh pr merge 1", self.write(human("授權取消")))),
                       "is not a go-ahead such as 授權 or 授權 merge #141")
        self.assertAsk(gate.decide(self.event("gh pr merge 1", path, tool="PowerShell")), "gh pr merge")

    def test_authorized_message_gives_no_decision(self):
        path = self.write(human("授權"), assistant("merging"))
        self.assertEqual(gate.decide(self.event("gh pr merge 1 --squash", path)), {})
        self.assertEqual(gate.decide(self.event("gh pr merge 1", path, tool="PowerShell")), {})

    def test_unreadable_commands_ask_unless_authorized(self):
        command = "git commit -m \"$(cat <<'EOF'\nx\nEOF)\"\ngit push --force"
        self.assertAsk(gate.decide(self.event(command, self.write(human("授權取消")))), "unreadable command (heredoc)")
        self.assertEqual(gate.decide(self.event(command, self.write(human("授權")))), {})

    def test_subagents_always_ask(self):
        path = self.write(human("授權"))
        self.assertAsk(gate.decide(self.event("git push -f", path, agent_id="a1", agent_type="implementer")),
                       "subagent")

    def test_unreadable_transcript_asks(self):
        missing = str(self.path.parent / "missing.jsonl")
        self.assertAsk(gate.decide(self.event("npm publish", missing)), "transcript")
        self.assertAsk(gate.decide(self.event("npm publish", None)), "transcript")
        self.assertAsk(gate.decide(self.event("npm publish", self.write(peer("授權")))), "transcript")

    def test_other_events_and_commands_give_no_decision(self):
        path = self.write(human("hi"))
        self.assertEqual(gate.decide(self.event("git status", path)), {})
        self.assertEqual(gate.decide({**self.event("gh pr merge 1", path), "tool_name": "Write"}), {})
        self.assertEqual(gate.decide({**self.event("gh pr merge 1", path), "hook_event_name": "PostToolUse"}), {})
        self.assertEqual(gate.decide({**self.event("gh pr merge 1", path), "tool_input": {"command": 3}}), {})

    def run_main(self, raw):
        out = io.StringIO()
        self.assertEqual(gate.main(io.StringIO(raw), out), 0)
        return out.getvalue()

    def test_main_prints_nothing_without_a_decision(self):
        path = self.write(human("hi"))
        self.assertEqual(self.run_main(json.dumps(self.event("git status", path))), "")
        self.assertEqual(self.run_main("not json"), "")
        self.assertEqual(self.run_main("[]"), "")

    def test_main_asks_when_the_check_fails_on_a_candidate(self):
        path = self.write(human("hi"))
        printed = self.run_main(json.dumps(self.event("gh pr merge 1", path)))
        self.assertAsk(json.loads(printed), "gh pr merge")
        original = gate.decide
        try:
            gate.decide = lambda event: 1 / 0
            self.assertAsk(json.loads(self.run_main(json.dumps(self.event("gh pr merge 1", path)))), "unchecked")
            self.assertEqual(self.run_main(json.dumps(self.event("ls", path))), "")
        finally:
            gate.decide = original
        self.assertAsk(json.loads(self.run_main('{"command": "git push -f' + "x" * gate.MAX_INPUT)), "unchecked")


class OutwardGateHookTests(FixtureCase):
    """hooks/outward-gate: bash fast path, then the real module."""

    def setUp(self):
        super().setUp()
        self.log = self.root / "python.log"
        self.bin = self.root / "bin"
        self.transcript = self.root / "t.jsonl"
        self.transcript.write_text(json.dumps(human("merge 吧"), ensure_ascii=False) + "\n", encoding="utf-8")

    def event(self, command, **extra):
        return json.dumps({"session_id": "s1", "transcript_path": str(self.transcript), "cwd": str(self.root),
                           "hook_event_name": "PreToolUse", "tool_name": "Bash",
                           "tool_input": {"command": command, "description": "run"}, **extra}, ensure_ascii=False)

    def run_hook(self, stdin, env=None, fake=True):
        self.log.unlink(missing_ok=True)
        dirs = []
        if fake:
            self.fake_interpreter(self.bin, "python3", self.log)
            dirs = [self.bin]
        result = self.run_bash(HOOK, "push", path_dirs=dirs, stdin=stdin, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_commands_without_a_keyword_start_no_interpreter(self):
        for command in ("git status", "ls -la", "npm test", "gh pr view 3"):
            with self.subTest(command):
                result = self.run_hook(self.event(command))
                self.assertEqual(result.stdout, "")
                self.assertFalse(self.log.exists(), "no keyword: no interpreter may start")

    def test_keywords_in_any_case_reach_python(self):
        for command in ("gh pr MERGE 1", "git Push -f", "npm publish", "git branch -D x", "gh release create v1"):
            with self.subTest(command):
                self.run_hook(self.event(command))
                self.assertIn("-m provost_runtime.outward_gate", self.log.read_text(encoding="utf-8"))

    def test_unreadable_command_field_reaches_python(self):
        self.run_hook('{"tool_input": {"command": 12}, "x": "push"}')
        self.assertIn("-m provost_runtime.outward_gate", self.log.read_text(encoding="utf-8"))

    def test_missing_python_asks_for_a_candidate(self):
        # No interpreter on PATH (and none cached under the fixture's PROVOST_HOME): bash answers itself.
        result = self.run_bash(HOOK, stdin=self.event("gh pr merge 1"), env={"PATH": str(self.root / "no-bin")})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_real_module_end_to_end(self):
        env = {"PROVOST_PYTHON": sys.executable}
        result = self.run_hook(self.event("cd x && gh pr merge 140 --squash"), env=env, fake=False)
        self.assertEqual(json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"], "ask")
        self.transcript.write_text(json.dumps(human("授權"), ensure_ascii=False) + "\n", encoding="utf-8")
        self.assertEqual(self.run_hook(self.event("gh pr merge 140"), env=env, fake=False).stdout, "")
        result = self.run_hook(self.event("gh pr merge 140", agent_id="a1"), env=env, fake=False)
        self.assertEqual(json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertEqual(self.run_hook(self.event("git push origin feature"), env=env, fake=False).stdout, "")


if __name__ == "__main__":
    unittest.main()
