"""The commands that drive an agent: run, bench, agents."""
from __future__ import annotations

import argparse
import json
import os
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


def _task_text(value: str) -> str:
    """--task takes the text itself, a file holding it, or - for stdin."""
    from pathlib import Path
    if value == "-":
        text = sys.stdin.read()
    else:
        p = Path(value).expanduser()
        try:
            is_file = len(value) < 1000 and "\n" not in value and p.is_file()
        except OSError:
            is_file = False
        text = p.read_text(encoding="utf-8") if is_file else value
    if not text.strip():
        raise ConfigError("--task is empty")
    return text


def render_contract(body: dict, state: dict, workdir: Any) -> str:
    lines = [f"evalctl contract: {len(body['items'])} acceptance item(s) for {workdir}",
             f"written by: {body['model']}   contract: {body['hash']}"
             + (f"   profile for this task: {state['profile']}" if state.get("profile") else ""), ""]
    for i in body["items"]:
        lines.append(f"[{i['kind']}] {i['id']}: {i['text']}")
        lines.append(f"    passes when: {i['pass']}")
        lines.append(f"    fails when:  {i['fail']}")
    n = len(state.get("contracts", []))
    lines += ["", ("This task has " + str(n) + " contracts active (earlier requests of the same task still count). " if n > 1 else "")
              + "`evalctl check` verifies every item when the work is done. The contract is stored outside this "
              "directory and signed; it cannot be edited."]
    return "\n".join(lines)


def cmd_contract(args: argparse.Namespace) -> int:
    from pathlib import Path
    from . import contract as ct, settings
    from .judge import detect_worker
    d = Path(args.dir).expanduser() if args.dir else Path.cwd()
    if not d.is_dir():
        raise ConfigError(f"not a directory: {d}")
    workdir = d.resolve()
    worker = detect_worker(args.worker, None)
    if args.clear:
        ct.clear_state(workdir)
        print(f"evalctl contract: no contract is active for {workdir} any more.")
        return 0
    if args.complete:   # the detached half of --background
        ct.complete(workdir, args.complete, worker)
        return 0
    if not args.task:
        state = ct.load_state(workdir)
        if not state or not (state.get("contracts") or state.get("pending")):
            print(f"No acceptance contract is active for {workdir}.\n"
                  "Write one from the task:  evalctl contract --task \"<the request, word for word>\"")
            return 1
        if args.json:
            print(json.dumps({"state": state, "contracts": [ct.load_contract(h) for h in state["contracts"]]}, ensure_ascii=False, indent=2))
            return 0
        for h in state["contracts"]:
            print(render_contract(ct.load_contract(h), state, workdir) + "\n")
        if state.get("pending"):
            print(f"{len(state['pending'])} contract(s) are still being written.")
        return 0
    task = _task_text(args.task)
    conf = settings.load(create=True)
    profile = args.profile or ct.pick_profile(task, conf["triggers"])
    if args.hook:   # called by the Claude Code prompt hook: decide here whether this prompt is gated at all
        from . import config as cfg, loop as lp
        pointer = cfg.find_pointer(workdir)
        pdata = cfg.pointer_data(pointer) if pointer else {}
        workdir = pointer.parent if pointer else workdir
        try:
            state = ct.load_state(workdir)
        except ConfigError:
            state = None
        same = bool(state) and state.get("session") == args.session and bool(args.session)
        if not (pointer or profile or same):
            print(json.dumps({"gated": False}))
            return 0
        wants = pdata.get("contract", "auto") != "off" and len(task.strip()) >= int(conf["contract_min_chars"]) \
            and not task.lstrip().startswith("/")
        if wants:
            state, h = ct.start(workdir, task, source="hook", session=args.session, profile=profile)
            if ct.contract_path(h).is_file():
                ct.complete(workdir, h, worker)
            else:
                ct.spawn_background(workdir, h, worker)
        name = profile or (state or {}).get("profile")
        loop = lp.from_pointer(pointer, name) if pointer else lp.resolve(None, name)
        print(json.dumps({"gated": True, "dir": str(workdir), "contract": wants, "profile": loop.profile,
                          "repeat": loop.repeat["k"] if loop.repeat_on else None}))
        return 0
    if os.environ.get("EVALCTL_CHILD") or os.environ.get("EVALCTL_OUTER"):
        # A run started by evalctl already has its contract; the agent inside cannot swap it.
        state = ct.load_state(workdir)
        print("evalctl contract: this session was started by evalctl, which already wrote the acceptance contract "
              f"({len((state or {}).get('contracts', []))} active). Nothing to do; carry on with the task.")
        return 0
    if args.new and os.environ.get("CLAUDECODE") and (ct.load_state(workdir) or {}).get("source") == "hook":
        raise ConfigError("--new would replace the acceptance contract that was written from the user's own prompt. "
                          "Only the user can do that, from their own terminal.")
    state, h = ct.start(workdir, task, source=args.source, session=args.session, profile=profile, new=args.new)
    if args.background:
        if not ct.contract_path(h).is_file():
            ct.spawn_background(workdir, h, worker)
        else:
            ct.complete(workdir, h, worker)
        print(json.dumps({"hash": h, "profile": state.get("profile"), "pending": True}) if args.json
              else f"evalctl contract: being written in the background ({h}).")
        return 0
    body = ct.complete(workdir, h, worker)
    state = ct.load_state(workdir) or state
    print(json.dumps({"contract": body, "profile": state.get("profile"), "active": state["contracts"]}, ensure_ascii=False, indent=2)
          if args.json else render_contract(body, state, workdir))
    return 0


def cmd_repeat(args: argparse.Namespace) -> int:
    from . import sessionrepeat as sr
    k = args.k
    if k is None:   # the profile of the task in hand says how many
        from . import engine
        try:
            k = engine.load_target(args.dir, None, args.profile, None)["loop"].repeat["k"]
        except ConfigError:
            k = 3
    report = sr.repeat(args.dir, k, args.agent, _task_text(args.task) if args.task else None, args.profile, _say(args))
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else report["text"])
    return 0


def cmd_improve(args: argparse.Namespace) -> int:
    from . import improve as im
    name = args.config
    if not (args.apply or args.approve):
        proposals = im.propose(name)
        im.save(name, proposals)
        print(json.dumps(proposals, ensure_ascii=False, indent=2) if args.json else im.render(name, proposals))
        return 0
    proposals = im.load(name)
    doable = [p for p in proposals if p["action"] != "advice" and p["status"] == "pending"]
    chosen = []
    if args.approve:
        unknown = set(args.approve) - {p["id"] for p in doable}
        if unknown:
            raise ConfigError(f"not a pending proposal that can be applied: {', '.join(sorted(unknown))}")
        chosen = [p for p in doable if p["id"] in args.approve]
    else:
        try:
            for p in doable:
                print(f"{p['id']}  {p['action'].replace('_', ' ')} {p['target']}\n    why: {p['why']}")
                a = input("  apply? [y/N/q] > ").strip().lower()
                if a == "q":
                    break
                p["status"] = "approved" if a == "y" else "rejected"
                if a == "y":
                    chosen.append(p)
        except EOFError:
            print("\nNo answer (input closed). Use --approve ID to apply without prompting.")
            return 1
    for p in chosen:
        p["status"] = "approved"
    if chosen:
        backup = im.apply(name, chosen)
        print(f"Applied {len(chosen)} change(s) to config '{name}': {', '.join(p['id'] for p in chosen)}. "
              f"Previous version: {backup}\nReview the result: evalctl lint {name}")
    else:
        print("Nothing was applied.")
    im.save(name, proposals)
    return 0


def add_parsers(sub: Any) -> None:
    im = sub.add_parser("improve", help="manual: propose changes to a check config from the record of many tasks; apply what you approve")
    im.add_argument("config")
    im.add_argument("--apply", action="store_true", help="go through the stored proposals and apply the ones you approve")
    im.add_argument("--approve", nargs="+", metavar="ID", help="apply these proposals without prompting")
    im.add_argument("--json", action="store_true")
    im.set_defaults(fn=cmd_improve)

    c = sub.add_parser("contract", help="write the acceptance contract for a task from the request itself; or show the active one")
    c.add_argument("--task", help="the user's request, word for word: the text, a file holding it, or - for stdin")
    c.add_argument("--dir", help="the directory the work happens in (default: cwd)")
    c.add_argument("--profile", help="profile for this task (default: by trigger word in the task, see ~/.evalctl/settings.yaml)")
    c.add_argument("--new", action="store_true", help="start a new task: forget the contracts and the starting files of the previous one")
    c.add_argument("--clear", action="store_true", help="remove the active contract and the stored starting files")
    c.add_argument("--worker", help="model family doing the work; the contract is written by another one")
    c.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    c.add_argument("--hook", action="store_true", help=argparse.SUPPRESS)
    c.add_argument("--complete", metavar="HASH", help=argparse.SUPPRESS)
    c.add_argument("--source", default="cli", choices=["cli", "hook"], help=argparse.SUPPRESS)
    c.add_argument("--session", help=argparse.SUPPRESS)
    c.add_argument("--json", action="store_true")
    c.set_defaults(fn=cmd_contract)

    rp = sub.add_parser("repeat", help="in your session: K independent runs of the task in copies of the starting files, "
                                       "and the differences from your output")
    rp.add_argument("--task", help="the task text or a file holding it (default: the task of the active contract)")
    rp.add_argument("-k", type=int, help="number of independent runs (default: the profile's repeat.k, else 3)")
    rp.add_argument("--agent", help="agent adapter for the runs (default: the calling agent; `default_agent` in settings.yaml)")
    rp.add_argument("--dir")
    rp.add_argument("--profile")
    rp.add_argument("--json", action="store_true")
    rp.set_defaults(fn=cmd_repeat)

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
