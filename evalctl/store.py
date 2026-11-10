"""Run log in ~/.evalctl/runs.sqlite and the numbers `evalctl stats` reports.

Terms:
  run    one `evalctl check` invocation.
  round  goes up by one each time check runs on *changed* output; re-running on
         identical files and config stays in the same round.
  task   consecutive runs for one (dir, config) up to and including the first
         PASS. A failing task untouched for TASK_GAP_HOURS is considered
         abandoned and the next run starts a new task.
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import paths

TASK_GAP_HOURS = 6

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    dir TEXT NOT NULL,
    config TEXT NOT NULL,
    config_sha TEXT,
    artifacts_digest TEXT,
    task_id INTEGER,
    round INTEGER,
    status TEXT NOT NULL,          -- pass | fail | error
    source TEXT NOT NULL,          -- cli | hook
    worker TEXT,
    duration REAL,
    results TEXT NOT NULL          -- JSON list of per-check results
);
CREATE INDEX IF NOT EXISTS runs_dir_config ON runs (dir, config, id);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    dir TEXT NOT NULL,
    config TEXT NOT NULL,
    artifacts_digest TEXT,
    verdict TEXT NOT NULL,         -- good | bad (the human's judgement)
    check_status TEXT NOT NULL,    -- pass | fail | error (what the checks said about the same files)
    failed_checks TEXT NOT NULL,   -- JSON list of check ids
    note TEXT
);
"""


def connect() -> sqlite3.Connection:
    p = paths.db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def plan_round(workdir: Path, config_name: str, config_sha: str, digest: str,
               now: Optional[float] = None) -> Tuple[int, int]:
    """Decide (task_id, round) for a run that is about to happen."""
    now = now or time.time()
    with closing(connect()) as conn:
        prev = conn.execute(
            "SELECT * FROM runs WHERE dir=? AND config=? AND status IN ('pass','fail') "
            "ORDER BY id DESC LIMIT 1", (str(workdir), config_name)).fetchone()
        next_task = (conn.execute("SELECT COALESCE(MAX(task_id), 0) FROM runs").fetchone()[0]) + 1
    if prev is None:
        return next_task, 1
    same = prev["artifacts_digest"] == digest and prev["config_sha"] == config_sha
    if prev["status"] == "pass":
        return (prev["task_id"], prev["round"]) if same else (next_task, 1)
    if now - prev["ts"] > TASK_GAP_HOURS * 3600:
        return next_task, 1
    return prev["task_id"], prev["round"] + (0 if same else 1)


def record(workdir: Path, config_name: str, config_sha: Optional[str], digest: Optional[str],
           task_id: Optional[int], round_no: Optional[int], status: str, source: str,
           worker: Optional[str], duration: float, results: List[Dict[str, Any]]) -> None:
    with closing(connect()) as conn, conn:
        conn.execute(
            "INSERT INTO runs (ts, dir, config, config_sha, artifacts_digest, task_id, round, status,"
            " source, worker, duration, results) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), str(workdir), config_name, config_sha, digest, task_id, round_no, status,
             source, worker, duration, json.dumps(results, ensure_ascii=False)))


def last_results(workdir: Path, config_name: str, config_sha: str, digest: str) -> Optional[Tuple[str, List[Dict[str, Any]]]]:
    """(status, per-check results) of the latest run, if it judged exactly these files with this config."""
    with closing(connect()) as conn:
        prev = conn.execute(
            "SELECT * FROM runs WHERE dir=? AND config=? AND status IN ('pass','fail') "
            "ORDER BY id DESC LIMIT 1", (str(workdir), config_name)).fetchone()
    if prev is None or prev["artifacts_digest"] != digest or prev["config_sha"] != config_sha:
        return None
    return prev["status"], json.loads(prev["results"])


def record_feedback(workdir: Path, config_name: str, digest: str, verdict: str, check_status: str,
                    failed: List[str], note: Optional[str]) -> None:
    with closing(connect()) as conn, conn:
        conn.execute(
            "INSERT INTO feedback (ts, dir, config, artifacts_digest, verdict, check_status, failed_checks, note)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (time.time(), str(workdir), config_name, digest, verdict, check_status,
             json.dumps(failed, ensure_ascii=False), note))


def feedback_summary(config_name: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Per config: how often the human's verdict agreed with the checks, and which checks were overruled."""
    with closing(connect()) as conn:
        rows = conn.execute("SELECT * FROM feedback ORDER BY id").fetchall()
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        if config_name and r["config"] != config_name:
            continue
        s = out.setdefault(r["config"], {"verdicts": 0, "agree": 0, "checks_too_lenient": 0,
                                         "checks_too_strict": 0, "overruled": Counter()})
        s["verdicts"] += 1
        if r["check_status"] == "pass" and r["verdict"] == "bad":
            s["checks_too_lenient"] += 1
        elif r["check_status"] == "fail" and r["verdict"] == "good":
            s["checks_too_strict"] += 1
            s["overruled"].update(json.loads(r["failed_checks"]))
        elif r["check_status"] in ("pass", "fail"):
            s["agree"] += 1
    for s in out.values():
        s["overruled"] = dict(s["overruled"].most_common())
    return out


def stats(only_config: Optional[str] = None) -> Dict[str, Any]:
    from . import config as cfg, receipt  # late: avoid import cycle

    with closing(connect()) as conn:
        rows = conn.execute("SELECT * FROM runs ORDER BY id").fetchall()
    tasks: Dict[int, List[sqlite3.Row]] = defaultdict(list)
    errors: Counter = Counter()
    for r in rows:
        if only_config and r["config"] != only_config:
            continue
        if r["task_id"] is None:
            errors[r["config"]] += 1
        else:
            tasks[r["task_id"]].append(r)

    latest_in_dir: Dict[str, int] = {}
    for tid, runs in tasks.items():
        latest_in_dir[runs[-1]["dir"]] = max(latest_in_dir.get(runs[-1]["dir"], 0), tid)

    def receipt_still_valid(run: sqlite3.Row) -> bool:
        d = Path(run["dir"])
        if not d.is_dir():
            return True  # directory is gone; the pass is all we know
        try:
            _, cpath, _ = cfg.select(str(d), None) if cfg.find_pointer(d) else (None, None, None)
            conf = cfg.load(cpath, d) if cpath else None
        except cfg.ConfigError:
            conf = None
        return receipt.verify(d, conf)[0]

    per: Dict[str, Dict[str, Any]] = {}
    for tid, runs in sorted(tasks.items()):
        name = runs[-1]["config"]
        s = per.setdefault(name, {"tasks": 0, "passed": 0, "first_round_pass": 0, "rounds": [],
                                  "with_receipt": 0, "hook_caught": 0, "fails": Counter(),
                                  "runs": 0})
        final = runs[-1]
        s["tasks"] += 1
        s["runs"] += len(runs)
        s["rounds"].append(final["round"])
        if runs[0]["source"] == "hook":
            s["hook_caught"] += 1
        if final["status"] == "pass":
            s["passed"] += 1
            if final["round"] == 1:
                s["first_round_pass"] += 1
            if latest_in_dir[final["dir"]] != tid or receipt_still_valid(final):
                s["with_receipt"] += 1
        counted = set()
        for r in runs:
            for c in json.loads(r["results"]):
                if c.get("status") == "fail" and (r["round"], c["id"]) not in counted:
                    counted.add((r["round"], c["id"]))
                    s["fails"][c["id"]] += 1

    out: Dict[str, Any] = {"configs": {}}
    for name, s in per.items():
        n = s["tasks"]
        out["configs"][name] = {
            "tasks": n,
            "runs": s["runs"],
            "pass_rate": s["passed"] / n,
            "first_round_pass_rate": s["first_round_pass"] / n,
            "avg_rounds": sum(s["rounds"]) / n,
            "compliance_rate": s["with_receipt"] / n,
            "tasks_with_valid_receipt": s["with_receipt"],
            "tasks_first_checked_by_hook": s["hook_caught"],
            "config_error_runs": errors.get(name, 0),
            "top_failing_checks": [{"id": cid, "failed_rounds": k} for cid, k in s["fails"].most_common(5)],
        }
    for name, fb in feedback_summary(only_config).items():
        if name in out["configs"]:
            out["configs"][name]["feedback"] = fb
    return out
