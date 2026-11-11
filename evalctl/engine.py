"""One check run: pick the config, apply the loop switches, run the checks, write receipt and log.

`evalctl check` (skill mode), the Stop hook, `evalctl run` and `evalctl bench` all go through
`check()`, so a verdict means the same thing whoever asked for it.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import config as cfg, loop as lp, receipt, report, store
from .checks import ERROR, FAIL, PASS, REGISTRY, SKIPPED, SOFT_TYPES, Context, Result
from .config import ConfigError
from .judge import JudgeError, detect_worker

EXIT_PASS, EXIT_FAIL, EXIT_CONFIG = 0, 1, 2
EXIT = {PASS: EXIT_PASS, FAIL: EXIT_FAIL, ERROR: EXIT_CONFIG}


@dataclass
class Outcome:
    status: str                                  # pass | fail | error
    text: str                                    # the checklist, as shown to the agent
    data: Dict[str, Any]                         # the same as JSON
    results: List[Result] = field(default_factory=list)
    conf: Optional[cfg.Config] = None
    workdir: Optional[Path] = None
    loop: Optional[lp.Loop] = None
    round_no: Optional[int] = None
    task_id: Optional[int] = None
    run_id: Optional[int] = None
    receipt: Optional[str] = None
    ran: Dict[str, Any] = field(default_factory=dict)
    inputs: Optional[Path] = None
    worker: Optional[str] = None

    @property
    def passed(self) -> bool:
        return self.status == PASS

    @property
    def exit_code(self) -> int:
        return EXIT[self.status]

    def failed(self) -> List[Result]:
        return [r for r in self.results if r.status == FAIL]

    def failure_count(self) -> int:
        """Number of separate things left to fix (a failed check with three listed items counts three)."""
        return sum(max(1, sum(1 for f in r.failures if not str(f).startswith("  "))) for r in self.failed())


def _guard(spec: cfg.CheckSpec, fn: Any) -> Result:
    try:
        return fn()
    except ConfigError as e:
        return Result(spec.id, spec.type, ERROR, str(e), [str(e)])


def run_checks(conf: cfg.Config, workdir: Path, worker: Optional[str], inputs: Optional[Path] = None,
               loop: Optional[lp.Loop] = None, round_no: Optional[int] = None,
               trajectories: Optional[List[Path]] = None) -> Tuple[List[Result], Dict[str, Any]]:
    """Hard checks first; model-graded checks only when every hard check passed.

    -> (results in config order, what judge_repeat did or None)
    """
    ctx = Context(workdir=workdir, config=conf, worker=worker, inputs=inputs,
                  trajectories=list(trajectories or []))
    results: Dict[str, Result] = {}
    hard = [c for c in conf.checks if c.type not in SOFT_TYPES]
    soft = [c for c in conf.checks if c.type in SOFT_TYPES]
    for spec in hard:
        results[spec.id] = _guard(spec, lambda: REGISTRY[spec.type].run(spec, ctx))
    jr_ran: Optional[Dict[str, Any]] = None
    # A hard check that was skipped on purpose (action_policy without a log, `on_missing: skip`)
    # neither fails the run nor holds back the graded checks.
    if any(results[s.id].status in (FAIL, ERROR) for s in hard):
        for spec in soft:
            results[spec.id] = Result(spec.id, spec.type, SKIPPED,
                                      "not run: runs only after all other checks pass")
        return [results[c.id] for c in conf.checks], {"judge_repeat": jr_ran}

    # First grading of every rubric check.
    state: Dict[str, Tuple[Any, List[Any]]] = {}
    for spec in soft:
        cls = REGISTRY[spec.type]
        prep = _guard(spec, lambda: cls.prepare(spec, ctx))
        if isinstance(prep, Result):
            results[spec.id] = prep
            continue
        try:
            state[spec.id] = (prep, [cls.grade(prep)])
            results[spec.id] = cls.conclude(spec, prep, state[spec.id][1])
        except JudgeError as e:
            results[spec.id] = cls.grading_error(spec, prep, e)

    jr = loop.judge_repeat if loop else None
    if soft and jr and loop.judge_repeat_on:
        would_pass = all(results[s.id].status == PASS for s in soft)
        last_round = round_no is not None and round_no >= conf.max_rounds
        if jr["when"] == "always" or would_pass or last_round:
            for spec in soft:
                if spec.id not in state or results[spec.id].status == ERROR:
                    continue
                cls = REGISTRY[spec.type]
                prep, gradings = state[spec.id]
                try:
                    while len(gradings) < jr["n"]:
                        gradings.append(cls.grade(prep))
                    results[spec.id] = cls.conclude(spec, prep, gradings, jr["agree"], jr["unstable"])
                except JudgeError as e:
                    results[spec.id] = cls.grading_error(spec, prep, e)
            jr_ran = {"ran": True, "n": jr["n"], "agree": jr["agree"], "mode": jr["unstable"],
                      "unstable": [f"{s.id}.{c}" for s in soft for c in results[s.id].meta.get("unstable", [])]}
    return [results[c.id] for c in conf.checks], {"judge_repeat": jr_ran}


def _error(message: str, as_json_extra: Optional[Dict[str, Any]] = None, **kw: Any) -> Outcome:
    text = f"evalctl: configuration error\n{message}"
    data = {"status": "error", "passed": False, "error": message, "text": text}
    data.update(as_json_extra or {})
    return Outcome(status=ERROR, text=text, data=data, **kw)


def env_trajectories(args: Optional[List[str]]) -> List[Path]:
    raw = list(args or []) or [t for t in os.environ.get("EVALCTL_TRAJECTORY", "").split(os.pathsep) if t]
    out = []
    for t in raw:
        p = Path(t).expanduser()
        if not p.is_file():
            raise ConfigError(f"trajectory file not found: {p}")
        out.append(p.resolve())
    return out


def check(dir_arg: Optional[str], config_arg: Optional[str] = None, *, inputs_arg: Optional[str] = None,
          worker_arg: Optional[str] = None, profile_arg: Optional[str] = None, source: str = "cli",
          mode: str = "skill", agent: Optional[str] = None, trajectories: Optional[List[Path]] = None,
          task: Optional[Tuple[int, int]] = None, defer_receipt: bool = False, record: bool = True,
          max_rounds: Optional[int] = None) -> Outcome:
    """`task` = (task_id, round) when the caller counts rounds itself (run / bench).
    `defer_receipt`: do not write the receipt on a pass; the caller does, via write_receipt()."""
    started = time.time()
    inner = mode == "skill" and bool(os.environ.get("EVALCTL_OUTER"))  # the agent ran check inside `evalctl run`
    record = record and not inner

    def log_error(message: str, workdir: Optional[Path], name: str) -> None:
        if record and workdir is not None:
            try:
                store.record(workdir, name, None, None, None, None, "error", source, None, 0.0,
                             [{"id": "config", "type": "config", "status": "error", "summary": message}],
                             agent=agent, mode=mode)
            except Exception:
                pass

    try:
        workdir, cpath, pointer = cfg.select(dir_arg, config_arg)
    except ConfigError as e:
        return _error(str(e))
    try:
        conf = cfg.load(cpath, workdir)
        inputs = cfg.resolve_inputs(inputs_arg, pointer)
        loop = lp.from_pointer(pointer, profile_arg)
        trajectories = list(trajectories) if trajectories is not None else env_trajectories(None)
    except ConfigError as e:
        log_error(str(e), workdir, cpath.stem)
        return _error(str(e), workdir=workdir)
    conf.max_rounds = max_rounds or loop.max_rounds or conf.max_rounds
    worker = detect_worker(worker_arg, conf.worker_family)
    ran: Dict[str, Any] = {"lint": None, "judge_repeat": None, "repeat": None}
    notes: List[str] = []

    if loop.lint_on:
        from . import lint
        try:
            found = lint.run(conf, worker=worker, refresh=loop.lint["when"] == "always")
        except ConfigError as e:
            msg = f"lint is required by the loop settings (when: {loop.lint['when']}) but could not run:\n{e}"
            log_error(msg, workdir, conf.name)
            return _error(msg, workdir=workdir, conf=conf, loop=loop)
        counts = lint.counts(found)
        ran["lint"] = {"ran": True, "config_sha": conf.sha256, "cached": bool(found.get("cached")), **counts}
        block = loop.lint["block_on"]
        blocking = counts["critical"] + (counts["warning"] if block == "warning" else 0) if block != "never" else 0
        if blocking:
            msg = (f"lint found {blocking} blocking problem(s) in check config '{conf.name}'. The checks were "
                   "not run: this is a problem with the rules, not with the output. Report it to the user.\n\n"
                   + lint.render(found))
            log_error(msg, workdir, conf.name)
            return _error(msg, {"lint": found}, workdir=workdir, conf=conf, loop=loop)
        if counts["warning"]:
            notes.append(f"lint: {counts['warning']} warning(s) about check config '{conf.name}' "
                         f"(for the user: `evalctl lint {conf.name}`)")

    digest = receipt.digest(receipt.hash_artifacts(workdir, conf.artifacts))
    if task is not None:
        task_id, round_no = task
    else:
        task_id, round_no = store.plan_round(workdir, conf.name, conf.sha256, digest)

    results, did = run_checks(conf, workdir, worker, inputs, loop, round_no, trajectories)
    ran["judge_repeat"] = did["judge_repeat"]
    status = report.overall(results)
    unstable = (ran["judge_repeat"] or {}).get("unstable") or []
    if unstable and loop.judge_repeat["unstable"] == "exclude":
        notes.append(f"{len(unstable)} rubric item(s) had no stable verdict across "
                     f"{loop.judge_repeat['n']} gradings and were left out of the verdict "
                     "(nothing to fix; reported to the user in `evalctl stats`)")
    if loop.repeat_on and mode == "skill":
        notes.append(f"the loop settings ask for repeat (k={loop.repeat['k']}, when: on_pass). Repeat only runs "
                     "under `evalctl run` and `evalctl bench`; it was ignored here, so `evalctl verify` "
                     "will not accept a receipt from this check")
    if inner:
        notes.append("an outer `evalctl run` is driving this session: this check wrote no receipt and no "
                     "log entry. The outer loop checks again when you stop")

    receipt_file = None
    if status == PASS and not defer_receipt and not inner:
        try:
            receipt_file = str(receipt.write(workdir, conf, results, round_no, loop, ran))
        except ConfigError as e:
            results.append(Result("receipt", "receipt", ERROR, str(e), [str(e)]))
            status = ERROR
    if status == FAIL and not inner:
        receipt.invalidate(workdir)

    is_error = status == ERROR
    run_id = None
    if record:
        run_id = store.record(
            workdir, conf.name, conf.sha256, digest, None if is_error else task_id,
            None if is_error else round_no, status, source, worker, time.time() - started,
            [r.to_dict() for r in results], agent=agent or os.environ.get("EVALCTL_AGENT"),
            trajectory=os.pathsep.join(str(t) for t in trajectories) or None, profile=loop.profile, mode=mode,
            extra={"switches": loop.switches(), "ran": ran, "unstable": unstable})

    shown_round = None if is_error else round_no
    text = report.render_text(conf, workdir, results, status, shown_round, receipt_file, mode, notes, run_id,
                              receipt_expected=not (defer_receipt or inner))
    data = report.to_json(conf, workdir, results, status, shown_round, receipt_file, text,
                          run_id=run_id, profile=loop.profile, switches=loop.switches(), ran=ran,
                          unstable=unstable, notes=notes)
    return Outcome(status=status, text=text, data=data, results=results, conf=conf, workdir=workdir, loop=loop,
                   round_no=shown_round, task_id=task_id, run_id=run_id, receipt=receipt_file, ran=ran,
                   inputs=inputs, worker=worker)


def write_receipt(outcome: Outcome, repeat: Dict[str, Any]) -> str:
    """Write the receipt of a deferred pass once the repeat runs agreed."""
    outcome.ran["repeat"] = repeat
    path = str(receipt.write(outcome.workdir, outcome.conf, outcome.results, outcome.round_no,
                             outcome.loop, outcome.ran))
    outcome.receipt = path
    outcome.data["receipt"] = path
    outcome.data["ran"] = outcome.ran
    if outcome.run_id:
        store.update_extra(outcome.run_id, ran=outcome.ran)
    return path
