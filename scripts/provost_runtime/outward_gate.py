"""Outward-action gate: the PreToolUse (Bash, PowerShell) check behind hooks/outward-gate.

python -m provost_runtime.outward_gate reads one PreToolUse event on stdin. When the command would
merge a pull request, publish a release or package, or force, delete, tag or main/master-push with git,
it prints a permissionDecision "ask" unless the person's most recent own message says 授權; otherwise it
prints nothing (no decision: the normal permission flow, auto mode's classifier included, decides).

The person's messages are the transcript entries Claude Code marks origin.kind "human": typed prompts
(type "user") and messages sent mid-turn (a "queued_command" attachment). Subagent hand-backs ("peer"),
background notices ("task-notification"), compact summaries and slash-command output carry another or no
origin and never authorize. A call from a subagent (agent_id) always asks.

The command check errs toward asking: git, gh and npm are recognised anywhere in a simple command
(after if/then/do, braces, sudo -u, xargs -I ...), so a command that only mentions them as plain words
(echo gh pr merge) asks too. Every failure on a candidate command asks; nothing here ever denies.
Stdlib only, no store access.
"""
import json
import os
import re
import shlex
import sys

WORD = "授權"
MAX_INPUT = 1024 * 1024
CHUNK = 1024 * 1024
TAIL_LIMIT = 64 * 1024 * 1024  # transcript bytes read back from the end before giving up (then: ask)
MAX_DEPTH = 4                  # nested bash -c / $(...) levels followed
# Lower-case substrings every outward command contains; hooks/outward-gate uses the same list for its
# fast path and install.py for the handlers' "if" rules.
KEYWORDS = ("push", "merge", "release", "publish", "branch")
PROTECTED = ("main", "master")
# A 授權 counts only in the shape of a go-ahead: it opens its clause (the text between punctuation marks),
# after nothing or a short affirmation (LEADS), and is not followed by a word that makes it a noun or a
# time (FOLLOWERS: 授權條款, 授權之前). Any 授權 with a negation before it or a question in its clause makes
# the whole message not count; a 授權 in quotes is a mention and neither counts nor refuses.
LEADS = re.compile(r"(?:好|好的|ok|okay|yes|可以|同意|我|那就|那|就|嗯|對|是|現在|直接)*")
FOLLOWERS = ("前", "後", "之前", "之後", "以前", "以後", "時", "的", "條款", "流程", "碼", "書", "機制", "規則",
             "問題", "範圍", "方式", "設定", "檔")
NEGATIONS = ("不", "未", "沒", "別", "無", "非", "勿", "免", "取消", "撤銷", "撤回", "收回", "拒絕", "停止", "暫緩")
QUESTIONS = ("?", "？", "嗎", "呢", "為什麼", "是否", "要不要", "能不能", "可不可以", "可以嗎")
CLAUSE_END = "，,。.！!？?；;：:\n、"
QUOTES_OPEN, QUOTES_CLOSE = "「『\"“'‘", "」』\"”'’"

SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "pwsh", "powershell", "cmd", "eval", "iex", "invoke-expression"}
CHDIR = {"cd", "pushd", "set-location", "sl", "chdir", "push-location"}
# A substitution with one of these texts names the checked-out branch: it is read as HEAD.
CURRENT_BRANCH = ("git branch --show-current", "git rev-parse --abbrev-ref head", "git symbolic-ref --short head")
GIT_VALUE_OPTIONS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix",
                     "--config-env", "--attr-source", "--list-cmds"}
PUSH_VALUE_OPTIONS = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}
GH_PAIRS = {("pr", "merge"): "gh pr merge", ("release", "create"): "gh release create"}
PLACEHOLDER = "\0sub"  # stands in for a $(...) argument, so the arguments after it are kept
_OPERATOR = set("();<>|&")


def outward_actions(command, cwd=None, powershell=False):
    """The outward actions command would take, as short labels in order of appearance, without repeats.
    powershell: backslashes are path separators, not escapes."""
    found = []
    # A backslash in a bash command is an escape, but a Windows path (C:\tools\gh.exe) typed into the Bash
    # tool means a separator: read such a command both ways and report what either reading finds.
    readings = (powershell,) if powershell or "\\" not in str(command) else (False, True)
    for reading in readings:
        state = {"cwd": cwd, "named": False, "powershell": reading, "depth": 0}
        for tokens in _segments(command, 0, reading):
            for label in _classify(tokens, state):
                if label not in found:
                    found.append(label)
    return found


def _lex(command, powershell):
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        if powershell:
            lexer.escape = ""
        return list(lexer)
    except ValueError:  # unbalanced quotes: a plain split, which may find more commands, never fewer
        return re.findall(r"[();<>|&]+|[^\s();<>|&]+", command.replace('"', " ").replace("'", " "))


def _segments(command, depth, powershell=False):
    """Simple commands of command (token lists): split on shell operators and newlines, with every $(...)
    and (outside PowerShell) backtick substitution read as a command of its own wherever it stands, in
    quotes or glued to a word, and left in place as one placeholder word."""
    if depth > MAX_DEPTH or not isinstance(command, str) or not command.strip():
        return
    command = _drop_cat_heredocs(command.replace("\r\n", "\n").replace("\r", "\n"))
    # Line continuations (bash \, PowerShell `) join lines; any other newline ends a command.
    command = re.sub(r"[\\`]\n", " ", command).replace("\n", " ; ")
    inner = []
    command = _substitute(command, powershell, inner)
    for text in inner:
        yield from _segments(text, depth + 1, powershell)
    if powershell:
        # Script-block braces need no spaces in PowerShell: {gh pr merge 5}, % {git push -f}.
        command = re.sub(r"[{}]", r" \g<0> ", command)
    tokens = _lex(command, powershell)
    current, i = [], 0
    while i < len(tokens):
        token = tokens[i]
        if token and set(token) <= _OPERATOR:
            if ("<" in token or ">" in token) and set(token) <= set("<>&"):
                # A redirect: its target (a file or a descriptor) is not an argument, and neither is the
                # stream written before it (2>&1, PowerShell *> $null).
                if current and (current[-1].isdigit() or current[-1] == "*"):
                    current.pop()
                i += 2
                continue
            if current:
                yield current
            current = []
            i += 1
            continue
        if token != "$":
            current.append(token)
        i += 1
    if current:
        yield current


def _substitute(command, powershell, inner):
    """command with each $(...) (and, outside PowerShell, each `...`) replaced by a placeholder word, or by
    HEAD when its text names the checked-out branch; the texts are appended to inner. An unclosed one runs
    to the end. Single quotes are not honoured: a quoted '$(x)' is read as a command too (asks more)."""
    out, i, n = [], 0, len(command)
    while i < n:
        if command.startswith("$(", i):
            level, j = 0, i + 1
            while j < n:
                level += {"(": 1, ")": -1}.get(command[j], 0)
                if level == 0:
                    break
                j += 1
            text = command[i + 2:j]
        elif command[i] == "`" and not powershell:
            j = command.find("`", i + 1)
            j = n if j < 0 else j
            text = command[i + 1:j]
        else:
            out.append(command[i])
            i += 1
            continue
        inner.append(text)
        out.append("HEAD" if " ".join(text.split()).lower() in CURRENT_BRANCH else PLACEHOLDER)
        i = j + 1
    return "".join(out)


def _drop_cat_heredocs(command):
    """command without the bodies of heredocs fed to cat (commit messages, PR bodies): they are data. A
    heredoc fed to anything else (bash <<EOF) keeps its body, which is then read as commands."""
    out, end = [], None
    for line in command.split("\n"):
        if end is not None:
            if line.strip() == end:
                end = None
            continue
        out.append(line)
        match = re.search(r"\bcat\s+<<-?\s*(['\"]?)([A-Za-z_]\w*)\1", line)
        if match:
            end = match.group(2)
    return "\n".join(out)


def _name(token):
    base = re.split(r"[\\/]", token)[-1].lower()
    for suffix in (".exe", ".cmd", ".bat", ".ps1"):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


def _join(base, target):
    """target (from cd or git -C) made absolute against base when possible; None when the command does not
    say which directory it is (a variable, a substitution, cd -)."""
    if target == "-" or PLACEHOLDER in target or any(mark in target for mark in "$%`"):
        return None
    target = os.path.expanduser(target)
    match = re.fullmatch(r"/([A-Za-z])(/.*)?", target.replace("\\", "/"))
    if os.name == "nt" and match:  # Git Bash spelling of a drive path: /d/x is D:/x
        target = f"{match.group(1).upper()}:{match.group(2) or '/'}"
    if base and not os.path.isabs(target):
        return os.path.join(base, target)
    return target


def _classify(tokens, state):
    """The labels of the outward actions in one simple command. Command words are looked for at every
    position, not only the first, so keywords (if, then, do), braces and wrappers with options
    (sudo -u me, xargs -I {}) cannot hide them."""
    found = []
    for i, token in enumerate(tokens):
        name, args = _name(token), tokens[i + 1:]
        if name in CHDIR and i == next((k for k, t in enumerate(tokens) if _name(t) in CHDIR), -1):
            target = next((a for a in args if a == "-" or not a.startswith("-")), None)
            if target:
                state["cwd"] = _join(state.get("cwd"), target)
                state["named"] = True
        elif name == "git":
            found.append(_git(args, state))
        elif name == "gh":
            found.append(_gh(args))
        elif name == "npm" and "publish" in [a.lower() for a in args if not a.startswith("-")]:
            found.append("npm publish")
        elif name in SHELLS:
            # bash -c "git push -f", pwsh -NoProfile -Command git push -f, eval "...": options and their
            # values come first, so the command text is read from every starting argument.
            depth = state.get("depth", 0) + 1
            if depth > MAX_DEPTH:
                continue
            inner_state = {**state, "depth": depth}
            for start in range(min(len(args), 64)):
                for inner in _segments(" ".join(a for a in args[start:] if a != PLACEHOLDER), depth,
                                       state.get("powershell", False)):
                    found += _classify(inner, inner_state)
    return [label for label in dict.fromkeys(found) if label]


def _gh(args):
    lowered = [a.lower() for a in args]
    for (first, second), label in GH_PAIRS.items():
        for k, word in enumerate(lowered):
            if word == first and _gh_sub(lowered[k + 1:]) == second:
                return label
    return None


def _gh_sub(args):
    """The subcommand word at the start of args, skipping flags and their values (gh pr -R o/r merge).
    After a flag the next word could be its value or the subcommand: a protected word there counts."""
    i = 0
    while i < len(args):
        arg = args[i]
        if not arg.startswith("-"):
            return arg
        if "=" not in arg and i + 1 < len(args) and args[i + 1] in {second for _, second in GH_PAIRS}:
            return args[i + 1]
        i += 1 if "=" in arg else 2
    return None


def _git(args, state):
    directory, named = state.get("cwd"), state.get("named", False)
    i = 0
    while i < len(args) and args[i].startswith("-"):
        option = args[i]
        if option in GIT_VALUE_OPTIONS and i + 1 < len(args):
            if option == "-C":
                directory, named = _join(directory, args[i + 1]), True
            i += 2
        else:
            i += 1
    if i >= len(args):
        return None
    sub, rest = args[i].lower(), args[i + 1:]
    if sub == "push":
        return _push(rest, directory, named)
    if sub == "branch":
        return _branch(rest)
    return None


def _flags(args, value_options=()):
    """(long option names, short option letters, positional args) of args; '--' ends the options."""
    long, short, positional = set(), set(), []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            positional += args[i + 1:]
            break
        if arg.startswith("--"):
            name = arg.split("=", 1)[0]
            long.add(name)
            if name in value_options and "=" not in arg:
                i += 1
        elif arg.startswith("-") and len(arg) > 1:
            letters = arg[1:]
            short |= set(letters)
            if "-" + letters[-1] in value_options and len(letters) == 1:
                i += 1
        else:
            positional.append(arg)
        i += 1
    return long, short, positional


def _push(args, directory, named):
    long, short, positional = _flags(args, PUSH_VALUE_OPTIONS)
    reasons = []
    if long & {"--force", "--force-with-lease", "--force-if-includes"} or "f" in short:
        reasons.append("force")
    if "--delete" in long or "d" in short:
        reasons.append("delete")
    if long & {"--tags", "--follow-tags"}:
        reasons.append("tags")
    if long & {"--all", "--mirror", "--branches"}:
        reasons.append("all branches")

    def checked_out():
        current = current_branch(directory)
        if current in PROTECTED:
            reasons.append(f"to {current}")
        elif current is None and named:
            # The command names the repository (cd, git -C) and its branch cannot be read: it might be main.
            reasons.append("branch unknown")

    refspecs = positional[1:]
    previous = None
    for spec in refspecs:
        if previous == "tag":
            reasons.append("tags")
        previous = spec
        if spec == "tag":
            continue
        if PLACEHOLDER in spec:
            reasons.append("computed refspec")
            continue
        if spec.startswith("+"):
            reasons.append("force")
            spec = spec[1:]
        if spec.startswith(":"):
            reasons.append("delete")
        source, _, destination = spec.partition(":")
        destination = destination or source
        if source.startswith("refs/tags/") or destination.startswith("refs/tags/"):
            reasons.append("tags")
        target = destination[len("refs/heads/"):] if destination.startswith("refs/heads/") else destination
        if target in PROTECTED:
            reasons.append(f"to {target}")
        elif target in ("HEAD", "@") or not target:
            checked_out()
    if not refspecs:
        checked_out()
    unique = list(dict.fromkeys(reasons))
    return f"git push ({', '.join(unique)})" if unique else None


def _branch(args):
    long, short, _ = _flags(args)
    if "D" in short:
        return "git branch -D"
    if ("--delete" in long or "d" in short) and ("--force" in long or "f" in short):
        return "git branch -D"
    return None


def current_branch(directory):
    """The branch checked out in the repository holding directory (from HEAD), or None."""
    if not directory:
        return None
    try:
        path = os.path.abspath(directory)
        for _ in range(64):
            dot = os.path.join(path, ".git")
            if os.path.isdir(dot):
                head = os.path.join(dot, "HEAD")
                break
            if os.path.isfile(dot):
                with open(dot, encoding="utf-8") as fh:
                    line = fh.readline().strip()
                if not line.startswith("gitdir:"):
                    return None
                gitdir = line[len("gitdir:"):].strip()
                head = os.path.join(path, gitdir, "HEAD") if not os.path.isabs(gitdir) else os.path.join(gitdir, "HEAD")
                break
            parent = os.path.dirname(path)
            if parent == path:
                return None
            path = parent
        else:
            return None
        with open(head, encoding="utf-8") as fh:
            ref = fh.readline().strip()
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return ref[len("ref: refs/heads/"):] if ref.startswith("ref: refs/heads/") else None


def last_human_text(path, limit=TAIL_LIMIT):
    """The text of the newest transcript entry the person wrote (see the module docstring), "" when it
    has no text, or None when the transcript holds none within limit bytes of its end."""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        position = fh.tell()
        carry, scanned = b"", 0
        while position > 0 and scanned < limit:
            step = min(CHUNK, position)
            position -= step
            fh.seek(position)
            block = fh.read(step) + carry
            lines = block.split(b"\n")
            carry = lines[0] if position > 0 else b""
            for line in reversed(lines if position == 0 else lines[1:]):
                text = _human_text(line)
                if text is not None:
                    return text
            scanned += step
    return None


def _human_text(line):
    if b'"human"' not in line:
        return None
    try:
        entry = json.loads(line)
    except ValueError:
        return None
    if not isinstance(entry, dict) or entry.get("isSidechain") or entry.get("isCompactSummary"):
        return None
    if entry.get("type") == "user" and _is_human(entry.get("origin")) and not entry.get("isMeta"):
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            blocks = [b for b in content if isinstance(b, dict)]
            if any(b.get("type") == "tool_result" for b in blocks):
                return None
            return "\n".join(b["text"] for b in blocks if b.get("type") == "text" and isinstance(b.get("text"), str))
        return None
    if entry.get("type") == "attachment":
        attachment = entry.get("attachment")
        if (isinstance(attachment, dict) and attachment.get("type") == "queued_command"
                and _is_human(attachment.get("origin"))):
            prompt = attachment.get("prompt")
            if isinstance(prompt, str):
                return prompt
            if isinstance(prompt, list):
                return "\n".join(b["text"] for b in prompt
                                 if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))
            return ""
    return None


def _is_human(origin):
    return isinstance(origin, dict) and origin.get("kind") == "human"


def authorizes(text):
    """True when text has a 授權 shaped as a go-ahead and none that is refused or asked. Per occurrence, its
    clause is the text between the nearest punctuation marks (CLAUSE_END) around it. A negation
    (NEGATIONS) before it in the clause or a question (QUESTIONS) anywhere in the clause refuses the whole
    message. It counts only when the clause text before it is empty or a short affirmation (LEADS: 好，
    OK, 我, 可以) and the text after it does not start with a FOLLOWERS word (授權之前, 授權條款), so
    等我授權, 之後再授權 and OAuth 授權流程 do not count. A 授權 in quotes (「授權」) is a mention: it neither
    counts nor refuses."""
    if not isinstance(text, str) or WORD not in text:
        return False
    counted = False
    start = 0
    while (index := text.find(WORD, start)) >= 0:
        end = index + len(WORD)
        start = end
        before, after = text[index - 1:index], text[end:end + 1]
        if before and after and before in QUOTES_OPEN and after in QUOTES_CLOSE:
            continue
        left = max(text.rfind(mark, 0, index) for mark in CLAUSE_END) + 1
        stops = [pos for pos in (text.find(mark, end) for mark in CLAUSE_END) if pos >= 0]
        right = min(stops) + 1 if stops else len(text)
        lead, clause = text[left:index], text[left:right]
        if any(word in lead for word in NEGATIONS) or any(word in clause for word in QUESTIONS):
            return False
        shaped = LEADS.fullmatch("".join(lead.split()).lower()) is not None
        if shaped and not text[end:].lstrip().startswith(FOLLOWERS):
            counted = True
    return counted


def ask(actions, why):
    reason = (f"Provost outward gate: {'; '.join(actions)}. {why}. "
              f"Approve it here, or send a new message that says {WORD} to let such commands through.")
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                   "permissionDecisionReason": reason}}


def decide(event):
    """The hook output for one PreToolUse event: {} (no decision) or an "ask" decision."""
    tool = event.get("tool_name")
    if event.get("hook_event_name") != "PreToolUse" or tool not in ("Bash", "PowerShell"):
        return {}
    tool_input = event.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return {}
    cwd = event.get("cwd") if isinstance(event.get("cwd"), str) else None
    actions = outward_actions(command, cwd, powershell=tool == "PowerShell")
    if not actions:
        return {}
    if event.get("agent_id"):
        return ask(actions, "A subagent cannot be authorized by a message")
    path = event.get("transcript_path")
    try:
        text = last_human_text(path) if isinstance(path, str) and path else None
    except (OSError, ValueError):
        text = None
    if text is None:
        return ask(actions, "No message of yours could be read from the transcript")
    if authorizes(text):
        return {}
    return ask(actions, f"Your last message does not say {WORD}")


def _candidate(raw):
    lowered = raw.lower()
    return any(word in lowered for word in KEYWORDS)


def main(stdin=None, stdout=None):
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    raw = ""
    try:
        raw = stdin.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise ValueError("input too large")
        event = json.loads(raw)
        if not isinstance(event, dict):
            raise ValueError("not an object")
        result = decide(event)
    except Exception:  # noqa: BLE001 - a broken check asks for a candidate command, never denies
        result = ask(["unchecked command"], "The gate could not check it") if _candidate(raw) else {}
    if result:
        stdout.write(json.dumps(result, ensure_ascii=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
