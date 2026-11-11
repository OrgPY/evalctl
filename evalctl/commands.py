"""The commands that drive an agent: run, bench, agents."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import agents as ag, bench as bn, runner
from .config import ConfigError


def _say(args: argparse.Namespace):
    return (lambda s: None) if args.json else (lambda s: print(f"evalctl: {s}", file=sys.stderr, flush=True))


def cmd_run(args: argparse.Namespace) -> int:
    res = runner.run_task(args.dir, args.agent, args.task, config_arg=args.config, profile_arg=args.profile,
                          inputs_arg=args.inputs, max_rounds=args.max_rounds, say=_say(args))
    if args.json:
        print(json.dumps(res.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(runner.render(res))
    return res.exit_code


def cmd_bench(args: argparse.Namespace) -> int:
    if args.compare:
        c = bn.compare(bn.load_result(args.compare[0]), bn.load_result(args.compare[1]))
        print(json.dumps(c, ensure_ascii=False, indent=2) if args.json else bn.render_compare(c))
        return 1 if c["worse"] else 0
    if not args.suite or not args.agent:
        raise ConfigError("usage: evalctl bench SUITE.yaml --agent NAME [--repeat K]\n"
                          "   or: evalctl bench --compare RUN_A RUN_B")
    result = bn.run_suite(args.suite, args.agent, args.repeat, args.profile, args.task, _say(args))
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else bn.render(result))
    return 0 if result["summary"]["pass_k"] == 1.0 else 1


def cmd_agents(args: argparse.Namespace) -> int:
    agents = ag.load_agents()
    if args.json:
        print(json.dumps({n: vars(a) for n, a in agents.items()}, ensure_ascii=False, indent=2))
        return 0
    print(f"agents in {ag.agents_path()}:")
    for name, a in agents.items():
        print(f"  {name}  (family: {a.family or '?'}, output: {a.output}, trajectory: {a.trajectory}, "
              f"timeout: {a.timeout:.0f}s, {'continues the session' if a.can_resume else 'new call each round'})")
        print(f"      start:  {' '.join(a.start)}")
        if a.resume:
            print(f"      resume: {' '.join(a.resume)}")
    return 0


def cmd_lint(args: argparse.Namespace) -> int:
    from pathlib import Path
    from . import lint

    def directory(value, flag):
        if not value:
            return None
        d = Path(value).expanduser()
        if not d.is_dir():
            raise ConfigError(f"{flag}: not a directory: {d}")
        return d.resolve()

    final, inputs = directory(args.final, "--final"), directory(args.inputs, "--inputs")
    task = args.task
    if task and task.startswith("@"):
        try:
            task = Path(task[1:]).expanduser().read_text(encoding="utf-8")
        except OSError as e:
            raise ConfigError(f"--task: cannot read {task[1:]}: {e}")
    conf = lint.load_for_lint(args.config, final)
    result = lint.run(conf, refresh=args.refresh, inputs=inputs, final=final, task=task, static_only=args.static)
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else lint.render(result))
    return 1 if lint.counts(result)["critical"] else 0


def default_lang() -> str:
    import os
    lang = os.environ.get("EVALCTL_LANG") or os.environ.get("LC_ALL") or os.environ.get("LANG") or ""
    return "zh" if lang.lower().startswith("zh") else "en"


def cmd_explain(args: argparse.Namespace) -> int:
    from pathlib import Path
    from . import engine, explain as ex
    lang = args.lang or default_lang()
    given = [x for x in (args.run_id, args.round, args.cluster) if x is not None]
    if len(given) != 1:
        raise ConfigError("usage: evalctl explain RUN_ID   |   evalctl explain --round DIR   |   evalctl explain --cluster CONFIG")
    if args.cluster:
        c = ex.cluster(args.cluster, lang, args.limit, not args.no_model)
        print(json.dumps(c, ensure_ascii=False, indent=2) if args.json else ex.render_cluster(c))
        return 0
    if args.round:
        d = Path(args.round).expanduser()
        if not d.is_dir():
            raise ConfigError(f"--round: not a directory: {d}")
        data = ex.explain_round(d.resolve(), lang, not args.no_model)
    else:
        extra = engine.env_trajectories(args.trajectory) if args.trajectory else None
        data = ex.explain_run(args.run_id, lang, not args.no_model, extra, args.refresh)
    print(json.dumps(data, ensure_ascii=False, indent=2) if args.json else ex.render(data))
    return 0


def cmd_judge(args: argparse.Namespace) -> int:
    from pathlib import Path
    from . import config as cfg, pairwise as pw, receipt

    if args.stats:
        data = pw.summary(args.config)
        print(json.dumps(data, ensure_ascii=False, indent=2) if args.json else pw.render_summary(data))
        return 0
    if args.bench:
        pairs = pw.bench_pairs(bn.load_result(args.bench[0]), bn.load_result(args.bench[1]), args.task)
        if not pairs:
            raise ConfigError("the two bench runs have no task in common" + (f" among {args.task}" if args.task else ""))
    elif len(args.dirs) == 2:
        pairs = [{"task": None, "config": None, "a": Path(args.dirs[0]).expanduser(), "b": Path(args.dirs[1]).expanduser()}]
    else:
        raise ConfigError("usage: evalctl judge DIR_A DIR_B [--config NAME]\n"
                          "   or: evalctl judge --bench RUN_A RUN_B\n   or: evalctl judge --stats")
    interactive = not (args.pick and args.reason)
    if not interactive and len(pairs) > 1:
        raise ConfigError("--pick / --reason answer one pair; with --bench add --task ID to choose it")
    results = []
    for pair in pairs:
        a, b = pair["a"], pair["b"]
        for d in (a, b):
            if not d.is_dir():
                raise ConfigError(f"not a directory: {d}")
        a, b = a.resolve(), b.resolve()
        name = args.config or pair["config"]
        if not name:
            pointer = cfg.find_pointer(a) or cfg.find_pointer(b)
            if pointer is None:
                raise ConfigError("which check config do these outputs belong to? Pass --config NAME "
                                  "(neither directory has a .evalctl.yaml)")
            name = cfg.read_pointer(pointer)
        conf = cfg.load(cfg.resolve_config_path(name), a)
        digest = lambda d: receipt.digest(receipt.hash_artifacts(d, conf.artifacts))
        label = f"task {pair['task']}: " if pair["task"] else ""
        if digest(a) == digest(b):
            print(f"{label}the two outputs are identical, nothing to judge.")
            continue
        blind = interactive and (args.blind or bool(args.bench))
        if blind and pw.shuffled(digest(a) + digest(b)):
            a, b = b, a
        if not args.json:
            print(f"{label}config {conf.name}")
            print("A and B are shown in random order; which run each came from is revealed after you choose.\n" if blind
                  else f"A: {a}\nB: {b}\n")
            print(pw.describe_pair(a, b, conf.artifacts, full=args.full) + "\n")
        pick, reason = args.pick, args.reason
        try:
            while pick not in ("a", "b"):
                pick = input("Which is better? [a/b]  (s = skip, q = quit) > ").strip().lower()
                if pick == "s":
                    break
                if pick == "q":
                    return 0
            if pick == "s":
                continue
            while not (reason or "").strip():
                reason = input("Why, in one sentence > ")
        except EOFError:
            print("\nNo answer (input closed). Use --pick a|b --reason TEXT to judge without prompting.")
            return 1
        res = pw.judge_pair(a, b, conf, pick, reason, args.file, not args.no_rubric, not args.no_candidate)
        results.append(res)
        if not args.json:
            print(("\n" if interactive else "") + pw.render(res))
            if blind:
                print(f"A was {a}\nB was {b}")
            print()
    if args.json:
        print(json.dumps(results[0] if len(results) == 1 else results, ensure_ascii=False, indent=2))
    return 0


def add_parsers(sub: Any) -> None:
    j = sub.add_parser("judge", help="compare two outputs of the same task yourself: pick the better one and say why")
    j.add_argument("dirs", nargs="*", metavar="DIR", help="the two output directories, A then B")
    j.add_argument("--config", help="check config the outputs belong to (default: from .evalctl.yaml in A or B)")
    j.add_argument("--bench", nargs=2, metavar=("RUN_A", "RUN_B"), help="judge the same tasks from two bench runs, one pair after another")
    j.add_argument("--task", action="append", metavar="ID", help="with --bench: only this task (repeatable)")
    j.add_argument("--pick", choices=["a", "b"], help="your choice, without prompting")
    j.add_argument("--reason", help="one sentence: why that output is better")
    j.add_argument("--file", metavar="GLOB", help="the file your reason is about (default: the config's first `artifacts` entry)")
    j.add_argument("--blind", action="store_true", help="show the pair in random order without the paths (default with --bench)")
    j.add_argument("--full", action="store_true", help="print whole files instead of a diff")
    j.add_argument("--no-rubric", action="store_true", help="do not grade the pair with the config's llm_rubric checks")
    j.add_argument("--no-candidate", action="store_true", help="do not turn the reason into a candidate rubric")
    j.add_argument("--stats", action="store_true", help="agreement between the llm_rubric checks and your judgements, per config")
    j.add_argument("--json", action="store_true")
    j.set_defaults(fn=cmd_judge)

    e = sub.add_parser("explain", help="why did a run fail: the earliest step that went wrong, and the kind of cause")
    e.add_argument("run_id", nargs="?", type=int, help="run id, as printed by `evalctl check` (run: #N)")
    e.add_argument("--round", metavar="DIR", help="explain a round directory instead (.eval/rounds/N: check.json + trajectory.jsonl)")
    e.add_argument("--cluster", metavar="CONFIG", help="explain the recent failed tasks of this config and group them by cause")
    e.add_argument("--trajectory", action="append", metavar="FILE", help="trajectory to read when the run has none recorded")
    e.add_argument("--lang", choices=["en", "zh"], help="language of the one-sentence cause (default: $EVALCTL_LANG, else from $LANG)")
    e.add_argument("--no-model", action="store_true", help="rule-based guess only")
    e.add_argument("--refresh", action="store_true", help="ignore the stored explanation")
    e.add_argument("--limit", type=int, default=30, help="--cluster: how many failed tasks to read (default 30)")
    e.add_argument("--json", action="store_true")
    e.set_defaults(fn=cmd_explain)

    li = sub.add_parser("lint", help="review a check config itself: coverage, conflicts, brittle checks, reference output")
    li.add_argument("config", help="check config name or path")
    li.add_argument("--final", help="a known-good output that must pass every check (default: `reference.final` in the config)")
    li.add_argument("--inputs", help="the inputs that belong to --final")
    li.add_argument("--task", help="what the task asks for, as text or @FILE (added to `description` / `task` in the config)")
    li.add_argument("--static", action="store_true", help="no model: only the rule-based findings")
    li.add_argument("--refresh", action="store_true", help="ignore the cached result")
    li.add_argument("--json", action="store_true")
    li.set_defaults(fn=cmd_lint)

    r = sub.add_parser("run", help="call an agent on a task, check the output, feed failures back, repeat")
    r.add_argument("task", help="the task, as you would give it to the agent")
    r.add_argument("--agent", required=True, help="agent name from ~/.evalctl/agents.yaml (claude, codex, ...)")
    r.add_argument("--dir", help="working directory (default: the one holding .evalctl.yaml, else cwd)")
    r.add_argument("--config", help="check config name or path (default: from .evalctl.yaml)")
    r.add_argument("--profile", help="loop profile from .evalctl.yaml")
    r.add_argument("--inputs", help="directory with the task's input files")
    r.add_argument("--max-rounds", type=int, help="override max_rounds")
    r.add_argument("--json", action="store_true")
    r.set_defaults(fn=cmd_run)

    b = sub.add_parser("bench", help="run a suite of stored tasks; or compare two such runs")
    b.add_argument("suite", nargs="?", help="suite YAML file")
    b.add_argument("--agent")
    b.add_argument("--repeat", type=int, metavar="K", help="attempts per task (default: 1, or the suite's loop.repeat.k when it is on)")
    b.add_argument("--task", action="append", metavar="ID", help="run only this task (repeatable)")
    b.add_argument("--profile")
    b.add_argument("--compare", nargs=2, metavar=("RUN_A", "RUN_B"), help="compare two bench runs (ids or result.json paths)")
    b.add_argument("--json", action="store_true")
    b.set_defaults(fn=cmd_bench)

    a = sub.add_parser("agents", help="list the agent adapters in ~/.evalctl/agents.yaml")
    a.add_argument("--json", action="store_true")
    a.set_defaults(fn=cmd_agents)
