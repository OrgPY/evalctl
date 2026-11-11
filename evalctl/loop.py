"""Loop switches from `.evalctl.yaml`: max_rounds, lint, judge_repeat, repeat, and profiles.

    config: forum_scrape
    profile: daily
    loop:
      max_rounds: 3
      lint:         {when: config_changed, block_on: critical}
      judge_repeat: {n: 3, agree: 2, when: final_round, unstable: exclude}
      repeat:       {k: 3, when: never, compare: [numbers, check_results], on_disagree: escalate}
    profiles:
      high_stakes: {judge_repeat: {when: always}, repeat: {when: on_pass}}

The profile comes from --profile, then $EVALCTL_PROFILE, then the `profile:` field. A profile
overrides single keys of the `loop` section. Without a `loop` section every switch is off.
"""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from .config import ConfigError

LINT_WHEN = ("never", "config_changed", "always")
LINT_BLOCK = ("critical", "warning", "never")
JUDGE_WHEN = ("always", "final_round", "calibrate_only")
UNSTABLE = ("exclude", "fail")
REPEAT_WHEN = ("never", "on_pass")
COMPARE = ("numbers", "check_results")
ON_DISAGREE = ("escalate", "fail")

OFF: Dict[str, Any] = {
    "max_rounds": None,
    "lint": {"when": "never", "block_on": "critical"},
    "judge_repeat": {"n": 1, "agree": 1, "when": "calibrate_only", "unstable": "exclude"},
    "repeat": {"k": 3, "when": "never", "compare": ["numbers", "check_results"], "on_disagree": "escalate"},
}
# What a section means once it is written down but a key is left out.
WRITTEN: Dict[str, Dict[str, Any]] = {
    "lint": {"when": "config_changed"},
    "judge_repeat": {"n": 3, "when": "final_round"},
    "repeat": {},
}


@dataclass
class Loop:
    profile: Optional[str] = None
    max_rounds: Optional[int] = None
    lint: Dict[str, Any] = field(default_factory=lambda: dict(OFF["lint"]))
    judge_repeat: Dict[str, Any] = field(default_factory=lambda: dict(OFF["judge_repeat"]))
    repeat: Dict[str, Any] = field(default_factory=lambda: copy.deepcopy(OFF["repeat"]))

    def switches(self) -> Dict[str, Any]:
        """The resolved values, as they go into the receipt."""
        return {"lint": dict(self.lint), "judge_repeat": dict(self.judge_repeat), "repeat": dict(self.repeat)}

    @property
    def lint_on(self) -> bool:
        return self.lint["when"] != "never"

    @property
    def judge_repeat_on(self) -> bool:
        return self.judge_repeat["n"] > 1 and self.judge_repeat["when"] in ("always", "final_round")

    @property
    def repeat_on(self) -> bool:
        return self.repeat["when"] == "on_pass"


def _choice(where: str, key: str, value: Any, allowed: tuple) -> None:
    if value not in allowed:
        raise ConfigError(f"{where}: `{key}` must be one of {' | '.join(allowed)} (got {value!r})")


def _posint(where: str, key: str, value: Any) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ConfigError(f"{where}: `{key}` must be a positive integer (got {value!r})")


def _merge(base: Dict[str, Any], over: Any, where: str, seen: set) -> Dict[str, Any]:
    if over is None:
        return base
    if not isinstance(over, dict):
        raise ConfigError(f"{where}: expected a mapping")
    unknown = set(over) - set(OFF)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s): {', '.join(sorted(unknown))} "
                          f"(allowed: {', '.join(OFF)})")
    out = copy.deepcopy(base)
    for key, value in over.items():
        if key == "max_rounds":
            _posint(where, key, value)
            out[key] = value
            continue
        if not isinstance(value, dict):
            raise ConfigError(f"{where}.{key}: expected a mapping")
        bad = set(value) - set(OFF[key])
        if bad:
            raise ConfigError(f"{where}.{key}: unknown key(s): {', '.join(sorted(bad))} "
                              f"(allowed: {', '.join(OFF[key])})")
        if key not in seen:  # first time this section is written: its documented defaults apply
            seen.add(key)
            out[key].update(WRITTEN[key])
        out[key].update(value)
    return out


def _finish(d: Dict[str, Any], where: str, agree_given: bool) -> None:
    lint, jr, rp = d["lint"], d["judge_repeat"], d["repeat"]
    _choice(f"{where}.lint", "when", lint["when"], LINT_WHEN)
    _choice(f"{where}.lint", "block_on", lint["block_on"], LINT_BLOCK)
    _posint(f"{where}.judge_repeat", "n", jr["n"])
    if not agree_given:
        jr["agree"] = jr["n"] // 2 + 1  # majority
    _posint(f"{where}.judge_repeat", "agree", jr["agree"])
    if jr["agree"] > jr["n"]:
        raise ConfigError(f"{where}.judge_repeat: `agree` ({jr['agree']}) cannot exceed `n` ({jr['n']})")
    if jr["agree"] * 2 <= jr["n"]:
        raise ConfigError(f"{where}.judge_repeat: `agree` ({jr['agree']}) must be more than half of `n` "
                          f"({jr['n']}), otherwise pass and fail could both be adopted")
    _choice(f"{where}.judge_repeat", "when", jr["when"], JUDGE_WHEN)
    _choice(f"{where}.judge_repeat", "unstable", jr["unstable"], UNSTABLE)
    _posint(f"{where}.repeat", "k", rp["k"])
    _choice(f"{where}.repeat", "when", rp["when"], REPEAT_WHEN)
    _choice(f"{where}.repeat", "on_disagree", rp["on_disagree"], ON_DISAGREE)
    compare = rp["compare"] if isinstance(rp["compare"], list) else [rp["compare"]]
    for c in compare:
        _choice(f"{where}.repeat", "compare", c, COMPARE)
    if not compare:
        raise ConfigError(f"{where}.repeat: `compare` needs at least one of {', '.join(COMPARE)}")
    rp["compare"] = compare


def resolve(pointer_data: Optional[Dict[str, Any]], profile_arg: Optional[str] = None,
            where: str = ".evalctl.yaml") -> Loop:
    data = pointer_data or {}
    seen: set = set()
    base = _merge(copy.deepcopy(OFF), data.get("loop"), f"{where}: loop", seen)
    profiles = data.get("profiles") or {}
    if not isinstance(profiles, dict):
        raise ConfigError(f"{where}: `profiles` must be a mapping of profile name -> overrides")
    own = data.get("profile")
    if own is not None and not isinstance(own, str):
        raise ConfigError(f"{where}: `profile` must be a name")
    name = profile_arg or os.environ.get("EVALCTL_PROFILE") or own
    agree_given = "agree" in ((data.get("loop") or {}).get("judge_repeat") or {})
    if name:
        if name in profiles:
            base = _merge(base, profiles[name], f"{where}: profiles.{name}", seen)
            over = (profiles[name] or {}).get("judge_repeat") or {}
            if "agree" in over:
                agree_given = True
            elif "n" in over:
                agree_given = False
        elif name != own:
            raise ConfigError(f"profile '{name}' is not defined in {where} "
                              f"(defined: {', '.join(sorted(profiles)) or 'none'}"
                              + (f"; base profile: {own}" if own else "") + ")")
    _finish(base, where, agree_given)
    return Loop(profile=name, max_rounds=base["max_rounds"], lint=base["lint"],
                judge_repeat=base["judge_repeat"], repeat=base["repeat"])


def from_pointer(pointer: Optional[Path], profile_arg: Optional[str] = None) -> Loop:
    from .config import pointer_data
    return resolve(pointer_data(pointer) if pointer else None, profile_arg, str(pointer or ".evalctl.yaml"))
