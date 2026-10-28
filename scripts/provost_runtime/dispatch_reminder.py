"""Dispatch reminder: the PreToolUse (Agent) branch of hooks/acceptance-gate.

When the main session dispatches implementer, implementer-sonnet or mech-executor while it has an
acceptance file (<cwd>/.claude/acceptance/*.json that its own transcript shows it wrote, found by
acceptance.session_files; another session's file in the same directory does not count) and the brief
(tool_input.prompt) cites none of the session's files, remind() returns additional context asking main to
cite them. It is advisory: never a permission decision, and any error or doubt returns {}.

A brief cites a file when its prompt contains the file's name (feature.json). That also matches the
relative path .claude/acceptance/feature.json and an absolute path, since both end in the name; the name
must not continue a longer one (myfeature.json, feature.json.bak do not count). Case is ignored.
"""
import re

from .acceptance import _directory, load, session_files, DIRECTORY, Malformed

ROLES = ("implementer", "implementer-sonnet", "mech-executor")


def remind(event):
    """The PreToolUse additionalContext for an uncited implementation brief, else {}. Never raises."""
    try:
        if "agent_id" in event:  # only the main session's dispatches are checked
            return {}
        tool_input = event.get("tool_input")
        if not isinstance(tool_input, dict) or tool_input.get("subagent_type") not in ROLES:
            return {}
        prompt = tool_input.get("prompt")
        if not isinstance(prompt, str):
            return {}
        directory = _directory(event)
        if directory is None:
            return {}
        files = session_files(event, directory)
        if not files or any(_cites(prompt, path.name) for path in files):
            return {}
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "additionalContext": _text(files, any(_visual(path) for path in files))}}
    except Exception:  # advisory: a broken reminder must never disturb a dispatch
        return {}


def _cites(prompt, name):
    text = prompt.replace("\\", "/")
    return re.search(r"(?<![\w.-])" + re.escape(name) + r"(?![\w-]|\.\w)", text, re.IGNORECASE) is not None


def _visual(path):
    try:
        return any(item["kind"] == "visual" for item in load(path)["gates"])
    except (Malformed, OSError):  # a malformed file is the Stop check's to report
        return False


def _text(files, visual):
    names = ", ".join("/".join(DIRECTORY) + "/" + path.name for path in files)
    text = ("This brief does not cite the session's acceptance file (%s); cite it so the implementer builds "
            "against the same gates that will judge the work." % names)
    if visual:
        text += (" It has visual gates: also name the fixture data each screenshot needs and the "
                 "screenshot output directory.")
    return text
