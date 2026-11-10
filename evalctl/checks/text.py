from __future__ import annotations

import re
from pathlib import Path
from typing import List, Pattern

from ..config import CheckSpec, ConfigError
from .base import MAX_LISTED, Check, Context, Result, as_list, clip, match_files, rel, result

PER_PATTERN = 5  # occurrences listed per forbidden pattern per file


class _TextCheck(Check):
    """must: every pattern has to appear in each matched file. must_not: none may appear."""

    allowed = ("file", "must", "must_not", "ignore_case")
    required = ("file",)
    literal = True

    @classmethod
    def _compile(cls, pat: str, ignore_case: bool) -> Pattern[str]:
        flags = re.MULTILINE | (re.IGNORECASE if ignore_case else 0)
        return re.compile(re.escape(pat) if cls.literal else pat, flags)

    @classmethod
    def validate_extra(cls, spec: CheckSpec, config_dir: Path, workdir: Path) -> List[Path]:
        p = spec.params
        pats = as_list(p.get("must")) + as_list(p.get("must_not"))
        if not pats:
            raise ConfigError(f"{cls.type} check needs `must` and/or `must_not`")
        for pat in pats:
            if not isinstance(pat, str) or not pat:
                raise ConfigError("patterns must be non-empty strings")
            try:
                cls._compile(pat, False)
            except re.error as e:
                raise ConfigError(f"bad regex {pat!r}: {e}")
        return []

    @classmethod
    def run(cls, spec: CheckSpec, ctx: Context) -> Result:
        p = spec.params
        ic = bool(p.get("ignore_case"))
        word = "text" if cls.literal else "pattern"
        files = [f for f in match_files(ctx.workdir, p["file"]) if f.is_file()]
        if not files:
            return result(spec, [f"{p['file']}: no file matches, nothing to search"], "", "file missing")
        failures: List[str] = []
        for f in files:
            label = rel(ctx.workdir, f)
            text = f.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            for pat in as_list(p.get("must")):
                if not cls._compile(pat, ic).search(text):
                    failures.append(f"{label}: required {word} {pat!r} not found; add it")
            for pat in as_list(p.get("must_not")):
                rx = cls._compile(pat, ic)
                hits = list(rx.finditer(text))
                for m in hits[:PER_PATTERN]:
                    n = text.count("\n", 0, m.start()) + 1
                    line = lines[n - 1] if n <= len(lines) else ""
                    failures.append(
                        f"{label}:{n}: forbidden {word} {pat!r} found: \"{clip(line, 100)}\"; remove it"
                    )
                if len(hits) > PER_PATTERN:
                    failures.append(f"{label}: ... and {len(hits) - PER_PATTERN} more occurrence(s) of {pat!r}")
        count = len(failures)
        if count > MAX_LISTED:
            failures = failures[:MAX_LISTED] + [f"... and {count - MAX_LISTED} more"]
        return result(spec, failures, f"{len(files)} file(s) OK", f"{count} problem(s)")


class ContainsCheck(_TextCheck):
    type = "contains"
    literal = True


class RegexCheck(_TextCheck):
    type = "regex"
    literal = False
