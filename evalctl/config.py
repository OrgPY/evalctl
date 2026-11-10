"""Find and load a check config.

The working directory only holds a one-line pointer (`.evalctl.yaml`:
`config: <name>`). The rules themselves live in ~/.evalctl/checks/<name>.yaml,
outside the directory the agent works in.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from . import paths


class ConfigError(Exception):
    """Anything that makes the checks impossible to run (exit code 2)."""


HOW_TO_CONFIGURE = """\
No check config selected for {dir}.

Set one up in either of these ways:
  1. Put a file named .evalctl.yaml in the working directory (or a parent) containing one line:
         config: <name>
     where ~/.evalctl/checks/<name>.yaml holds the check rules.
  2. Pass it explicitly:  evalctl check --config <name>

Available configs in {checks_dir}: {available}"""


@dataclass
class CheckSpec:
    id: str
    type: str
    params: Dict[str, Any]
    hint: Optional[str] = None


@dataclass
class Config:
    name: str
    path: Path
    sha256: str
    hashed_files: List[str]
    checks: List[CheckSpec]
    artifacts: Optional[List[str]] = None
    max_rounds: int = 3
    judge: Any = None
    worker_family: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


def _load_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: invalid YAML: {e}")
    except OSError as e:
        raise ConfigError(f"{path}: cannot read: {e}")


def find_pointer(start: Path) -> Optional[Path]:
    """Return the nearest .evalctl.yaml in `start` or its parents."""
    start = start.resolve()
    for d in [start, *start.parents]:
        p = d / paths.POINTER_NAME
        if p.is_file():
            return p
    return None


def read_pointer(pointer: Path) -> str:
    data = _load_yaml(pointer)
    if not isinstance(data, dict) or not isinstance(data.get("config"), str) or not data["config"].strip():
        raise ConfigError(f"{pointer}: expected a single line like `config: <name>`")
    return data["config"].strip()


def _available() -> str:
    d = paths.checks_dir()
    names = sorted(p.stem for p in d.glob("*.yaml")) if d.is_dir() else []
    return ", ".join(names) if names else "(none yet)"


def resolve_config_path(name_or_path: str) -> Path:
    """A bare name maps to ~/.evalctl/checks/<name>.yaml; anything else is a path."""
    s = name_or_path
    if "/" in s or s.endswith((".yaml", ".yml")):
        p = Path(s).expanduser()
        if not p.is_file():
            raise ConfigError(f"check config not found: {p}")
        return p.resolve()
    p = paths.checks_dir() / f"{s}.yaml"
    if not p.is_file():
        raise ConfigError(
            f"check config '{s}' not found at {p}\nAvailable configs: {_available()}"
        )
    return p.resolve()


def select(dir_arg: Optional[str], config_arg: Optional[str]) -> Tuple[Path, Path, Optional[Path]]:
    """Decide (workdir, config_path, pointer).

    --config wins. Otherwise the nearest .evalctl.yaml decides, and when --dir
    was not given the directory holding that file becomes the workdir (so the
    command works from a subdirectory, like git).
    """
    start = Path(dir_arg).expanduser() if dir_arg else Path.cwd()
    if not start.is_dir():
        raise ConfigError(f"not a directory: {start}")
    start = start.resolve()
    pointer = find_pointer(start)
    workdir = start if (dir_arg or pointer is None) else pointer.parent
    if config_arg:
        return workdir, resolve_config_path(config_arg), pointer
    if pointer is None:
        raise ConfigError(
            HOW_TO_CONFIGURE.format(dir=start, checks_dir=paths.checks_dir(), available=_available())
        )
    return workdir, resolve_config_path(read_pointer(pointer)), pointer


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def load(config_path: Path, workdir: Path) -> Config:
    from .checks import REGISTRY  # late import: checks import this module

    config_path = config_path.resolve()
    if _inside(config_path, workdir):
        raise ConfigError(
            f"check config {config_path} is inside the working directory {workdir}.\n"
            f"Configs must live outside it (default {paths.checks_dir()}) so the agent being "
            "checked cannot edit its own rules."
        )
    data = _load_yaml(config_path)
    if not isinstance(data, dict):
        raise ConfigError(f"{config_path}: top level must be a mapping")
    known = {"name", "description", "artifacts", "max_rounds", "judge", "worker_family", "checks"}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"{config_path}: unknown top-level key(s): {', '.join(sorted(unknown))}")

    raw_checks = data.get("checks")
    if not isinstance(raw_checks, list) or not raw_checks:
        raise ConfigError(f"{config_path}: `checks` must be a non-empty list")

    artifacts = data.get("artifacts")
    if artifacts is not None:
        if isinstance(artifacts, str):
            artifacts = [artifacts]
        if not isinstance(artifacts, list) or not all(isinstance(a, str) for a in artifacts):
            raise ConfigError(f"{config_path}: `artifacts` must be a list of glob patterns")

    max_rounds = data.get("max_rounds", 3)
    if not isinstance(max_rounds, int) or max_rounds < 1:
        raise ConfigError(f"{config_path}: `max_rounds` must be a positive integer")

    checks: List[CheckSpec] = []
    seen = set()
    extra_files: List[Path] = []
    for i, rc in enumerate(raw_checks, 1):
        where = f"{config_path}: checks[{i}]"
        if not isinstance(rc, dict) or "type" not in rc:
            raise ConfigError(f"{where}: each check needs a `type`")
        ctype = rc["type"]
        if ctype not in REGISTRY:
            raise ConfigError(
                f"{where}: unknown check type '{ctype}' (supported: {', '.join(sorted(REGISTRY))})"
            )
        cid = str(rc.get("id") or f"{ctype}_{i}")
        if cid in seen:
            raise ConfigError(f"{where}: duplicate check id '{cid}'")
        seen.add(cid)
        params = {k: v for k, v in rc.items() if k not in ("id", "type", "hint")}
        spec = CheckSpec(id=cid, type=ctype, params=params, hint=rc.get("hint"))
        try:
            extra = REGISTRY[ctype].validate(spec, config_path.parent, workdir)
        except ConfigError as e:
            raise ConfigError(f"{where} (id={cid}): {e}")
        extra_files.extend(extra or [])
        checks.append(spec)

    # The config hash covers the YAML and any python check modules it loads.
    h = hashlib.sha256()
    hashed = [config_path, *sorted(set(extra_files))]
    for p in hashed:
        b = p.read_bytes()
        h.update(str(len(b)).encode() + b"\0" + b)

    return Config(
        name=str(data.get("name") or config_path.stem),
        path=config_path,
        sha256=h.hexdigest(),
        hashed_files=[str(p) for p in hashed],
        checks=checks,
        artifacts=artifacts,
        max_rounds=max_rounds,
        judge=data.get("judge"),
        worker_family=data.get("worker_family"),
        raw=data,
    )
