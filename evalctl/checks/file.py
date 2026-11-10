from __future__ import annotations

import re
from pathlib import Path
from typing import Any, List

from ..config import CheckSpec, ConfigError
from .base import Check, Context, Result, match_files, rel, result

_UNITS = {"b": 1, "kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3}


def parse_size(v: Any) -> int:
    if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
        return v
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kmg]?b)\s*", str(v), re.I)
    if not m:
        raise ConfigError(f"bad size {v!r}; use bytes or e.g. 10KB, 2MB")
    return int(float(m.group(1)) * _UNITS[m.group(2).lower()])


class FileCheck(Check):
    """Existence, count, naming and size of files matching a glob.

    exists: true (default) -> at least one match; false -> no match allowed
    min_count / max_count -> number of matches
    name_regex -> every matched file name (basename) must fully match
    min_size / max_size -> per file, bytes or "10KB"
    """

    type = "file"
    allowed = ("path", "exists", "min_count", "max_count", "name_regex", "min_size", "max_size")
    required = ("path",)

    @classmethod
    def validate_extra(cls, spec: CheckSpec, config_dir: Path, workdir: Path) -> List[Path]:
        p = spec.params
        if "exists" in p and not isinstance(p["exists"], bool):
            raise ConfigError("`exists` must be true or false")
        for k in ("min_size", "max_size"):
            if p.get(k) is not None:
                parse_size(p[k])
        for k in ("min_count", "max_count"):
            if p.get(k) is not None and (not isinstance(p[k], int) or p[k] < 0):
                raise ConfigError(f"`{k}` must be a non-negative integer")
        if p.get("name_regex") is not None:
            try:
                re.compile(p["name_regex"])
            except re.error as e:
                raise ConfigError(f"bad name_regex: {e}")
        return []

    @classmethod
    def run(cls, spec: CheckSpec, ctx: Context) -> Result:
        p = spec.params
        pattern = p["path"]
        hits = [f for f in match_files(ctx.workdir, pattern) if f.is_file()]
        failures: List[str] = []
        if p.get("exists", True) is False:
            failures = [f"{rel(ctx.workdir, f)}: must not exist; delete it" for f in hits]
            return result(spec, failures, f"no file matches {pattern}")
        if not hits:
            return result(spec, [f"{pattern}: no such file; create it"], "", "file missing")
        n = len(hits)
        if p.get("min_count") is not None and n < p["min_count"]:
            failures.append(f"{pattern}: {n} file(s) match, need at least {p['min_count']}")
        if p.get("max_count") is not None and n > p["max_count"]:
            failures.append(f"{pattern}: {n} file(s) match, at most {p['max_count']} allowed")
        lo = parse_size(p["min_size"]) if p.get("min_size") is not None else None
        hi = parse_size(p["max_size"]) if p.get("max_size") is not None else None
        for f in hits:
            label = rel(ctx.workdir, f)
            if p.get("name_regex") and not re.fullmatch(p["name_regex"], f.name):
                failures.append(f"{label}: file name does not match /{p['name_regex']}/; rename it")
            size = f.stat().st_size
            if lo is not None and size < lo:
                failures.append(
                    f"{label}: is empty" if size == 0 else f"{label}: {size} bytes, minimum is {lo}"
                )
            if hi is not None and size > hi:
                failures.append(f"{label}: {size} bytes, maximum is {hi}")
        return result(spec, failures, f"{n} file(s) OK")
