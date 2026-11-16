"""Outward-action gate: the PreToolUse (Bash, PowerShell) check behind hooks/outward-gate.

python -m provost_runtime.outward_gate reads one PreToolUse event on stdin. When the command would
merge a pull request, publish a release or package, or force, delete, tag or main/master-push with git,
it prints a permissionDecision "ask" unless the person's most recent own message is a 授權 go-ahead;
otherwise it prints nothing (no decision: the normal permission flow, auto mode's classifier included,
decides).

The person's messages are the transcript entries Claude Code marks origin.kind "human": typed prompts
(type "user") and messages sent mid-turn (a "queued_command" attachment). Subagent hand-backs ("peer"),
background notices ("task-notification"), compact summaries and slash-command output carry another or no
origin and never authorize. A call from a subagent (agent_id) always asks. A message authorizes only when
the whole of it, one line, is the go-ahead shape: at most two short leads (好, OK, 我 ...), 授權, then only
target words (merge, push, release, 合併, 安裝, PR, #141, v1.2.0 ...) and final punctuation (see
authorizes); any other text in the message, a negation, a question or a condition, makes it not count.

The command check fails closed: git, gh and npm are recognised anywhere in a simple command (after
if/then/do, braces, sudo -u, xargs -I ...), so a command that only mentions them as plain words (echo gh pr
merge) asks too, and a candidate command (one with a KEYWORDS word) passes silently only when it is plain.
Anything else adds "unreadable command (<what>)": a comment, a heredoc other than cat <<WORD with its exact
terminator, a here-string, a substitution other than a cat heredoc or the current-branch idioms, a Bash
subshell (or a PowerShell directory change beside parentheses or braces), a variable, splat or call
operator, a line continuation glued to a word, a shell reading a script, pipe or stdin, a PowerShell
backtick escape, a brace expansion, unbalanced quotes, nesting deeper than MAX_DEPTH, or more than BUDGET
characters to read. Every failure on a candidate command asks; nothing here ever denies. Stdlib only, no
store access.
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
# Characters the command check may lex, summed over every level, every reading and every starting argument
# of a bash -c string (each text read also costs READ_COST; heredoc bodies fed to cat are free): about 0.2 s
# of work at most (one long unterminated quote is the slowest input). A hook that times out lets the
# command run, so once this is spent the check stops and asks ("unreadable command (too long)").
BUDGET = 50_000
READ_COST = 64
# Lower-case substrings every outward command contains; hooks/outward-gate uses the same list for its
# fast path and install.py for the handlers' "if" rules.
KEYWORDS = ("push", "merge", "release", "publish", "branch")
PROTECTED = ("main", "master")
# A message authorizes only as a whole: [lead [separator]]{0,2} 授權 [targets] [final punctuation], one
# line, nothing else (see authorizes). Leads and targets are matched case-insensitively.
LEADS = ("好的", "好", "okay", "ok", "yes", "可以", "同意", "我", "那就")
TARGETS = ("merge", "push", "release", "publish", "install", "setup", "合併", "推送", "發布", "發版", "安裝",
           "並", "和", "跟", "及", "然後", "pr")
_SEPARATOR = r"[\s，,、]"
_GO_AHEAD = re.compile(
    rf"(?:(?:{'|'.join(LEADS)}){_SEPARATOR}*){{0,2}}{WORD}"
    # Each target atomic and digits never split, so a long message cannot make the match backtrack.
    rf"(?:{_SEPARATOR}*+(?>{'|'.join(TARGETS)}|#\d++|v?\d++(?:\.\d++)++|\d++))*+[。.!！]*+",
    re.IGNORECASE)

POSIX_SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
POWERSHELLS = {"pwsh", "powershell", "iex", "invoke-expression"}
SHELLS = POSIX_SHELLS | POWERSHELLS | {"cmd", "eval"}  # cmd and eval keep the dialect they are called from
CHDIR = {"cd", "pushd", "set-location", "sl", "chdir", "push-location"}
POPDIR = {"popd", "pop-location"}  # back to a directory the command does not name
COMMAND_WORDS = SHELLS | {"git", "gh", "npm"}
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
    powershell: backslashes are path separators, not escapes. A command with a KEYWORDS word that some
    part of cannot be read with certainty also gets "unreadable command (<what>)" labels, after the
    actions that were found."""
    found = []
    problems = _Problems()  # shared by both readings: one budget per command
    # A backslash in a bash command is an escape, but a Windows path (C:\tools\gh.exe) typed into the Bash
    # tool means a separator: read such a command both ways and report what either reading finds.
    readings = (powershell,) if powershell or "\\" not in str(command) else (False, True)
    for reading in readings:
        # That second reading of a Bash command is a guess made to find more actions: what it cannot read
        # is not noted (Bash text is not plain PowerShell), but it spends the same budget.
        sink = problems if reading == powershell else _Problems(problems.left)
        state = {"cwd": cwd, "named": False, "powershell": reading, "depth": 0, "problems": sink}
        for tokens in _segments(command, 0, reading, sink):
            for label in _classify(tokens, state):
                if label not in found:
                    found.append(label)
        if sink is not problems:
            problems.left = sink.left
            for what in ("too deep", "too long"):
                if what in sink:
                    _note(problems, what)
    if problems and isinstance(command, str) and _candidate(command):
        found += [f"unreadable command ({what})" for what in problems]
    return found


class _Problems(dict):
    """What could not be read, in order of discovery (the keys: a dict as an ordered set), and the part of
    BUDGET still left to read."""

    def __init__(self, budget=BUDGET):
        super().__init__()
        self.left = budget


def _note(problems, what):
    if problems is not None:
        problems.setdefault(what)


def _spend(problems, amount):
    """Take amount from the budget: False, with "too long" noted, once it is spent."""
    problems.left -= amount
    if problems.left < 0:
        _note(problems, "too long")
        return False
    return True


def _afford(problems, amount):
    """Whether amount more may be read (always, without a budget to take it from)."""
    return problems is None or ("too long" not in problems and _spend(problems, amount))


def _stopped(problems):
    """Whether reading further cannot change the outcome: the check already asks for being too deep or
    too long."""
    return problems is not None and ("too deep" in problems or "too long" in problems)


def _lex(command, powershell, problems=None):
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        if powershell:
            lexer.escape = ""
        return list(lexer)
    except ValueError:  # unbalanced quotes: a plain split, which may find more commands, never fewer
        _note(problems, "quotes")
        return re.findall(r"[();<>|&]+|[^\s();<>|&]+", command.replace('"', " ").replace("'", " "))


def _segments(command, depth, powershell=False, problems=None):
    """Simple commands of command (token lists): split on shell operators and newlines, with every $(...)
    and (outside PowerShell) backtick substitution read as a command of its own wherever it stands, in
    quotes or glued to a word, and left in place as one placeholder word. Whatever is not plain (see
    _scan and the module docstring) is noted in problems, which also holds the budget left."""
    if not isinstance(command, str) or not command.strip():
        return
    if problems is None:
        problems = _Problems()
    if depth > MAX_DEPTH:
        _note(problems, "too deep")
        return
    if "too long" in problems or not _spend(problems, len(command) + READ_COST):
        return
    command = _heredocs(command.replace("\r\n", "\n").replace("\r", "\n"), problems)
    # Line continuations join lines: a Bash \<newline> disappears, a PowerShell `<newline> (and, read as
    # PowerShell, a \<newline>) is a space. One glued to a word may join it to the next line's word.
    if re.search(r"\S[\\`]\n", command):
        _note(problems, "continuation")
    if powershell:
        command = re.sub(r"[\\`]\n", " ", command)
    else:
        command = command.replace("\\\n", "").replace("`\n", " ")
    command = command.replace("\n", " ; ")  # any other newline ends a command
    inner = []
    command = _substitute(command, powershell, inner, problems)
    for text in inner:
        yield from _segments(text, depth + 1, powershell, problems)
    _scan(command, powershell, problems)
    if powershell:
        # ${...} may name a variable with any characters, braces and operators included: it is not read.
        if "${" in command:
            _note(problems, "${...}")
        # Script-block braces need no spaces in PowerShell: {gh pr merge 5}, % {git push -f}; ${...} stays
        # one word.
        padded = re.sub(r"\$\{[^}]*\}|[{}]", lambda m: m[0] if len(m[0]) > 1 else f" {m[0]} ", command)
        if not _spend(problems, len(padded) - len(command)):
            return
        command = padded
    tokens = _lex(command, powershell, problems)
    if powershell:
        # A PowerShell backtick inside a word escapes the next character (g`it is git): it is not read.
        if any("`" in token for token in tokens):
            _note(problems, "escape")
        # Parentheses and braces are expressions and script blocks in PowerShell, and a directory change
        # inside one may or may not last: with both in one command, the directory is not known.
        if ("(" in command or "{" in command) and any(_name(t) in CHDIR or _name(t) in POPDIR for t in tokens):
            _note(problems, "subshell")
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


def _substitute(command, powershell, inner, problems=None):
    """command with each $(...) (and, outside PowerShell, each `...`) replaced by a placeholder word, or by
    HEAD when its text names the checked-out branch; the texts are appended to inner. Each one is noted in
    problems as a "substitution" unless it names the checked-out branch or is a plain cat heredoc ("$(cat
    <<'EOF' ... EOF)", marked by _heredocs); an unclosed one runs to the end and, like one whose text holds
    another $( or backtick, is noted too. In Bash a backslash makes the next character literal (\` is a
    backtick, not a substitution). Single quotes are not honoured: a quoted '$(x)' is read as a command too
    (asks more)."""
    out, i, n = [], 0, len(command)
    while i < n:
        if command[i] == "\\" and not powershell:
            out.append(command[i:i + 2])
            i += 2
            continue
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
        if j >= n:
            _note(problems, "unclosed substitution")
        if "$(" in text or "`" in text:
            _note(problems, "nested substitution")
        inner.append(text)
        plain = " ".join(text.replace(";", " ").split()).lower()
        if plain not in CURRENT_BRANCH and plain != "cat " + CAT_HEREDOC:
            _note(problems, "substitution")
        out.append("HEAD" if plain in CURRENT_BRANCH else PLACEHOLDER)
        i = j + 1
    return "".join(out)


# A delimiter word, bare or wholly quoted, ending at whitespace, the end of the line or an operator.
_DELIMITER = re.compile(r"[ \t]*(?:([A-Za-z_][A-Za-z0-9_]*)|'([A-Za-z_][A-Za-z0-9_]*)'"
                        r"|\"([A-Za-z_][A-Za-z0-9_]*)\")(?=[\s();<>|&]|$)")
CAT_HEREDOC = "\x01"  # left where a plain cat heredoc stood, so "$(cat <<'EOF' ... EOF)" reads as plain


def _heredocs(command, problems=None):
    """command with each heredoc operator (<<EOF, <<-'EOF') and terminator line taken out. A plain heredoc
    is cat <<WORD (cat right before the operator, WORD bare or wholly quoted): its body (a commit message,
    a PR body) is data and is dropped, and CAT_HEREDOC marks its place. Any other heredoc (bash <<EOF,
    cat > f <<EOF) is noted in problems and keeps its body, which is then read as commands; so is a <<<
    here-string. When a delimiter is not a simple word or its terminator line (the delimiter exactly;
    for <<- after leading tabs) is missing, the command is returned unchanged, every line read as
    commands, and the heredoc is noted. Linear in the length of command."""
    out, pending = [], []  # pending: [delimiter, strip_tabs, data] of the heredocs whose body comes next
    for line in command.split("\n"):
        if pending:
            delimiter, strip_tabs, data = pending[0]
            if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                pending.pop(0)
                continue
            if data:
                continue
        pieces, position, found = [], 0, []
        start = line.find("<<")
        while start >= 0:
            if line.startswith("<<<", start):
                _note(problems, "here-string")
                end = start + 3
                while end < len(line) and line[end] == "<":
                    end += 1
                start = line.find("<<", end)
                continue
            strip_tabs = line.startswith("-", start + 2)
            word = _DELIMITER.match(line, start + 2 + strip_tabs)
            if not word:
                _note(problems, "heredoc")
                return command
            before = start  # cat must stand right before the operator, after at least one space or tab
            while before > position and line[before - 1] in " \t":
                before -= 1
            data = (before < start and line.endswith("cat", position, before)
                    and (before == 3 or not (line[before - 4].isalnum() or line[before - 4] in "_-.")))
            if not data:
                _note(problems, "heredoc")
            found.append([next(group for group in word.groups() if group), strip_tabs, data])
            pieces.append(line[position:start] + (f" {CAT_HEREDOC} " if data else " "))
            position = word.end()
            start = line.find("<<", position)
        out.append("".join(pieces) + line[position:])
        # A heredoc opened inside a body read as commands ends before the one around it.
        pending[:0] = found
    if pending:
        _note(problems, "heredoc")
        return command
    return "\n".join(out)


_BREAK = set(" \t;&|()<>{}")  # characters after which a word starts


def _scan(command, powershell, problems):
    """Note in problems what makes command (after heredocs and substitutions) not plain, honouring quotes:
    a # starting a word ("comment"); a $ outside single quotes ("variable"); in Bash a ( or ) outside
    quotes ("subshell") and a {a,b} or {1..3} brace expansion; in PowerShell an @word splat ("variable"),
    an @' or @" here-string ("here-string") and the call operator & at the start of a command
    ("variable"). Linear in the length of command."""
    escape = "`" if powershell else "\\"
    quote, i, n = None, 0, len(command)
    braces_seen = 0  # a { before this was looked at by an earlier one's lookahead
    while i < n:
        c = command[i]
        if quote == "'":
            if c == "'":
                quote = None
            i += 1
            continue
        if c == escape:
            i += 2
            continue
        if quote == '"':
            if c == '"':
                quote = None
            elif c == "$":
                _note(problems, "variable")
            i += 1
            continue
        start = i == 0 or command[i - 1] in _BREAK
        if c in "'\"":
            quote = c
        elif c == "$":
            _note(problems, "variable")
        elif c == "#" and start:
            _note(problems, "comment")
        elif not powershell and c in "()":
            _note(problems, "subshell")
        elif not powershell and c == "{" and i >= braces_seen:
            end = i + 1
            while end < n and command[end] not in " \t;&|()<>'\"}":
                end += 1
            braces_seen = end
            if end < n and command[end] == "}" and ("," in command[i:end] or ".." in command[i:end]):
                _note(problems, "brace expansion")
        elif powershell and c == "@" and start and i + 1 < n:
            following = command[i + 1]
            if following in "'\"":
                _note(problems, "here-string")
            elif following.isalnum() or following == "_":
                _note(problems, "variable")
        elif powershell and c == "&" and command[i + 1:i + 2] != "&" and command[i - 1:i] != "&":
            back = i
            while back > 0 and command[back - 1] in " \t":
                back -= 1
            if back == 0 or command[back - 1] in ";|{(=":
                _note(problems, "variable")
        i += 1


def _name(token):
    base = re.split(r"[\\/]", token)[-1].lower()
    for suffix in (".exe", ".cmd", ".bat", ".ps1"):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


def _computed(word):
    return PLACEHOLDER in word or any(mark in word for mark in "$%`")


def _join(base, target, named=False):
    """target (from cd or git -C) made absolute against base when possible; None when the command does not
    say which directory it is (a variable, a substitution, cd -, or a relative path after such a one:
    base None while named)."""
    if target == "-" or _computed(target):
        return None
    target = os.path.expanduser(target)
    match = re.fullmatch(r"/([A-Za-z])(/.*)?", target.replace("\\", "/"))
    if os.name == "nt" and match:  # Git Bash spelling of a drive path: /d/x is D:/x
        target = f"{match.group(1).upper()}:{match.group(2) or '/'}"
    if os.path.isabs(target):
        return target
    if base:
        return os.path.join(base, target)
    return None if named else target


def _classify(tokens, state):
    """The labels of the outward actions in one simple command. Command words are looked for at every
    position, not only the first, so keywords (if, then, do), braces and wrappers with options
    (sudo -u me, xargs -I {}) cannot hide them."""
    found = []
    directory_words = CHDIR | POPDIR
    first_directory = next((k for k, t in enumerate(tokens) if _name(t) in directory_words), -1)
    for i, token in enumerate(tokens):
        name = _name(token)
        if name not in directory_words and name not in COMMAND_WORDS:
            continue
        # Reading the arguments costs their count, so a long run of command words stays within BUDGET.
        if not _afford(state.get("problems"), len(tokens) - i):
            break
        args = tokens[i + 1:]
        if name in directory_words and i == first_directory:
            if name in POPDIR:
                state["cwd"], state["named"] = None, True
                continue
            target = next((a for a in args if a == "-" or not a.startswith("-")), None)
            if target:
                state["cwd"] = _join(state.get("cwd"), target, state.get("named", False))
                state["named"] = True
        elif name == "git":
            found.append(_git(args, state))
        elif name == "gh":
            found.append(_gh(args))
        elif name == "npm" and "publish" in [a.lower() for a in args if not a.startswith("-")]:
            found.append("npm publish")
        elif name in SHELLS:
            # bash -c "git push -f", pwsh -NoProfile -Command git push -f, eval "...": options and their
            # values come first, so the command text is read from every starting argument, in the dialect
            # of the shell (cmd and eval keep the one they are called from). A shell given no command text
            # to read here (a script file, a pipe, stdin, -EncodedCommand) is not plain.
            if not _inline(name, args):
                _note(state.get("problems"), "shell input")
            depth = state.get("depth", 0) + 1
            if depth > MAX_DEPTH:
                _note(state.get("problems"), "too deep")
            if _stopped(state.get("problems")):  # it asks already; more reading only costs time
                continue
            powershell = name in POWERSHELLS or (name not in POSIX_SHELLS and state.get("powershell", False))
            inner_state = {**state, "depth": depth, "powershell": powershell}
            for start in range(min(len(args), 64)):
                for inner in _segments(" ".join(a for a in args[start:] if a != PLACEHOLDER), depth,
                                       powershell, state.get("problems")):
                    found += _classify(inner, inner_state)
    return [label for label in dict.fromkeys(found) if label]


def _gh(args):
    lowered = [a.lower() for a in args]
    for (first, second), label in GH_PAIRS.items():
        for k, word in enumerate(lowered):
            if word == first and _gh_sub(lowered, k + 1) == second:
                return label
    return None


def _gh_sub(args, i=0):
    """The subcommand word at args[i:], skipping flags and their values (gh pr -R o/r merge). After a
    flag the next word could be its value or the subcommand: a protected word there counts."""
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
                directory, named = _join(directory, args[i + 1], named), True
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
    if _given(long, "--force", "--force-with-lease", "--force-if-includes") or "f" in short:
        reasons.append("force")
    if _given(long, "--delete") or "d" in short:
        reasons.append("delete")
    if _given(long, "--tags", "--follow-tags"):
        reasons.append("tags")
    if _given(long, "--all", "--mirror", "--branches"):
        reasons.append("all branches")

    def checked_out():
        current = current_branch(directory)
        if current in PROTECTED:
            reasons.append(f"to {current}")
        elif current is None and named:
            # The command names the repository (cd, git -C) and its branch cannot be read: it might be main.
            reasons.append("branch unknown")

    refspecs = positional[1:]
    # A computed remote may carry the refspecs too; a glob (mai?, *) may match a file named like a branch.
    if positional and (_computed(positional[0]) or any(mark in positional[0] for mark in "*?[")):
        reasons.append("computed refspec")
    previous = None
    for spec in refspecs:
        if previous == "tag":
            reasons.append("tags")
        previous = spec
        if spec == "tag":
            continue
        if _computed(spec) or any(mark in spec for mark in "*?["):
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


def _given(long, *names):
    """Whether long holds one of names or an abbreviation of it: git takes any unambiguous prefix of a
    long option (--tag, --del, --mirr), and an ambiguous one fails, so every prefix counts."""
    return any(len(option) > 2 and name.startswith(option) for option in long for name in names)


def _inline(name, args):
    """Whether shell name is given its command text in args (bash -c, pwsh -Command, cmd /c, eval x)."""
    lowered = [a.lower() for a in args]
    if name in ("eval", "iex", "invoke-expression"):
        return bool(args)
    if name == "cmd":
        return any(a in ("/c", "/k") for a in lowered)
    if name in POWERSHELLS:
        if any(a in ("-e", "-ec") or (len(a) > 2 and "-encodedcommand".startswith(a)) for a in lowered):
            return False
        return any(len(a) > 1 and ("-command".startswith(a) or a.startswith("-command")) for a in lowered)
    return any(a.startswith("-") and not a.startswith("--") and "c" in a for a in args)


def _branch(args):
    long, short, _ = _flags(args)
    if "D" in short:
        return "git branch -D"
    if (_given(long, "--delete") or "d" in short) and (_given(long, "--force") or "f" in short):
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
    """True when the whole of text, stripped and on one line, is a go-ahead:
    [lead [separator]]{0,2} 授權 [targets] [final punctuation]. A lead is one of LEADS (好, 好的, OK, okay,
    yes, 可以, 同意, 我, 那就); a separator is whitespace, ，, , or 、. The targets are words from TARGETS
    (merge, push, release, publish, install, setup, 合併, 推送, 發布, 發版, 安裝, 並, 和, 跟, 及, 然後, PR), #141,
    141 or a version (v1.2.0), each after optional separators (合併並安裝 may be glued); the final
    punctuation is any run of 。 . ! ！. So 授權, 好，我授權, 授權 merge PR #141 and 授權合併並安裝 count, and
    any other text makes the message not count: 授權取消, 授權等 CI 過了再說, 授權了嗎？, 等我授權,
    「授權」是什麼, 我授權你 merge #140."""
    if not isinstance(text, str):
        return False
    text = text.strip()
    if len(text.splitlines()) != 1:
        return False
    return _GO_AHEAD.fullmatch(text) is not None


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
    return ask(actions, f"Your last message is not a go-ahead such as {WORD} or {WORD} merge #141")


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
