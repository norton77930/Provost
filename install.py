#!/usr/bin/env python3
"""Install this repository's Claude Code setup into a config dir, or report drift.

python install.py [install|check|setup] [--dry-run] [--config-dir DIR] [--source DIR] [--json]

check (default) is read-only: exit 0 no drift, 1 drift, 2 error (--dry-run is rejected with check).
install writes only changed targets, after backing them up: exit 0 success, 2 error.
setup is install plus first-time wiring: it appends the CLAUDE.md markers when there are none,
adds the missing Provost hook registrations to settings.json (other values are kept; the file is
re-serialized with 2-space indent) and writes provost/config.json when it does not exist.
The first time it replaces a local skills/handoff/SKILL.md it also keeps that copy beside it.
An error or interrupt during install or setup rolls back and names the backup dir.
Stdlib only; never prints settings.json values, and inspects only its hook entries.
"""
import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

RUNTIME = "provost/runtime/native-pilot-v1"
HOOKS = ("_python.sh", "runtime", "native-event", "native-facts", "acceptance-gate")
POLICY_SOURCE = "native/CLAUDE.native.md"
POLICY_TARGET = "CLAUDE.md"
BEGIN = b"<!-- claudex-native:begin -->"
END = b"<!-- claudex-native:end -->"
# A local copy of this file that differs is kept beside it (<name>.local-<UTC time>.bak) before the
# installer first overwrites it; once such a copy exists, later installs only use the backup dir.
KEEP_LOCAL = "skills/handoff/SKILL.md"
# The hooks check requires per event; Stop needs both the async declaration scan and the acceptance gate,
# PreToolUse the acceptance gate's dispatch reminder.
HOOK_EVENTS = {"SessionStart": ("native-event",),
               "PreToolUse": ("acceptance-gate",),
               "PostToolUse": ("native-facts",),
               "Stop": ("native-event", "acceptance-gate")}
# Hooks that count only when registered without "async": true (an async Stop hook cannot block the turn,
# and an async PreToolUse hook cannot add context to the dispatch).
SYNC_HOOKS = ("acceptance-gate",)
SETTINGS = "settings.json"
RUNTIME_CONFIG = "provost/config.json"
# What setup registers: (event, matcher or None, hook, extra hook keys), as in docs/native-handoff.md.
REGISTRATIONS = (
    ("SessionStart", "startup|clear|compact|resume|fork", "native-event", {"timeout": 30}),
    ("PreCompact", "manual|auto", "native-event", {"timeout": 30}),
    ("PostCompact", "manual|auto", "native-event", {"timeout": 30}),
    ("PreToolUse", "Agent", "acceptance-gate", {"timeout": 30}),
    ("PostToolUse", "Edit|Write|NotebookEdit|Agent|Bash|PowerShell|EnterWorktree|ExitWorktree",
     "native-facts", {"async": True, "timeout": 20}),
    ("UserPromptSubmit", None, "native-facts", {"timeout": 10}),
    ("Stop", None, "native-event", {"async": True, "timeout": 20}),
    ("Stop", None, "acceptance-gate", {"timeout": 30}),
)
DEFAULT_CONFIG = {"schema_version": 1, "mode": "adaptive", "workspace_roots": [], "all_workspaces": True,
                  "max_writers": 2, "burst_writers": 3, "handoff": True}
BOM = b"\xef\xbb\xbf"


class InstallError(Exception):
    pass


@dataclass
class Target:
    rel: str           # posix path relative to the config dir
    source: Path | None  # None: desired is built in memory (setup's settings and runtime config)
    policy: bool = False
    desired: bytes = b""
    status: str = ""   # ok | modified | missing


def digest(data):
    return hashlib.sha256(data).hexdigest()


def source_targets(source):
    """Every managed (source file, target rel path) pair; a missing source or empty policy is an error."""
    pairs = [(f"hooks/{name}", f"{RUNTIME}/hooks/{name}") for name in HOOKS]
    pairs += [(f"scripts/provost_runtime/{p.name}", f"{RUNTIME}/scripts/provost_runtime/{p.name}")
              for p in _glob(source, "scripts/provost_runtime", "*.py")]
    pairs.append(("docs/native-handoff.md", f"{RUNTIME}/docs/native-handoff.md"))
    pairs += [(f"docs/compatibility/{p.name}", f"{RUNTIME}/docs/compatibility/{p.name}")
              for p in _glob(source, "docs/compatibility", "*.md")]
    pairs += [(f"agents/{p.name}", f"agents/{p.name}") for p in _glob(source, "agents", "*.md")]
    pairs.append((KEEP_LOCAL, KEEP_LOCAL))
    pairs.append(("skills/verification-before-completion/SKILL.md",
                  "skills/verification-before-completion/SKILL.md"))
    pairs.append(("tools/dispatch-report.js", "experiments/dispatch-report.js"))
    targets = []
    for src, rel in pairs:
        path = source / src
        if not path.is_file():
            raise InstallError(f"source file missing: {src}")
        targets.append(Target(rel, path))
    policy = source / POLICY_SOURCE
    if not policy.is_file():
        raise InstallError(f"source file missing: {POLICY_SOURCE}")
    if _is_blank(policy.read_bytes()):  # installing it would silently empty the user's section
        raise InstallError(f"source file is empty: {POLICY_SOURCE}")
    targets.append(Target(POLICY_TARGET, policy, policy=True))
    return targets


def _is_blank(data):
    """True when the bytes hold only a BOM and/or (Unicode) whitespace; undecodable bytes are not blank."""
    try:
        return not data.decode("utf-8-sig").strip()
    except UnicodeDecodeError:
        return not data.strip()


def _glob(source, directory, pattern):
    root = source / directory
    if not root.is_dir():
        raise InstallError(f"source directory missing: {directory}")
    found = sorted(p for p in root.glob(pattern) if p.is_file())
    if not found:
        raise InstallError(f"source directory has no {pattern}: {directory}")
    return found


def _near_misses(lines):
    """Hints for lines that look like a marker but are not an exact match (never accepted)."""
    bom = b"\xef\xbb\xbf"
    hints = []
    for number, line in enumerate(lines, 1):
        content = line.rstrip(b"\r\n")
        if content in (BEGIN, END):
            continue
        has_bom = number == 1 and content.startswith(bom)
        core = content[len(bom):] if has_bom else content
        for name, marker in (("begin", BEGIN), ("end", END)):
            if core.strip(b" \t") == marker:
                cause = "the file starts with a BOM" if has_bom else "the line has leading or trailing whitespace"
                hints.append(f"line {number} looks like the {name} marker but {cause}")
    return "; " + "; ".join(hints) if hints else ""


def split_policy(data):
    """Return (before, body, after) around the marker lines; after starts at the end marker."""
    lines = data.splitlines(keepends=True)
    begins = [i for i, line in enumerate(lines) if line.rstrip(b"\r\n") == BEGIN]
    ends = [i for i, line in enumerate(lines) if line.rstrip(b"\r\n") == END]
    if len(begins) != 1 or len(ends) != 1:
        raise InstallError(f"{POLICY_TARGET}: expected exactly one begin and one end marker line "
                           f"(found {len(begins)} begin, {len(ends)} end)" + _near_misses(lines))
    begin, end = begins[0], ends[0]
    if begin > end:
        raise InstallError(f"{POLICY_TARGET}: end marker comes before begin marker")
    eol = b"\r\n" if lines[begin].endswith(b"\r\n") else b"\n"
    return b"".join(lines[:begin + 1]), b"".join(lines[begin + 1:end]), b"".join(lines[end:]), eol


def policy_body(source_bytes, eol):
    body = source_bytes.replace(b"\r\n", b"\n")
    if body and not body.endswith(b"\n"):
        body += b"\n"
    return body.replace(b"\n", eol) if eol != b"\n" else body


def evaluate(config, targets, policy_current=None):
    """policy_current: CLAUDE.md bytes to use instead of the file (setup with markers to add)."""
    for target in targets:
        path = config / target.rel
        if target.policy:
            if policy_current is None and not path.is_file():
                raise InstallError(f"{POLICY_TARGET} missing in config dir: markers required")
            current = path.read_bytes() if policy_current is None else policy_current
            before, body, after, eol = split_policy(current)
            wanted = policy_body(target.source.read_bytes(), eol)
            target.desired = before + wanted + after
            target.status = "ok" if digest(body) == digest(wanted) else "modified"
            continue
        target.desired = target.source.read_bytes()
        if not path.exists():
            target.status = "missing"
        elif not path.is_file():
            raise InstallError(f"target is not a file: {target.rel}")
        else:
            target.status = "ok" if digest(path.read_bytes()) == digest(target.desired) else "modified"


def extras(config, targets):
    """Files in the installed runtime dir that the source no longer ships (never deleted)."""
    root = config / RUNTIME
    managed = {t.rel for t in targets}
    found = []
    if root.is_dir():
        for directory, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for name in files:
                rel = (Path(directory) / name).relative_to(config).as_posix()
                if rel not in managed:
                    found.append(rel)
    return sorted(found)


def check_layout(source, config, targets):
    src, cfg = source.resolve(), config.resolve()
    if src == cfg or cfg.is_relative_to(src) or src.is_relative_to(cfg):
        raise InstallError("config dir and source repo must not contain each other")
    for rel in [t.rel for t in targets] + ["provost/backups"]:
        if not (config / rel).resolve().is_relative_to(cfg):
            raise InstallError(f"target resolves outside the config dir: {rel}")


def settings_status(config):
    """ok | not-registered | unknown. Only hook command strings are inspected; nothing is printed."""
    try:
        data = json.loads((config / SETTINGS).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return "unknown"
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return "not-registered"
    for event, names in HOOK_EVENTS.items():
        for name in names:
            if not _registered(hooks.get(event), name):
                return "not-registered"
    return "ok"


def _registered(groups, name):
    """True when a hook entry of groups runs RUNTIME/hooks/<name> (synchronously, for SYNC_HOOKS)."""
    return any(_ends_with(text, f"{RUNTIME}/hooks/{name}")
               for text in _hook_strings(groups, sync_only=name in SYNC_HOOKS))


def _hook_strings(groups, sync_only=False):
    for group in groups if isinstance(groups, list) else []:
        entries = group.get("hooks") if isinstance(group, dict) else None
        for hook in entries if isinstance(entries, list) else []:
            if not isinstance(hook, dict) or (sync_only and hook.get("async") is True):
                continue
            if isinstance(hook.get("command"), str):
                yield hook["command"]
            args = hook.get("args")
            for arg in args if isinstance(args, list) else []:
                if isinstance(arg, str):
                    yield arg


def _ends_with(text, suffix):
    text = text.replace("\\", "/").strip().rstrip("\"' ")
    return text == suffix or text.endswith("/" + suffix)


def with_markers(config):
    """(CLAUDE.md bytes with the marker section, whether setup adds it); malformed markers are an error."""
    path = config / POLICY_TARGET
    data = path.read_bytes() if path.is_file() else b""
    lines = data.splitlines(keepends=True)
    if any(line.rstrip(b"\r\n") in (BEGIN, END) for line in lines) or _near_misses(lines):
        split_policy(data)  # raises unless exactly one well-formed section exists
        return data, False
    eol = b"\r\n" if b"\r\n" in data else b"\n"
    gap = b"" if not data else (eol if data.endswith(b"\n") else eol + eol)
    return data + gap + BEGIN + eol + END + eol, True


def _unique_keys(pairs):
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):  # json.loads would silently drop all but the last value
        raise ValueError("duplicate key")
    return dict(pairs)


def with_hooks(config):
    """(settings.json bytes with the missing registrations, events added); never echoes values."""
    path = config / SETTINGS
    raw = path.read_bytes() if path.is_file() else None
    data = {}
    if raw is not None:
        try:
            data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_keys)
        except ValueError:  # includes UnicodeDecodeError; its message could quote file content
            raise InstallError(f"{SETTINGS} is not valid JSON (or repeats a key); "
                               "fix it first, nothing was changed") from None
    if not isinstance(data, dict):
        raise InstallError(f"{SETTINGS} is not a JSON object; nothing was changed")
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise InstallError(f"{SETTINGS}: \"hooks\" is not an object; nothing was changed")
    runtime = (config / RUNTIME).as_posix()
    if '"' in runtime:
        raise InstallError("config dir path contains a double quote; register the hooks by hand")
    added = []
    for event, matcher, name, extra in REGISTRATIONS:
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise InstallError(f"{SETTINGS}: hooks.{event} is not a list; nothing was changed")
        if _registered(groups, name):
            continue
        group = {} if matcher is None else {"matcher": matcher}
        group["hooks"] = [{"type": "command", "command": f'bash "{runtime}/hooks/{name}"', **extra}]
        groups.append(group)
        if event not in added:
            added.append(event)
    if not added:
        return raw, added
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if raw is not None and b"\r\n" in raw:
        text = text.replace("\n", "\r\n")
    return (BOM if raw is not None and raw.startswith(BOM) else b"") + text.encode("utf-8"), added


def setup_targets(config):
    """Build setup's in-memory targets; returns (policy bytes, settings/config targets, report)."""
    policy, markers = with_markers(config)
    settings, added = with_hooks(config)
    extra = []
    if added:
        extra.append(Target(SETTINGS, None, desired=settings,
                            status="modified" if (config / SETTINGS).exists() else "missing"))
    runtime_config = config / RUNTIME_CONFIG
    if not runtime_config.exists():
        extra.append(Target(RUNTIME_CONFIG, None, status="missing",
                            desired=(json.dumps(DEFAULT_CONFIG, indent=2) + "\n").encode("utf-8")))
    report = {"markers": markers, "register": added,
              "runtime_config": "keep" if runtime_config.exists() else "create"}
    return policy, extra, report


def atomic_write(path, data, mode=None):
    """Replace path atomically: temp file in the same dir, fsync, os.replace.

    If os.replace is denied onto an existing file (seen on Windows for
    ~/.claude/agents/*.md), write that file in place instead. That fallback is
    not atomic; the caller's backup and rollback cover a failure partway.
    """
    temp = None
    try:
        fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
        temp = Path(name)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None and os.name != "nt":
            os.chmod(temp, mode)
        try:
            os.replace(temp, path)
        except PermissionError:
            if not path.exists():
                raise
            with open(path, "r+b") as stream:
                stream.write(data)
                stream.truncate()
                stream.flush()
                os.fsync(stream.fileno())
    finally:
        if temp is not None:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass  # best effort: never mask the write result or its error


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def kept_copies(config, rel):
    path = config / rel
    return sorted(path.parent.glob(path.name + ".local-*.bak"))


def to_keep(config, changes):
    """Rels of KEEP_LOCAL when this install would first overwrite a differing local copy."""
    return [t.rel for t in changes
            if t.rel == KEEP_LOCAL and t.status == "modified" and not kept_copies(config, t.rel)]


def keep_local(config, rel, force=False):
    """Copy config/rel to <name>.local-<UTC time>[-n].bak beside it and return that path.

    None when a kept copy already exists (unless force). An existing name is never overwritten.
    """
    path = config / rel
    if not force and kept_copies(config, rel):
        return None
    data, stamp = path.read_bytes(), _stamp()
    for n in range(100):
        dest = path.with_name(f"{path.name}.local-{stamp}" + (f"-{n}" if n else "") + ".bak")
        try:
            stream = open(dest, "xb")
        except FileExistsError:
            continue
        try:
            with stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            dest.unlink(missing_ok=True)  # a torn copy must not count as the kept one
            raise
        return dest
    raise InstallError(f"could not create a unique local copy beside {rel}")


def backup_dir(config):
    stamp = _stamp()
    root = config / "provost/backups"
    root.mkdir(parents=True, exist_ok=True)
    for n in range(100):
        path = root / (f"install-{stamp}" + (f"-{n}" if n else ""))
        try:
            path.mkdir()
            return path
        except FileExistsError:
            continue
    raise InstallError("could not create a unique backup dir")


def missing_dirs(config, path):
    """Ancestors of path (inside config) that do not exist yet, shallowest first."""
    out, node = [], path.parent
    while node != config and not node.exists():
        out.append(node)
        node = node.parent
    return out[::-1]


def install(config, changes):
    backup = None
    changed = [t.rel for t in changes if (config / t.rel).exists()]
    created = [t.rel for t in changes if t.rel not in changed]
    kept = []
    try:
        backup = backup_dir(config)
        for rel in changed:
            dest = backup / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(config / rel, dest)
        for rel in to_keep(config, changes):
            kept.append(keep_local(config, rel).relative_to(config).as_posix())
        manifest = {"schema_version": 1, "changed": changed, "created": created, "kept_local": kept}
        atomic_write(backup / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode("utf-8"))
    except (OSError, InstallError) as exc:  # InstallError: no free name for the kept copy
        drop_kept(config, kept)  # the local file is unchanged, so its kept copy is only a duplicate
        # Nothing managed has been written yet. The partial backup dir is kept (an error path should
        # not delete anything) and named, so the user can see it holds only unfinished copies.
        left = (f"; partial backup dir {backup} has no manifest and can be deleted" if backup
                else "")
        raise InstallError(f"backup failed ({exc}); no managed file was changed{left}") from exc
    written, new_dirs = [], []
    try:
        for target in changes:
            path = config / target.rel
            for directory in missing_dirs(config, path):
                directory.mkdir()
                new_dirs.append(directory)
            ref = path if target.policy or target.source is None else target.source
            mode = stat.S_IMODE(ref.stat().st_mode) if ref.exists() else None
            # Record first: an interrupt after os.replace must still roll this target back.
            # Rolling back a target that was never written is harmless (restore or unlink a no-op).
            written.append(target.rel)
            atomic_write(path, target.desired, mode)
    except BaseException as exc:  # includes KeyboardInterrupt: never leave a half-written config
        failed = rollback(config, backup, written, set(changed), new_dirs) + drop_kept(config, kept)
        reason = str(exc) if isinstance(exc, OSError) else type(exc).__name__
        if failed:
            outcome = f"rollback incomplete: {', '.join(failed)}; backup kept at {backup}"
        else:
            outcome = f"all changes rolled back; backup at {backup}"
        message = f"install failed ({reason}); {outcome}"
        if isinstance(exc, OSError):
            raise InstallError(message) from exc
        print(f"install.py: {message}", file=sys.stderr)
        raise
    return backup, changed, created, kept


def drop_kept(config, kept):
    """Remove the local copies this run kept (the file they copy is back in place); failures listed."""
    failed = []
    for rel in kept:
        try:
            (config / rel).unlink(missing_ok=True)
        except OSError:
            failed.append(rel)
    return failed


def rollback(config, backup, written, changed, new_dirs):
    failed = []
    for rel in reversed(written):
        try:
            if rel in changed:
                shutil.copy2(backup / rel, config / rel)
            else:
                (config / rel).unlink(missing_ok=True)
        except OSError:
            failed.append(rel)
    for directory in reversed(new_dirs):
        try:
            directory.rmdir()
        except OSError:
            failed.append(directory.relative_to(config).as_posix() + "/")
    return failed


def parse(argv):
    parser = argparse.ArgumentParser(prog="install.py", description=__doc__.splitlines()[0])
    parser.add_argument("command", nargs="?", choices=("install", "check", "setup"), default="check")
    parser.add_argument("--dry-run", action="store_true",
                        help="install, setup: print the plan, write nothing")
    parser.add_argument("--config-dir", help="default: $CLAUDE_CONFIG_DIR or ~/.claude")
    parser.add_argument("--source", help="default: the directory containing install.py")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    if args.dry_run and args.command == "check":
        parser.error("--dry-run applies only to install (check is always read-only)")
    return args


def emit(args, payload, lines):
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print("\n".join(lines))


def run_setup(args, config, changes, report):
    would = "would " if args.dry_run else ""
    steps = ([f"{would}add the claudex-native markers to {POLICY_TARGET}"] if report["markers"] else [])
    steps += [f"{would}register the {event} hook in {SETTINGS}" for event in report["register"]]
    if report["runtime_config"] == "create":
        steps.append(f"{would}create {RUNTIME_CONFIG} (adaptive mode, handoff on)")
    plan = [{"path": t.rel, "action": "create" if t.status == "missing" else "update"} for t in changes]
    payload = {"command": "setup", "dry_run": args.dry_run, **report, "plan": plan, "backup": None}
    lines = steps + [f"{would}{p['action']:<6} {p['path']}" for p in plan]
    if args.dry_run or not changes:
        emit(args, payload, lines or ["nothing to set up: already installed and registered"])
        return 0
    backup, changed, created, _ = install(config, changes)
    payload.update(backup=str(backup), changed=changed, created=created)
    doctor = f'bash "{(config / RUNTIME).as_posix()}/hooks/runtime" doctor'
    emit(args, payload, lines + [f"set up: {len(changed)} updated, {len(created)} created",
                                 f"backup: {backup}",
                                 f"next: restart Claude Code, then run {doctor}"])
    return 0


def main(argv=None):
    args = parse(argv)
    config = Path(args.config_dir or os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").absolute()
    source = Path(args.source).absolute() if args.source else Path(__file__).resolve().parent
    try:
        targets = source_targets(source)
        if args.command == "setup":
            policy, setup_extra, report = setup_targets(config)
            check_layout(source, config, targets + setup_extra)
            evaluate(config, targets, policy)
            # settings and runtime config before the policy section, which stays the last write
            changes = ([t for t in targets if t.status != "ok" and not t.policy] + setup_extra
                       + [t for t in targets if t.status != "ok" and t.policy])
            return run_setup(args, config, changes, report)
        check_layout(source, config, targets)
        evaluate(config, targets)
        changes = [t for t in targets if t.status != "ok"]
        if args.command == "check":
            extra = extras(config, targets)
            settings = settings_status(config)
            payload = {"command": "check", "drift": bool(changes), "settings": settings, "extra": extra,
                       "targets": [{"path": t.rel, "status": t.status} for t in targets]}
            lines = [f"{t.status:<9} {t.rel}" for t in targets] + [f"{'extra':<9} {rel}" for rel in extra]
            lines += [f"settings  {settings}", f"drift: {'yes' if changes else 'no'}"]
            emit(args, payload, lines)
            return 1 if changes else 0
        plan = [{"path": t.rel, "action": "create" if t.status == "missing" else "update"} for t in changes]
        if args.dry_run or not changes:
            keep = to_keep(config, changes)
            payload = {"command": "install", "dry_run": args.dry_run, "plan": plan, "backup": None,
                       "keep_local": keep}
            lines = [f"{'would ' if args.dry_run else ''}{p['action']:<6} {p['path']}" for p in plan]
            lines += [f"would keep local {rel} beside it as {Path(rel).name}.local-<UTC time>.bak" for rel in keep]
            emit(args, payload, lines or ["nothing to install: no drift"])
            return 0
        backup, changed, created, kept = install(config, changes)
        payload = {"command": "install", "dry_run": False, "plan": plan, "backup": str(backup),
                   "changed": changed, "created": created, "kept_local": kept}
        emit(args, payload, [f"{p['action']:<6} {p['path']}" for p in plan]
             + [f"kept local copy: {rel}" for rel in kept]
             + [f"installed: {len(changed)} updated, {len(created)} created", f"backup: {backup}"])
        return 0
    except (InstallError, OSError) as exc:
        print(f"install.py: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
