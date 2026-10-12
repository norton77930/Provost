#!/usr/bin/env python3
"""Install this repository's Claude Code setup into a config dir, or report drift.

python install.py [install|check] [--dry-run] [--config-dir DIR] [--source DIR] [--json]

check (default) is read-only: exit 0 no drift, 1 drift, 2 error (--dry-run is rejected with check).
install writes only changed targets, after backing them up: exit 0 success, 2 error.
An error or interrupt during install rolls back and names the backup dir.
Stdlib only; never reads or prints settings.json values beyond the hook commands it inspects.
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
HOOKS = ("_python.sh", "runtime", "native-event", "native-facts")
POLICY_SOURCE = "native/CLAUDE.native.md"
POLICY_TARGET = "CLAUDE.md"
BEGIN = b"<!-- claudex-native:begin -->"
END = b"<!-- claudex-native:end -->"
HOOK_EVENTS = {"SessionStart": f"{RUNTIME}/hooks/native-event",
               "PostToolUse": f"{RUNTIME}/hooks/native-facts"}


class InstallError(Exception):
    pass


@dataclass
class Target:
    rel: str           # posix path relative to the config dir
    source: Path
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
    # skills/handoff/ is deliberately never installed: the user keeps a different local version.
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
    if not policy.read_bytes().strip():  # installing it would silently empty the user's section
        raise InstallError(f"source file is empty: {POLICY_SOURCE}")
    targets.append(Target(POLICY_TARGET, policy, policy=True))
    return targets


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


def evaluate(config, targets):
    for target in targets:
        path = config / target.rel
        if target.policy:
            if not path.is_file():
                raise InstallError(f"{POLICY_TARGET} missing in config dir: markers required")
            current = path.read_bytes()
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
        data = json.loads((config / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unknown"
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return "not-registered"
    for event, suffix in HOOK_EVENTS.items():
        if not any(_ends_with(text, suffix) for text in _hook_strings(hooks.get(event))):
            return "not-registered"
    return "ok"


def _hook_strings(groups):
    for group in groups if isinstance(groups, list) else []:
        entries = group.get("hooks") if isinstance(group, dict) else None
        for hook in entries if isinstance(entries, list) else []:
            if not isinstance(hook, dict):
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


def atomic_write(path, data, mode=None):
    """Replace path atomically: temp file in the same dir, fsync, os.replace."""
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
        os.replace(temp, path)
    finally:
        if temp is not None:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass  # best effort: never mask the write result or its error


def backup_dir(config):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
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
    try:
        backup = backup_dir(config)
        for rel in changed:
            dest = backup / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(config / rel, dest)
        manifest = {"schema_version": 1, "changed": changed, "created": created}
        atomic_write(backup / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode("utf-8"))
    except OSError as exc:
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
            mode = stat.S_IMODE((path if target.policy else target.source).stat().st_mode)
            # Record first: an interrupt after os.replace must still roll this target back.
            # Rolling back a target that was never written is harmless (restore or unlink a no-op).
            written.append(target.rel)
            atomic_write(path, target.desired, mode)
    except BaseException as exc:  # includes KeyboardInterrupt: never leave a half-written config
        failed = rollback(config, backup, written, set(changed), new_dirs)
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
    return backup, changed, created


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
    parser.add_argument("command", nargs="?", choices=("install", "check"), default="check")
    parser.add_argument("--dry-run", action="store_true", help="install: print the plan, write nothing")
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


def main(argv=None):
    args = parse(argv)
    config = Path(args.config_dir or os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").absolute()
    source = Path(args.source).absolute() if args.source else Path(__file__).resolve().parent
    try:
        targets = source_targets(source)
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
            payload = {"command": "install", "dry_run": args.dry_run, "plan": plan, "backup": None}
            lines = [f"{'would ' if args.dry_run else ''}{p['action']:<6} {p['path']}" for p in plan]
            emit(args, payload, lines or ["nothing to install: no drift"])
            return 0
        backup, changed, created = install(config, changes)
        payload = {"command": "install", "dry_run": False, "plan": plan, "backup": str(backup),
                   "changed": changed, "created": created}
        emit(args, payload, [f"{p['action']:<6} {p['path']}" for p in plan]
             + [f"installed: {len(changed)} updated, {len(created)} created", f"backup: {backup}"])
        return 0
    except (InstallError, OSError) as exc:
        print(f"install.py: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
