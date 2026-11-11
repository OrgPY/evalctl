"""`evalctl bench`: run a stored set of tasks through an agent and compare two such runs.

A suite:

    name: forum_small
    config: forum_scrape            # default check config for the tasks
    loop: {max_rounds: 2}           # optional, same keys as `loop:` in .evalctl.yaml
    tasks:
      - id: homelab
        prompt: Extract every post from page.html into posts.jsonl ...
        files: tasks/homelab        # copied into a fresh working directory (relative to the suite file)
        inputs: tasks/homelab       # optional: the task inputs, for checks that compare with them
        config: forum_scrape        # optional per task

Results go to ~/.evalctl/bench/<run id>/result.json; each attempt keeps its working directory
(with .eval/rounds/) next to it.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

from . import config as cfg, loop as lp, paths, runner
from .checks import FAIL, PASS
from .config import ConfigError

NAME_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.")


def bench_dir() -> Path:
    return paths.home() / "bench"


def load_suite(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"suite file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: invalid YAML: {e}")
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list) or not data["tasks"]:
        raise ConfigError(f"{path}: a suite needs a non-empty `tasks` list")
    unknown = set(data) - {"name", "config", "loop", "profile", "profiles", "tasks"}
    if unknown:
        raise ConfigError(f"{path}: unknown key(s): {', '.join(sorted(unknown))}")
    lp.resolve({k: data[k] for k in ("loop", "profile", "profiles") if k in data}, None, str(path))
    seen = set()
    tasks = []
    for i, t in enumerate(data["tasks"], 1):
        where = f"{path}: tasks[{i}]"
        if not isinstance(t, dict) or not isinstance(t.get("prompt"), str) or not t["prompt"].strip():
            raise ConfigError(f"{where}: each task needs a `prompt`")
        bad = set(t) - {"id", "prompt", "files", "inputs", "config"}
        if bad:
            raise ConfigError(f"{where}: unknown key(s): {', '.join(sorted(bad))}")
        tid = str(t.get("id") or f"task{i}")
        if tid in seen or not set(tid) <= NAME_OK:
            raise ConfigError(f"{where}: task id '{tid}' is repeated or has characters other than letters, digits, _ - .")
        seen.add(tid)
        config = t.get("config") or data.get("config")
        if not config:
            raise ConfigError(f"{where}: no check config (set `config` on the task or on the suite)")
        dirs = {}
        for key in ("files", "inputs"):
            if t.get(key):
                d = Path(str(t[key])).expanduser()
                d = d if d.is_absolute() else path.parent / d
                if not d.is_dir():
                    raise ConfigError(f"{where}: `{key}` directory not found: {d}")
                dirs[key] = d.resolve()
        tasks.append({"id": tid, "prompt": t["prompt"], "config": str(cfg.resolve_config_path(str(config))),
                      "config_name": Path(str(config)).stem, **dirs})
    return {"name": str(data.get("name") or path.stem), "path": str(path.resolve()), "tasks": tasks,
            "pointer": {k: data[k] for k in ("loop", "profile", "profiles") if k in data}}


def run_suite(suite_path: str, agent: str, repeat: Optional[int] = None, profile: Optional[str] = None,
              only: Optional[List[str]] = None, say: Callable[[str], None] = lambda s: None) -> Dict[str, Any]:
    suite = load_suite(Path(suite_path).expanduser())
    loop = lp.resolve(suite["pointer"], profile, suite["path"])
    # `--repeat K` wins; otherwise a suite whose loop asks for repeat on_pass runs each task k times.
    k = repeat or (loop.repeat["k"] if loop.repeat_on else 1)
    tasks = [t for t in suite["tasks"] if not only or t["id"] in only]
    if not tasks:
        raise ConfigError(f"no task matches {only}; tasks in the suite: {', '.join(t['id'] for t in suite['tasks'])}")
    run_id = f"{suite['name']}-{agent}-{time.strftime('%Y%m%d-%H%M%S')}"
    root = bench_dir() / run_id
    n = 1
    while root.exists():
        n += 1
        root = bench_dir() / f"{run_id}-{n}"
    run_id = root.name
    root.mkdir(parents=True)
    result: Dict[str, Any] = {"run_id": run_id, "suite": suite["name"], "suite_path": suite["path"],
                              "agent": agent, "profile": loop.profile, "repeat": k,
                              "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "tasks": {}}
    for t in tasks:
        attempts = []
        for i in range(1, k + 1):
            work = root / "tasks" / t["id"] / str(i)
            if t.get("files"):
                shutil.copytree(t["files"], work, ignore=runner.COPY_IGNORE)
            else:
                work.mkdir(parents=True)
            pointer = dict(suite["pointer"], config=t["config"])
            if t.get("inputs"):
                pointer["inputs"] = str(t["inputs"])
            (work / paths.POINTER_NAME).write_text(yaml.safe_dump(pointer, sort_keys=False), encoding="utf-8")
            say(f"[{t['id']} {i}/{k}] start")
            try:
                # Each attempt is already an independent run, so the in-run repeat is not nested here.
                res = runner.run_task(str(work), agent, t["prompt"], profile_arg=profile, mode="bench",
                                      allow_repeat=False, say=lambda s, p=f"[{t['id']} {i}/{k}] ": say(p + s))
                first = res.rounds[0] if res.rounds else {}
                attempts.append({"status": res.status, "passed": res.status == PASS, "rounds": len(res.rounds),
                                 "reason": res.reason, "checks": res.check_status(),
                                 "failed_checks": [r.id for r in res.outcome.failed()] if res.outcome else [],
                                 "failed_first_round": first.get("failed_checks", []),
                                 "run_id": res.outcome.run_id if res.outcome else None, "dir": str(work)})
            except ConfigError as e:
                attempts.append({"status": "error", "passed": False, "rounds": 0, "reason": str(e), "checks": {},
                                 "failed_checks": [], "failed_first_round": [], "run_id": None, "dir": str(work)})
        result["tasks"][t["id"]] = {"config": t["config_name"], "attempts": attempts,
                                    "passes": sum(a["passed"] for a in attempts),
                                    "pass_all": all(a["passed"] for a in attempts)}
    result["summary"] = summarize(result)
    (root / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def summarize(result: Dict[str, Any]) -> Dict[str, Any]:
    attempts = [a for t in result["tasks"].values() for a in t["attempts"]]
    judged = [a for a in attempts if a["status"] != "error"]
    checks: Dict[str, Dict[str, int]] = {}
    for a in judged:
        for cid, status in a["checks"].items():
            c = checks.setdefault(cid, {"attempts": 0, "passed": 0, "failed": 0, "failed_first_round": 0})
            c["attempts"] += 1
            c["passed"] += status == PASS
            c["failed"] += status == FAIL
        for cid in a["failed_first_round"]:
            checks.setdefault(cid, {"attempts": 0, "passed": 0, "failed": 0, "failed_first_round": 0})
            checks[cid]["failed_first_round"] += 1
    for c in checks.values():
        c["fail_rate"] = c["failed"] / c["attempts"] if c["attempts"] else 0.0
    n_tasks = len(result["tasks"])
    return {"tasks": n_tasks, "attempts": len(attempts), "errors": len(attempts) - len(judged),
            "pass_rate": sum(a["passed"] for a in attempts) / len(attempts) if attempts else 0.0,
            "pass_k": sum(t["pass_all"] for t in result["tasks"].values()) / n_tasks if n_tasks else 0.0,
            "avg_rounds": sum(a["rounds"] for a in judged) / len(judged) if judged else 0.0,
            "checks": checks}


def render(result: Dict[str, Any]) -> str:
    s, k = result["summary"], result["repeat"]
    lines = [f"evalctl bench: {result['run_id']}",
             f"suite: {result['suite']}   agent: {result['agent']}   tasks: {s['tasks']}   attempts per task: {k}", "",
             f"pass rate: {s['pass_rate']:.0%}  ({sum(t['passes'] for t in result['tasks'].values())} of {s['attempts']} attempts)",
             f"pass^{k}: {s['pass_k']:.0%}  (tasks where all {k} attempt(s) passed)",
             f"average rounds: {s['avg_rounds']:.1f}"]
    if s["errors"]:
        lines.append(f"attempts that hit a setup error: {s['errors']}")
    lines += ["", "tasks:"]
    for tid, t in result["tasks"].items():
        marks = " ".join({"pass": "PASS", "fail": "FAIL", "error": "ERROR"}.get(a["status"], a["status"].upper())
                         + f"({a['rounds']}r)" for a in t["attempts"])
        failing = sorted({c for a in t["attempts"] for c in a["failed_checks"]})
        lines.append(f"  {tid}: {t['passes']}/{len(t['attempts'])}  {marks}" + (f"  failing: {', '.join(failing)}" if failing else ""))
    lines += ["", "checks (final result per attempt):"]
    for cid, c in sorted(s["checks"].items(), key=lambda kv: -kv[1]["fail_rate"]):
        lines.append(f"  {cid}: passed {c['passed']}/{c['attempts']}, fail rate {c['fail_rate']:.0%}"
                     + (f", failed in the first round {c['failed_first_round']}x" if c["failed_first_round"] else ""))
    lines += ["", f"result: {bench_dir() / result['run_id'] / 'result.json'}",
              f"compare with another run: evalctl bench --compare {result['run_id']} <other run id>"]
    return "\n".join(lines)


def load_result(ref: str) -> Dict[str, Any]:
    """A run id (directory under ~/.evalctl/bench), a result.json, or a directory holding one."""
    p = Path(ref).expanduser()
    for cand in (p, p / "result.json", bench_dir() / ref / "result.json"):
        if cand.is_file():
            try:
                data = json.loads(cand.read_text(encoding="utf-8"))
            except ValueError as e:
                raise ConfigError(f"{cand}: not valid JSON: {e}")
            if not isinstance(data, dict) or "tasks" not in data:
                raise ConfigError(f"{cand}: not a bench result")
            return data
    have = sorted(d.name for d in bench_dir().iterdir()) if bench_dir().is_dir() else []
    raise ConfigError(f"bench run '{ref}' not found. Runs in {bench_dir()}: {', '.join(have[-10:]) or '(none)'}")


def compare(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    """What changed from run A (before) to run B (after)."""
    rate = lambda t: t["passes"] / len(t["attempts"]) if t["attempts"] else 0.0
    rounds = lambda t: (sum(x["rounds"] for x in t["attempts"]) / len(t["attempts"])) if t["attempts"] else 0.0
    failing = lambda t: sorted({c for x in t["attempts"] for c in x["failed_checks"]})
    better, worse, same, rounds_changed = [], [], [], []
    for tid in [t for t in a["tasks"] if t in b["tasks"]]:
        ta, tb = a["tasks"][tid], b["tasks"][tid]
        row = {"task": tid, "a": f"{ta['passes']}/{len(ta['attempts'])}", "b": f"{tb['passes']}/{len(tb['attempts'])}",
               "a_failing": failing(ta), "b_failing": failing(tb),
               "a_rounds": round(rounds(ta), 2), "b_rounds": round(rounds(tb), 2)}
        if rate(tb) > rate(ta):
            better.append(row)
        elif rate(tb) < rate(ta):
            worse.append(row)
        else:
            same.append(row)
            if row["a_rounds"] != row["b_rounds"]:
                rounds_changed.append(row)
    checks = []
    ca, cb = a["summary"]["checks"], b["summary"]["checks"]
    for cid in sorted(set(ca) | set(cb)):
        fa, fb = ca.get(cid, {}).get("fail_rate"), cb.get(cid, {}).get("fail_rate")
        if fa != fb:
            checks.append({"check": cid, "a_fail_rate": fa, "b_fail_rate": fb})
    return {"a": a["run_id"], "b": b["run_id"],
            "a_pass_rate": a["summary"]["pass_rate"], "b_pass_rate": b["summary"]["pass_rate"],
            "a_pass_k": a["summary"]["pass_k"], "b_pass_k": b["summary"]["pass_k"],
            "better": better, "worse": worse, "same": [r["task"] for r in same], "rounds_changed": rounds_changed,
            "only_in_a": [t for t in a["tasks"] if t not in b["tasks"]],
            "only_in_b": [t for t in b["tasks"] if t not in a["tasks"]],
            "checks": checks}


def render_compare(c: Dict[str, Any]) -> str:
    pct = lambda v: "n/a" if v is None else f"{v:.0%}"
    lines = [f"evalctl bench --compare",
             f"A (before): {c['a']}   pass rate {pct(c['a_pass_rate'])}, all attempts passed {pct(c['a_pass_k'])}",
             f"B (after):  {c['b']}   pass rate {pct(c['b_pass_rate'])}, all attempts passed {pct(c['b_pass_k'])}", ""]

    def block(title: str, rows: List[Dict[str, Any]]) -> None:
        lines.append(f"{title} ({len(rows)})")
        for r in rows:
            lines.append(f"  {r['task']}: {r['a']} -> {r['b']}"
                         + (f"   A failed: {', '.join(r['a_failing'])}" if r["a_failing"] else "")
                         + (f"   B fails: {', '.join(r['b_failing'])}" if r["b_failing"] else ""))
        if not rows:
            lines.append("  (none)")
    block("WORSE in B", c["worse"])
    block("BETTER in B", c["better"])
    lines.append(f"same pass rate ({len(c['same'])})" + (": " + ", ".join(c["same"]) if c["same"] else ""))
    for r in c["rounds_changed"]:
        lines.append(f"  {r['task']}: average rounds {r['a_rounds']} -> {r['b_rounds']}")
    for key, label in (("only_in_a", "only in A"), ("only_in_b", "only in B")):
        if c[key]:
            lines.append(f"{label}: {', '.join(c[key])}")
    if c["checks"]:
        lines += ["", "check fail rates that changed:"]
        lines += [f"  {x['check']}: {pct(x['a_fail_rate'])} -> {pct(x['b_fail_rate'])}" for x in c["checks"]]
    return "\n".join(lines)
