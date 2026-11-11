"""`evalctl run`: the outer loop. Call the agent, check what it produced, feed the failures back.

    call agent -> diff the working directory -> check -> failures go back into the same session -> ...

Stops when everything passes, when max_rounds is reached, or when the number of things left to
fix did not go down for 2 rounds in a row. Each round is kept in .eval/rounds/N/.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

from . import agents as ag, config as cfg, engine, loop as lp, paths, receipt, store
from .checks import ERROR, FAIL, PASS
from .checks.numbers import file_numbers, read_text
from .config import ConfigError

STALL_ROUNDS = 2
NEEDS_HUMAN = "needs_human"
COPY_IGNORE = shutil.ignore_patterns(paths.EVAL_DIR, ".git", "node_modules", ".venv", "__pycache__")


@dataclass
class RunResult:
    status: str                      # pass | fail | error | needs_human
    reason: str                      # why the loop stopped
    workdir: Path
    rounds: List[Dict[str, Any]] = field(default_factory=list)
    outcome: Optional[engine.Outcome] = None
    session_id: Optional[str] = None
    repeat: Optional[Dict[str, Any]] = None

    @property
    def exit_code(self) -> int:
        return {PASS: 0, ERROR: 2}.get(self.status, 1)

    def check_status(self) -> Dict[str, str]:
        return {r.id: r.status for r in self.outcome.results} if self.outcome else {}

    def to_dict(self) -> Dict[str, Any]:
        o = self.outcome
        return {"status": self.status, "passed": self.status == PASS, "reason": self.reason,
                "dir": str(self.workdir), "rounds": self.rounds, "session_id": self.session_id,
                "receipt": o.receipt if o else None, "run_id": o.run_id if o else None,
                "checks": [r.to_dict() for r in o.results] if o else [], "repeat": self.repeat,
                "text": o.text if o else self.reason}


def first_prompt(task: str, workdir: Path) -> str:
    return (f"{task.strip()}\n\n"
            f"Work in the current directory ({workdir}) and write your output there. Stop when the work is "
            "done: the output is then checked automatically, and you will be told if anything has to be fixed.")


def feedback_prompt(check_text: str, workdir: Path, problem: Optional[str]) -> str:
    return ((f"Note: {problem}.\n\n" if problem else "")
            + f"Your output in {workdir} was checked automatically and did not pass.\n\n{check_text}\n\n"
            "Do not edit .eval/ or .evalctl.yaml.")


def fresh_prompt(task: str, check_text: str, workdir: Path, produced: List[str]) -> str:
    """For an agent that cannot continue a session: everything it needs to pick the work up again."""
    files = ", ".join(produced[:40]) or "(no files yet)"
    return (f"{task.strip()}\n\n"
            f"A previous attempt at this task left its output in the current directory ({workdir}). "
            f"Files it wrote: {files}.\n"
            f"That output was checked automatically and did not pass:\n\n{check_text}\n\n"
            "Continue from the existing files. Do not edit .eval/ or .evalctl.yaml.")


def _hashes(workdir: Path) -> Dict[str, str]:
    return {a["path"]: a["sha256"] for a in receipt.hash_artifacts(workdir, None)}


def _diff(before: Dict[str, str], after: Dict[str, str]) -> Dict[str, List[str]]:
    return {"added": sorted(set(after) - set(before)),
            "changed": sorted(p for p in after if p in before and before[p] != after[p]),
            "deleted": sorted(set(before) - set(after))}


def _snapshot(workdir: Path, files: List[str], dest: Path) -> None:
    for rel in files:
        src = workdir / rel
        if src.is_file():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest / rel)


def key_numbers(workdir: Path, patterns: Optional[List[str]]) -> Dict[str, List[str]]:
    """Per output file, the sorted quantities it contains (dates and times are not quantities)."""
    out: Dict[str, List[str]] = {}
    for p in receipt.list_artifacts(workdir, patterns):
        text = read_text(p)
        if text is not None:
            out[p.relative_to(workdir).as_posix()] = sorted(str(n.value.normalize()) for n in file_numbers(p, text))
    return out


def compare_runs(runs: List[Dict[str, Any]], what: List[str]) -> List[str]:
    """`runs[0]` is the run that passed; the others are its independent repeats. -> differences."""
    diffs: List[str] = []
    base = runs[0]
    for i, r in enumerate(runs[1:], 1):
        if "check_results" in what:
            for cid in sorted(set(base["checks"]) | set(r["checks"])):
                a, b = base["checks"].get(cid, "missing"), r["checks"].get(cid, "missing")
                if a != b:
                    why = (r.get("failures") or {}).get(cid)
                    diffs.append(f"repeat {i}: check {cid} is {b.upper()} (first run: {a.upper()})"
                                 + (f": {why}" if why else ""))
        if "numbers" in what:
            for f in sorted(set(base["numbers"]) | set(r["numbers"])):
                if f not in r["numbers"] or f not in base["numbers"]:
                    diffs.append(f"repeat {i}: {f} exists only in the "
                                 + ("first run" if f in base["numbers"] else "repeat"))
                    continue
                a, b = Counter(base["numbers"][f]), Counter(r["numbers"][f])
                if a != b:
                    only_a, only_b = sorted((a - b).elements()), sorted((b - a).elements())
                    show = lambda xs: ", ".join(xs[:8]) + (f", ... ({len(xs)} in all)" if len(xs) > 8 else "")
                    diffs.append(f"repeat {i}: numbers in {f} differ"
                                 + (f"; only in the first run: {show(only_a)}" if only_a else "")
                                 + (f"; only in the repeat: {show(only_b)}" if only_b else ""))
    return diffs


def _summary(res: "RunResult", conf: cfg.Config) -> Dict[str, Any]:
    o = res.outcome
    return {"status": res.status, "rounds": len(res.rounds), "checks": res.check_status(),
            "failures": {r.id: (r.failures[0] if r.failures else r.summary) for r in (o.failed() if o else [])},
            "numbers": key_numbers(res.workdir, conf.artifacts)}


def run_task(dir_arg: Optional[str], agent_name: str, task: str, *, config_arg: Optional[str] = None,
             profile_arg: Optional[str] = None, inputs_arg: Optional[str] = None,
             max_rounds: Optional[int] = None, mode: str = "run", allow_repeat: bool = True,
             record: bool = True, say: Callable[[str], None] = lambda s: None) -> RunResult:
    agent = ag.get_agent(agent_name)
    # Everything that can be wrong with the setup is found before the agent spends a token.
    workdir, cpath, pointer = cfg.select(dir_arg, config_arg)
    conf = cfg.load(cpath, workdir)
    loop = lp.from_pointer(pointer, profile_arg)
    inputs = cfg.resolve_inputs(inputs_arg, pointer)
    limit = max_rounds or loop.max_rounds or conf.max_rounds
    want_repeat = loop.repeat_on and allow_repeat and mode == "run"

    rounds_dir = workdir / paths.EVAL_DIR / "rounds"
    if rounds_dir.exists():  # keep the previous run's rounds, once
        old = rounds_dir.with_name("rounds.previous")
        shutil.rmtree(old, ignore_errors=True)
        rounds_dir.rename(old)
    seed: Optional[Path] = None
    if want_repeat:
        seed = Path(tempfile.mkdtemp(prefix="evalctl-seed-")) / "work"
        shutil.copytree(workdir, seed, ignore=COPY_IGNORE)

    start_state = _hashes(workdir)
    task_id = store.new_task_id() if record else 0
    res = RunResult(status=FAIL, reason="", workdir=workdir)
    prompt = first_prompt(task, workdir)
    trajectories: List[Path] = []
    stalled, previous_left = 0, None
    try:
        for n in range(1, limit + 1):
            rd = rounds_dir / str(n)
            rd.mkdir(parents=True)
            (rd / "input.md").write_text(prompt, encoding="utf-8")
            before = _hashes(workdir)
            ext = "jsonl" if agent.output in ("json", "jsonl") else "txt"
            say(f"round {n}/{limit}: calling {agent.name}" + (" (continuing the session)" if res.session_id and agent.can_resume else ""))
            call = ag.invoke(agent, prompt, workdir, rd / f"trajectory.{ext}", rd / "stderr.txt",
                             session_id=res.session_id)
            res.session_id = call.session_id
            trajectories.append(call.stdout_path)
            after = _hashes(workdir)
            changes = _diff(before, after)
            produced = _diff(start_state, after)
            (rd / "changes.json").write_text(json.dumps(
                {"this_round": changes, "since_start": produced, "command": call.command[:1] + ["..."],
                 "exit_code": call.exit_code, "timed_out": call.timed_out, "duration": round(call.duration, 1),
                 "session_id": call.session_id}, indent=2, ensure_ascii=False), encoding="utf-8")
            _snapshot(workdir, produced["added"] + produced["changed"], rd / "output")

            out = engine.check(str(workdir), str(cpath), inputs_arg=str(inputs) if inputs else None,
                               worker_arg=agent.family, profile_arg=profile_arg, source="cli", mode=mode,
                               agent=agent.name, trajectories=trajectories, task=(task_id, n),
                               defer_receipt=want_repeat or not record, record=record, max_rounds=limit)
            (rd / "check.json").write_text(json.dumps(out.data, indent=2, ensure_ascii=False), encoding="utf-8")
            (rd / "check.txt").write_text(out.text + "\n", encoding="utf-8")
            res.outcome = out
            left = out.failure_count()
            res.rounds.append({"round": n, "status": out.status, "failed_checks": [r.id for r in out.failed()],
                               "left_to_fix": left, "changed_files": changes["added"] + changes["changed"],
                               "agent_problem": call.problem(), "run_id": out.run_id})
            say(f"round {n}/{limit}: check {out.status.upper()}"
                + (f", {left} thing(s) to fix in: {', '.join(r.id for r in out.failed())}" if out.status == FAIL else "")
                + (f"  [{call.problem()}]" if call.problem() else ""))
            if out.status == PASS:
                res.status, res.reason = PASS, f"all checks passed in round {n}"
                break
            if out.status == ERROR:
                res.status, res.reason = ERROR, "evalctl could not evaluate the output (setup error)"
                break
            from .checks import REGISTRY
            if all(getattr(REGISTRY.get(r.type), "irreversible", False) for r in out.failed()):
                res.reason = (f"stopped after round {n}: what is left ({', '.join(r.id for r in out.failed())}) is about "
                              "actions the agent already took. Another round cannot undo them; this needs your judgement")
                break
            if previous_left is not None and left >= previous_left:
                stalled += 1
            else:
                stalled = 0
            previous_left = left
            if n == limit:
                res.reason = f"round limit reached ({limit}) with {left} thing(s) still failing"
            elif stalled >= STALL_ROUNDS:
                res.reason = (f"stopped after round {n}: the number of failing items did not go down for "
                              f"{STALL_ROUNDS} rounds in a row")
                break
            elif agent.can_resume and res.session_id:
                prompt = feedback_prompt(out.text, workdir, call.problem())
            else:
                prompt = fresh_prompt(task, out.text, workdir, produced["added"] + produced["changed"])

        if res.status == PASS and want_repeat:
            _repeat(res, seed, agent_name, task, conf, cpath, pointer, inputs, profile_arg, limit, say)
    finally:
        if seed is not None:
            shutil.rmtree(seed.parent, ignore_errors=True)
    if record and res.outcome and res.outcome.run_id:
        store.update_extra(res.outcome.run_id, loop_result=res.status, stop_reason=res.reason)
    return res


def _repeat(res: RunResult, seed: Path, agent_name: str, task: str, conf: cfg.Config, cpath: Path,
            pointer: Optional[Path], inputs: Optional[Path], profile_arg: Optional[str], limit: int,
            say: Callable[[str], None]) -> None:
    """The task passed: run it k more times from the same starting files and see if the result holds."""
    loop, out = res.outcome.loop, res.outcome
    k, what = loop.repeat["k"], loop.repeat["compare"]
    runs = [_summary(res, conf)]
    keep = res.workdir / paths.EVAL_DIR / "repeat"
    shutil.rmtree(keep, ignore_errors=True)
    for i in range(1, k + 1):
        say(f"repeat {i}/{k}: running the task again from the starting files")
        tmp = Path(tempfile.mkdtemp(prefix="evalctl-repeat-"))
        work = tmp / "work"
        try:
            shutil.copytree(seed, work)
            data = dict(cfg.pointer_data(pointer)) if pointer else {}
            data["config"] = str(cpath)
            data.pop("inputs", None)
            if inputs:
                data["inputs"] = str(inputs)
            (work / paths.POINTER_NAME).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
            again = run_task(str(work), agent_name, task, profile_arg=profile_arg, max_rounds=limit,
                             mode="run", allow_repeat=False, record=False)
            summary = _summary(again, conf)
            runs.append(summary)
            dest = keep / str(i)
            dest.mkdir(parents=True)
            if (work / paths.EVAL_DIR / "rounds").is_dir():
                shutil.copytree(work / paths.EVAL_DIR / "rounds", dest / "rounds")
            _snapshot(work, [p.relative_to(work).as_posix() for p in receipt.list_artifacts(work, conf.artifacts)],
                      dest / "output")
            (dest / "result.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
            say(f"repeat {i}/{k}: {again.status.upper()} after {len(again.rounds)} round(s)")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    diffs = compare_runs(runs, what)
    passes = sum(r["status"] == PASS for r in runs[1:])
    record = {"ran": True, "k": k, "compare": what, "agreed": not diffs, "passes": passes,
              "differences": diffs}
    res.repeat = record
    if not diffs:
        engine.write_receipt(out, record)
        res.reason += f"; {k} independent repeat(s) gave the same check results and numbers"
        return
    out.ran["repeat"] = record
    if out.run_id:
        store.update_extra(out.run_id, ran=out.ran, needs_human=loop.repeat["on_disagree"] == "escalate")
    receipt.invalidate(res.workdir)
    if loop.repeat["on_disagree"] == "escalate":
        res.status = NEEDS_HUMAN
        res.reason = (f"NEEDS HUMAN JUDGEMENT: the task passed, but {k} independent repeat(s) did not give "
                      "the same result. No receipt was written")
    else:
        res.status = FAIL
        res.reason = f"the task passed, but {k} independent repeat(s) did not give the same result (on_disagree: fail)"


def render(res: RunResult) -> str:
    head = {PASS: "PASS", FAIL: "FAIL", ERROR: "ERROR", NEEDS_HUMAN: "NEEDS HUMAN JUDGEMENT"}[res.status]
    lines = [f"evalctl run: {head}  ({len(res.rounds)} round(s))", f"dir: {res.workdir}", ""]
    for r in res.rounds:
        lines.append(f"  round {r['round']}: {r['status'].upper()}"
                     + (f"  failing: {', '.join(r['failed_checks'])} ({r['left_to_fix']} to fix)" if r["failed_checks"] else "")
                     + (f"  changed: {len(r['changed_files'])} file(s)" if r["changed_files"] else "  no file changed")
                     + (f"  [{r['agent_problem']}]" if r["agent_problem"] else ""))
    lines += ["", f"stopped: {res.reason}"]
    if res.repeat and res.repeat["differences"]:
        lines.append("differences between the runs:")
        lines += [f"  - {d}" for d in res.repeat["differences"]]
        lines.append(f"the repeat runs are kept in {res.workdir / paths.EVAL_DIR / 'repeat'}")
    if res.outcome and res.status != PASS:
        lines += ["", "last check:", res.outcome.text]
    elif res.outcome and res.outcome.receipt:
        lines.append(f"receipt: {res.outcome.receipt}")
    lines.append(f"rounds (input, output snapshot, check result, trajectory): {res.workdir / paths.EVAL_DIR / 'rounds'}")
    return "\n".join(lines)
