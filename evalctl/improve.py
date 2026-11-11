"""`evalctl improve`: what the accumulated record says should change in a check config.

A manual tool. It is never part of a delivery: it reads what many tasks left behind, proposes
changes to the config, and applies only the ones you approve.

Evidence it reads (all per config):
  * your `feedback` verdicts: checks you overruled, outputs you judged bad although they passed
  * repeated gradings: rubric criteria whose gradings do not agree with each other
  * stored `explain` results: checks blamed for false positives, and recurring causes of failure
  * your pairwise judgements: how often the llm_rubric checks agreed with you
  * learned candidates that are waiting for `review`, and the last `lint` result

Proposals that remove something (a check, a rubric criterion) can be applied here after your
approval; the previous config is kept as <name>.yaml.bak. Everything else names the command to run.
"""
from __future__ import annotations

import json
import shutil
import time
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from . import config as cfg, paths, store
from .config import ConfigError

OVERRULED_MIN = 2
CONSISTENCY_MAX = 0.75
CONSISTENCY_RUNS = 3


def improve_path(name: str) -> Path:
    return paths.home() / "improve" / f"{name}.yaml"


def _explanations(name: str) -> List[Dict[str, Any]]:
    from . import explain
    explain.explanations_table()
    with closing(store.connect()) as conn:
        rows = conn.execute("SELECT e.data FROM explanations e JOIN runs r ON r.id = e.run_id WHERE r.config=?", (name,)).fetchall()
    return [json.loads(r["data"]) for r in rows]


def propose(name: str) -> List[Dict[str, Any]]:
    cpath = cfg.resolve_config_path(name)
    raw = cfg._load_yaml(cpath)
    checks = {str(c.get("id")): c for c in raw.get("checks") or [] if isinstance(c, dict)}
    out: List[Dict[str, Any]] = []

    def add(action: str, target: Optional[str], why: str, evidence: str, command: Optional[str] = None) -> None:
        out.append({"id": f"P{len(out) + 1}", "action": action, "target": target, "why": why, "evidence": evidence,
                    "command": command, "status": "pending"})

    fb = store.feedback_summary(name).get(name, {})
    for cid, n in (fb.get("overruled") or {}).items():
        if n >= OVERRULED_MIN and cid in checks:
            add("remove_check", cid, f"you judged the output good {n} times although '{cid}' failed it",
                f"{n} `evalctl feedback --verdict good` on outputs that failed this check")
    if fb.get("checks_too_lenient"):
        add("advice", None, f"{fb['checks_too_lenient']} output(s) passed every check and you judged them bad: the checks have a blind spot",
            "`evalctl feedback --verdict bad` on passing outputs",
            f"fix one of those outputs by hand, then: evalctl learn --draft <saved copy> --final <fixed> --name {name}")

    stats = store.stats(name)["configs"].get(name, {})
    for item, c in (stats.get("rubric_consistency") or {}).items():
        cid, _, crit = item.partition(".")
        if cid in checks and c["runs"] >= CONSISTENCY_RUNS and (c["consistency"] <= CONSISTENCY_MAX or c["unstable_runs"] >= 2):
            add("remove_criterion", item, f"its repeated gradings agree only {c['consistency']:.0%} of the time "
                f"({c['unstable_runs']} of {c['runs']} runs had no stable verdict): the wording is too vague to grade",
                "judge_repeat votes in the run log",
                "or rewrite it with an explicit fail condition instead of removing it")

    blamed: Counter = Counter()
    causes: Counter = Counter()
    for e in _explanations(name):
        if e.get("category") == "checker_false_positive":
            blamed.update(e.get("failed_checks") or [])
        elif e.get("category"):
            causes[e["category"]] += 1
    for cid, n in blamed.items():
        if n >= 2 and cid in checks and not any(p["target"] == cid for p in out):
            add("remove_check", cid, f"`evalctl explain` found the output right and this check wrong in {n} failed runs",
                f"{n} stored explanations with cause checker_false_positive", "or narrow the check instead of removing it")
    if causes:
        top, n = causes.most_common(1)[0]
        if n >= 3:
            add("advice", None, f"the most common cause of failure is {top} ({n} explained runs)",
                "stored `evalctl explain` results", f"evalctl explain --cluster {name}   # then put what is missing into the task or the hints")

    from . import pairwise
    jd = pairwise.summary(name).get(name)
    if jd and jd["graded"] >= 3 and (jd["agreement_rate"] or 0) < 0.6:
        add("advice", None, f"the llm_rubric checks preferred the same output as you in only {jd['agreement_rate']:.0%} of "
            f"{jd['graded']} pairwise judgements", "`evalctl judge` records", f"evalctl review {name}   # the criteria made from your reasons")

    from . import learn as lrn
    if lrn.learned_path(name).is_file():
        try:
            waiting = [c["id"] for c in lrn.load(name)["candidates"] if c["enabled"] and not c.get("in_config")]
        except ConfigError:
            waiting = []
        if waiting:
            add("advice", None, f"{len(waiting)} learned candidate check(s) are enabled but not in the config yet: "
                + ", ".join(waiting[:6]), "~/.evalctl/learned", f"evalctl review {name}")
    lint_file = paths.home() / "lint" / f"{name}.json"
    if lint_file.is_file():
        try:
            findings = json.loads(lint_file.read_text(encoding="utf-8")).get("findings") or []
        except ValueError:
            findings = []
        if findings:
            add("advice", None, f"the last lint of this config has {len(findings)} finding(s)", str(lint_file), f"evalctl lint {name}")
    return out


def save(name: str, proposals: List[Dict[str, Any]]) -> Path:
    p = improve_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump({"config": name, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "proposals": proposals},
                                sort_keys=False, allow_unicode=True, width=110), encoding="utf-8")
    return p


def load(name: str) -> List[Dict[str, Any]]:
    p = improve_path(name)
    if not p.is_file():
        raise ConfigError(f"no proposals stored for '{name}'. Run `evalctl improve {name}` first")
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("proposals") or []


def apply(name: str, proposals: List[Dict[str, Any]]) -> Path:
    """Apply the approved removals. Comments in the YAML are not preserved; the old file is kept as .bak."""
    cpath = cfg.resolve_config_path(name)
    raw = cfg._load_yaml(cpath)
    checks = [c for c in raw.get("checks") or [] if isinstance(c, dict)]
    for p in proposals:
        if p["action"] == "remove_check":
            checks = [c for c in checks if str(c.get("id")) != p["target"]]
        elif p["action"] == "remove_criterion":
            cid, _, crit = p["target"].partition(".")
            for c in checks:
                if str(c.get("id")) == cid and isinstance(c.get("rubric"), list):
                    c["rubric"] = [r for i, r in enumerate(c["rubric"], 1)
                                   if (str(r.get("id") or f"c{i}") if isinstance(r, dict) else f"c{i}") != crit]
            checks = [c for c in checks if not (str(c.get("id")) == cid and c.get("type") == "llm_rubric" and not c.get("rubric"))]
    if not checks:
        raise ConfigError("applying these proposals would leave the config without any check; nothing was changed")
    raw["checks"] = checks
    backup = cpath.with_name(cpath.name + ".bak")
    shutil.copyfile(cpath, backup)
    cpath.write_text(f"# Edited by `evalctl improve {name}` on {time.strftime('%Y-%m-%d %H:%M')}; previous version: {backup.name}\n"
                     + yaml.safe_dump(raw, sort_keys=False, allow_unicode=True, width=110), encoding="utf-8")
    return backup


def render(name: str, proposals: List[Dict[str, Any]]) -> str:
    if not proposals:
        return f"evalctl improve: nothing to propose for '{name}' from the record so far."
    lines = [f"evalctl improve: {len(proposals)} proposal(s) for '{name}'. Nothing is changed until you approve.", ""]
    for p in proposals:
        what = {"remove_check": f"remove check {p['target']}", "remove_criterion": f"remove rubric criterion {p['target']}",
                "advice": "look into"}[p["action"]]
        lines.append(f"{p['id']}  {what}" + (f"  [{p['status']}]" if p["status"] != "pending" else ""))
        lines.append(f"    why: {p['why']}")
        lines.append(f"    evidence: {p['evidence']}")
        if p.get("command"):
            lines.append(f"    {'next' if p['action'] == 'advice' else 'alternative'}: {p['command']}")
    if any(p["action"] != "advice" for p in proposals):
        lines += ["", f"Approve the removals one by one:  evalctl improve {name} --apply      (or --approve P1 P2)"]
    return "\n".join(lines)
