"""Personal configuration; never inspect Claude settings or credentials."""

import hashlib
import json
import os
from pathlib import Path


class ConfigError(ValueError):
    pass


def config_path() -> Path:
    return Path(os.environ.get("PROVOST_CONFIG", str(Path.home() / ".claude/provost/config.json")))


def normalized(root: Path) -> str:
    return os.path.normcase(str(root.resolve()))


def workspace_key(root: Path) -> str:
    return hashlib.sha256(normalized(root).encode("utf-8")).hexdigest()


def load_config(path: Path) -> dict:
    if not path.exists():
        return {"schema_version": 1, "mode": "legacy"}
    try:
        cfg = json.loads(path.read_text(encoding="utf-8-sig"))
        keys = {"schema_version", "mode", "workspace_roots", "all_workspaces",
                "max_writers", "burst_writers", "handoff"}
        if not isinstance(cfg, dict) or set(cfg) - keys:
            raise ConfigError("INVALID_CONFIG")
        if type(cfg.get("schema_version")) is not int or cfg["schema_version"] != 1:
            raise ConfigError("UNSUPPORTED_CONFIG_SCHEMA")
        if cfg.get("mode") not in ("legacy", "adaptive"):
            raise ConfigError("INVALID_MODE")
        result = {"workspace_roots": [], "all_workspaces": False,
                  "max_writers": 2, "burst_writers": 3, "handoff": True, **cfg}
        for key in ("all_workspaces", "handoff"):
            if type(result[key]) is not bool:
                raise ConfigError("INVALID_CONFIG_TYPE")
        for key in ("max_writers", "burst_writers"):
            if type(result[key]) is not int:
                raise ConfigError("INVALID_WRITER_LIMIT")
        if not 1 <= result["max_writers"] <= result["burst_writers"] <= 3:
            raise ConfigError("INVALID_WRITER_LIMIT")
        roots = result["workspace_roots"]
        if not isinstance(roots, list) or any(not isinstance(p, str) or not Path(p).is_absolute() for p in roots):
            raise ConfigError("INVALID_WORKSPACE_ROOTS")
        return result
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError("CONFIG_UNREADABLE") from exc


def resolve_mode(config: dict, workspace: Path) -> str:
    if config.get("mode") != "adaptive":
        return "legacy"
    if config.get("all_workspaces") is True:
        return "adaptive"
    current = normalized(workspace)
    return "adaptive" if any(normalized(Path(p)) == current for p in config.get("workspace_roots", [])) else "legacy"
