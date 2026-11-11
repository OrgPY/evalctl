from __future__ import annotations

import glob as _glob
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from ..config import CheckSpec, Config, ConfigError

MAX_LISTED = 25  # failure lines shown per check before "... and N more"

PASS, FAIL, ERROR, SKIPPED = "pass", "fail", "error", "skipped"


@dataclass
class Result:
    id: str
    type: str
    status: str  # pass | fail | error | skipped
    summary: str = ""
    failures: List[str] = field(default_factory=list)
    hint: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = {"id": self.id, "type": self.type, "status": self.status, "summary": self.summary}
        if self.failures:
            d["failures"] = self.failures
        if self.hint:
            d["hint"] = self.hint
        if self.meta:
            d["meta"] = self.meta
        return d


@dataclass
class Context:
    workdir: Path
    config: Config
    worker: Optional[str] = None
    inputs: Optional[Path] = None  # the task's input files, for input/output relation checks
    trajectories: List[Path] = field(default_factory=list)  # the agent's tool-call logs, oldest first


class Check:
    """One check type. `validate` runs at config load, `run` at check time."""

    type = ""
    allowed: Iterable[str] = ()
    required: Iterable[str] = ()

    @classmethod
    def validate(cls, spec: CheckSpec, config_dir: Path, workdir: Path) -> List[Path]:
        unknown = set(spec.params) - set(cls.allowed)
        if unknown:
            raise ConfigError(
                f"unknown option(s) for {cls.type}: {', '.join(sorted(unknown))} "
                f"(allowed: {', '.join(sorted(cls.allowed))})"
            )
        missing = [k for k in cls.required if spec.params.get(k) in (None, "", [])]
        if missing:
            raise ConfigError(f"{cls.type} check needs: {', '.join(missing)}")
        return cls.validate_extra(spec, config_dir, workdir) or []

    @classmethod
    def validate_extra(cls, spec: CheckSpec, config_dir: Path, workdir: Path) -> List[Path]:
        return []

    @classmethod
    def run(cls, spec: CheckSpec, ctx: Context) -> Result:
        raise NotImplementedError


def result(spec: CheckSpec, failures: List[str], ok_summary: str, fail_summary: str = "") -> Result:
    if failures:
        return Result(spec.id, spec.type, FAIL, fail_summary or f"{len(failures)} problem(s)",
                      failures, spec.hint)
    return Result(spec.id, spec.type, PASS, ok_summary)


def error(spec: CheckSpec, message: str) -> Result:
    return Result(spec.id, spec.type, ERROR, message, [message])


def match_files(workdir: Path, pattern: str) -> List[Path]:
    """Glob relative to workdir; never returns anything under .eval/."""
    hits = _glob.glob(os.path.join(_glob.escape(str(workdir)), pattern), recursive=True)
    out = []
    for h in sorted(hits):
        p = Path(h)
        rel = p.relative_to(workdir)
        if rel.parts and rel.parts[0] == ".eval":
            continue
        out.append(p)
    return out


def rel(workdir: Path, p: Path) -> str:
    try:
        return str(p.relative_to(workdir))
    except ValueError:
        return str(p)


def as_list(v: Any) -> List[Any]:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def clip(s: str, n: int = 120) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"
