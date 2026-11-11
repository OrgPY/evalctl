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


# Columns added after v0. `mode` is skill | run | bench; `extra` is a JSON object.
ADDED_COLUMNS = {"agent": "TEXT", "trajectory": "TEXT", "profile": "TEXT", "mode": "TEXT", "extra": "TEXT"}


def connect() -> sqlite3.Connection:
    p = paths.db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    have = {r["name"] for r in conn.execute("PRAGMA table_info(runs)")}
    for col, kind in ADDED_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {col} {kind}")
    return conn


def new_task_id() -> int:
    with closing(connect()) as conn:
        return conn.execute("SELECT COALESCE(MAX(task_id), 0) FROM runs").fetchone()[0] + 1


def get_run(run_id: int) -> Optional[Dict[str, Any]]:
    with closing(connect()) as conn:
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["results"] = json.loads(d["results"])
    d["extra"] = json.loads(d["extra"]) if d.get("extra") else {}
    return d


def update_extra(run_id: int, **values: Any) -> None:
    with closing(connect()) as conn, conn:
        row = conn.execute("SELECT extra FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            return
        extra = json.loads(row["extra"]) if row["extra"] else {}
        extra.update(values)
        conn.execute("UPDATE runs SET extra=? WHERE id=?", (json.dumps(extra, ensure_ascii=False), run_id))


def runs_for(config_name: str, status: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
    """Newest first."""
    q, args = "SELECT * FROM runs WHERE config=?", [config_name]
    if status:
        q, args = q + " AND status=?", args + [status]
    with closing(connect()) as conn:
        rows = conn.execute(q + " ORDER BY id DESC LIMIT ?", args + [limit]).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["results"] = json.loads(d["results"])
        d["extra"] = json.loads(d["extra"]) if d.get("extra") else {}
        out.append(d)
    return out


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
           worker: Optional[str], duration: float, results: List[Dict[str, Any]],
           agent: Optional[str] = None, trajectory: Optional[str] = None, profile: Optional[str] = None,
           mode: str = "skill", extra: Optional[Dict[str, Any]] = None) -> int:
    """-> the run id (what `evalctl explain` takes)."""
    with closing(connect()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO runs (ts, dir, config, config_sha, artifacts_digest, task_id, round, status,"
            " source, worker, duration, results, agent, trajectory, profile, mode, extra)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), str(workdir), config_name, config_sha, digest, task_id, round_no, status,
             source, worker, duration, json.dumps(results, ensure_ascii=False), agent, trajectory,
             profile, mode, json.dumps(extra or {}, ensure_ascii=False)))
        return int(cur.lastrowid)


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
    from . import config as cfg, loop as lp, receipt  # late: avoid import cycle

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
            pointer = cfg.find_pointer(d)
            _, cpath, _ = cfg.select(str(d), None) if pointer else (None, None, None)
            conf = cfg.load(cpath, d) if cpath else None
            loop = lp.from_pointer(pointer) if pointer else None
        except cfg.ConfigError:
            conf = loop = None
        return receipt.verify(d, conf, loop)[0]

    per: Dict[str, Dict[str, Any]] = {}
    for tid, runs in sorted(tasks.items()):
        name = runs[-1]["config"]
        s = per.setdefault(name, {"tasks": 0, "passed": 0, "first_round_pass": 0, "rounds": [],
                                  "with_receipt": 0, "hook_caught": 0, "fails": Counter(),
                                  "runs": 0, "needs_human": 0, "votes": {}})
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
        if final["extra"] and json.loads(final["extra"]).get("needs_human"):
            s["needs_human"] += 1
        counted = set()
        for r in runs:
            for c in json.loads(r["results"]):
                for crit, v in ((c.get("meta") or {}).get("votes") or {}).items():
                    if v.get("n", 1) > 1:  # only repeated gradings say anything about consistency
                        a = s["votes"].setdefault(f"{c['id']}.{crit}", {"runs": 0, "agreement": 0.0, "unstable": 0})
                        a["runs"] += 1
                        a["agreement"] += max(v["pass"], v["n"] - v["pass"]) / v["n"]
                        a["unstable"] += v.get("verdict") == "unstable"
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
            "tasks_needing_human_judgement": s["needs_human"],
            # per rubric item: how often its repeated gradings agreed with each other
            "rubric_consistency": {k: {"runs": v["runs"], "consistency": v["agreement"] / v["runs"],
                                       "unstable_runs": v["unstable"]}
                                   for k, v in sorted(s["votes"].items())},
        }
    for name, fb in feedback_summary(only_config).items():
        if name in out["configs"]:
            out["configs"][name]["feedback"] = fb
    return out
