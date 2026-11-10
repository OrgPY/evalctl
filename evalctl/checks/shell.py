from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import List

from ..config import CheckSpec, ConfigError
from .base import FAIL, PASS, Check, Context, Result

TAIL_LINES = 30


class ShellCheck(Check):
    """Run a command in the working directory and look at its exit code."""

    type = "shell"
    allowed = ("command", "timeout", "expect_exit")
    required = ("command",)

    @classmethod
    def validate_extra(cls, spec: CheckSpec, config_dir: Path, workdir: Path) -> List[Path]:
        p = spec.params
        if not isinstance(p["command"], str):
            raise ConfigError("`command` must be a string (it is run through the shell)")
        if not isinstance(p.get("timeout", 120), (int, float)) or p.get("timeout", 120) <= 0:
            raise ConfigError("`timeout` must be a positive number of seconds")
        if not isinstance(p.get("expect_exit", 0), int):
            raise ConfigError("`expect_exit` must be an integer")
        return []

    @classmethod
    def run(cls, spec: CheckSpec, ctx: Context) -> Result:
        p = spec.params
        cmd, timeout, want = p["command"], p.get("timeout", 120), p.get("expect_exit", 0)
        env = dict(os.environ, EVALCTL_DIR=str(ctx.workdir))
        try:
            proc = subprocess.run(
                cmd, shell=True, cwd=str(ctx.workdir), env=env, timeout=timeout,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return Result(spec.id, spec.type, FAIL, "timed out",
                          [f"`{cmd}` did not finish within {timeout}s"], spec.hint)
        if proc.returncode == want:
            return Result(spec.id, spec.type, PASS, f"`{cmd}` exited {want}")
        out = proc.stdout.decode("utf-8", errors="replace").rstrip().splitlines()
        tail = out[-TAIL_LINES:]
        failures = [f"`{cmd}` exited {proc.returncode} (expected {want}). Output"
                    + (f" (last {TAIL_LINES} lines):" if len(out) > TAIL_LINES else ":")]
        failures += [f"  {line}" for line in tail] or ["  (no output)"]
        return Result(spec.id, spec.type, FAIL, f"exit {proc.returncode}", failures, spec.hint)
