"""~/.evalctl/settings.yaml: the few choices that are yours and apply to every directory."""
from __future__ import annotations

from typing import Any, Dict

import yaml

from . import paths
from .config import ConfigError

TEMPLATE = """\
# evalctl settings (all directories).

# A task whose text contains one of these words is run under that profile, unless --profile or
# $EVALCTL_PROFILE says otherwise. Profiles `daily` (contract + check) and `high_stakes`
# (adds repeat, k=3) are built in; a .evalctl.yaml can redefine them.
triggers:
  high_stakes: ["重要", "important", "high-stakes", "high stakes"]

# Agent adapter (from agents.yaml) that `evalctl repeat` starts its independent runs with when
# --agent is not given. Empty: the agent that is calling (Claude Code -> claude).
default_agent: ""

# The Claude Code prompt hook writes an acceptance contract for prompts at least this long
# (shorter ones are follow-ups like "yes, go on"), in directories that have a .evalctl.yaml or
# for prompts that contain a trigger word.
contract_min_chars: 40
"""
DEFAULTS: Dict[str, Any] = {"triggers": {"high_stakes": ["重要", "important", "high-stakes", "high stakes"]},
                            "default_agent": "", "contract_min_chars": 40}


def settings_path():
    return paths.home() / "settings.yaml"


def load(create: bool = False) -> Dict[str, Any]:
    p = settings_path()
    if not p.is_file():
        if create:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(TEMPLATE, encoding="utf-8")
        return dict(DEFAULTS)
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{p}: invalid YAML: {e}")
    if not isinstance(data, dict):
        raise ConfigError(f"{p}: expected a mapping")
    unknown = set(data) - set(DEFAULTS)
    if unknown:
        raise ConfigError(f"{p}: unknown key(s): {', '.join(sorted(unknown))} (allowed: {', '.join(DEFAULTS)})")
    out = dict(DEFAULTS, **data)
    if not isinstance(out["triggers"], dict) or not all(isinstance(v, list) for v in out["triggers"].values()):
        raise ConfigError(f"{p}: `triggers` maps a profile name to a list of words")
    return out
