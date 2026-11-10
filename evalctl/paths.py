"""Locations of everything evalctl keeps outside the agent's working directory."""
from __future__ import annotations

import os
from pathlib import Path

POINTER_NAME = ".evalctl.yaml"
EVAL_DIR = ".eval"
RECEIPT_NAME = "receipt.json"


def home() -> Path:
    """~/.evalctl, or $EVALCTL_HOME (used by the tests).

    Pointing EVALCTL_HOME somewhere else does not help an agent cheat: the
    receipt would be signed with a different key and fail `evalctl verify`
    run from the real home.
    """
    return Path(os.environ.get("EVALCTL_HOME") or Path.home() / ".evalctl").expanduser()


def checks_dir() -> Path:
    return home() / "checks"


def key_path() -> Path:
    return home() / "key"


def db_path() -> Path:
    return home() / "runs.sqlite"


def judge_path() -> Path:
    return home() / "judge.yaml"


def receipt_path(workdir: Path) -> Path:
    return workdir / EVAL_DIR / RECEIPT_NAME
