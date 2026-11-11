from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import List, Optional

from . import __version__, config as cfg, engine, loop as lp, receipt, store
from .checks import ERROR
from .config import ConfigError

EXIT_PASS, EXIT_FAIL, EXIT_CONFIG = 0, 1, 2


def _config_error(args: argparse.Namespace, message: str) -> int:
    if getattr(args, "json", False):
        print(json.dumps({"status": "error", "passed": False, "error": message,
                          "text": f"evalctl: configuration error\n{message}"}, ensure_ascii=False, indent=2))
    else:
        print(f"evalctl: configuration error\n{message}", file=sys.stderr)
    return EXIT_CONFIG


def cmd_check(args: argparse.Namespace) -> int:
    try:
        trajectories = engine.env_trajectories(args.trajectory)
    except ConfigError as e:
        return _config_error(args, str(e))
    out = engine.check(args.dir, args.config, inputs_arg=args.inputs, worker_arg=args.worker,
                       profile_arg=args.profile, source=args.source, trajectories=trajectories)
    if args.json:
        print(json.dumps(out.data, ensure_ascii=False, indent=2))
    elif out.status == ERROR and not out.results:
        print(out.text, file=sys.stderr)
    else:
        print(out.text)
    return out.exit_code


def cmd_verify(args: argparse.Namespace) -> int:
    try:
        start = Path(args.dir).expanduser() if args.dir else Path.cwd()
        if not start.is_dir():
            raise ConfigError(f"not a directory: {start}")
        start = start.resolve()
        pointer = cfg.find_pointer(start)
        workdir = start if (args.dir or pointer is None) else pointer.parent
        if args.config or pointer is not None:
            _, cpath, _ = cfg.select(args.dir, args.config)
        else:
            # No pointer: fall back to the config the receipt itself names.
            rec = receipt.read(workdir) or {}
            recorded = (rec.get("config") or {}).get("path")
            cpath = Path(recorded) if recorded else None
            if cpath is not None and not cpath.is_file():
                raise ConfigError(f"the receipt names check config {cpath}, which no longer exists")
        conf = cfg.load(cpath, workdir) if cpath else None
        loop = lp.from_pointer(pointer, args.profile)
    except ConfigError as e:
        return _config_error(args, str(e))

    valid, reasons, rec = receipt.verify(workdir, conf, loop)
    if args.json:
        print(json.dumps({"valid": valid, "dir": str(workdir), "reasons": reasons, "receipt": rec},
                         ensure_ascii=False, indent=2))
    elif valid:
        rc = rec["config"]
        print(f"evalctl verify: VALID\n{workdir}: {len(rec['artifacts'])} file(s) unchanged since they "
              f"passed '{rc['name']}' at {rec['passed_at']}")
    else:
        print("evalctl verify: INVALID\n" + "\n".join(f"  - {r}" for r in reasons)
              + "\nRun `evalctl check` to get a fresh receipt.")
    return EXIT_PASS if valid else EXIT_FAIL


def cmd_stats(args: argparse.Namespace) -> int:
    from . import pairwise
    data = store.stats(args.config)
    judged = pairwise.summary(args.config)
    for name, j in judged.items():
        if name in data["configs"]:
            data["configs"][name]["judgements"] = j
    data["judgements"] = judged
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    if not data["configs"]:
        print("No runs recorded yet." + ("\n\n" + pairwise.render_summary(judged) if judged else ""))
        return 0
    for name, s in sorted(data["configs"].items()):
        print(f"{name}")
        print(f"  tasks: {s['tasks']}   check runs: {s['runs']}")
        print(f"  pass rate: {s['pass_rate']:.0%}   (first round: {s['first_round_pass_rate']:.0%})")
        print(f"  average rounds: {s['avg_rounds']:.1f}")
        print(f"  compliance rate: {s['compliance_rate']:.0%}   "
              f"({s['tasks_with_valid_receipt']} of {s['tasks']} tasks have a valid receipt)")
        if s["tasks_first_checked_by_hook"]:
            print(f"  tasks where the agent skipped the check and the hook ran it: {s['tasks_first_checked_by_hook']}")
        if s["config_error_runs"]:
            print(f"  runs that hit a setup error: {s['config_error_runs']}")
        if s["top_failing_checks"]:
            print("  most often failing checks:")
            for c in s["top_failing_checks"]:
                print(f"    {c['failed_rounds']:>3}x  {c['id']}")
        if s.get("tasks_needing_human_judgement"):
            print(f"  tasks whose repeat runs disagreed (need your judgement): {s['tasks_needing_human_judgement']}")
        if s.get("rubric_consistency"):
            print("  rubric items, agreement between repeated gradings:")
            for item, c in s["rubric_consistency"].items():
                print(f"    {c['consistency']:>4.0%}  {item}  ({c['runs']} run(s)"
                      + (f", unstable in {c['unstable_runs']}" if c["unstable_runs"] else "") + ")")
        jd = s.get("judgements")
        if jd and jd["graded"]:
            print(f"  llm_rubric vs your pairwise judgements: agreement {jd['agreement_rate']:.0%}  "
                  f"({jd['agree']} agree, {jd['disagree']} disagree, {jd['tie']} tie, of {jd['graded']} pairs)")
        elif jd:
            print(f"  pairwise judgements: {jd['judgements']} (none graded by an llm_rubric check)")
        fb = s.get("feedback")
        if fb:
            print(f"  your verdicts: {fb['verdicts']}   agreed with the checks: {fb['agree']}   "
                  f"checks too lenient: {fb['checks_too_lenient']}   too strict: {fb['checks_too_strict']}")
            for cid, k in fb["overruled"].items():
                print(f"    {k:>3}x  overruled: {cid}")
        print()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="evalctl", description="Quality gate for AI agent output.")
    ap.add_argument("--version", action="version", version=f"evalctl {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="run the checks; write a signed receipt when everything passes")
    c.add_argument("--dir", help="directory to check (default: the one holding .evalctl.yaml, else cwd)")
    c.add_argument("--config", help="check config name (in ~/.evalctl/checks) or path to a .yaml")
    c.add_argument("--json", action="store_true", help="print JSON instead of the text checklist")
    c.add_argument("--worker", help="model family that produced the output (anthropic, openai, deepseek, ...)")
    c.add_argument("--inputs", help="directory with the task's input files, for checks that compare "
                                    "the output with them (default: $EVALCTL_INPUTS, else `inputs:` in .evalctl.yaml)")
    c.add_argument("--profile", help="loop profile from .evalctl.yaml (default: $EVALCTL_PROFILE, else its `profile:`)")
    c.add_argument("--trajectory", action="append", metavar="FILE",
                   help="the agent's tool-call log, for action_policy checks (repeatable; default: $EVALCTL_TRAJECTORY)")
    c.add_argument("--source", default="cli", choices=["cli", "hook"], help=argparse.SUPPRESS)
    c.set_defaults(fn=cmd_check)

    v = sub.add_parser("verify", help="is the receipt valid for the files and config as they are now?")
    v.add_argument("--dir")
    v.add_argument("--config")
    v.add_argument("--profile", help="verify against this loop profile (default: $EVALCTL_PROFILE, else `profile:`)")
    v.add_argument("--json", action="store_true")
    v.set_defaults(fn=cmd_verify)

    s = sub.add_parser("stats", help="pass rate, rounds, failing checks and compliance per config")
    s.add_argument("--config")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_stats)

    from . import commands, teach
    teach.add_parsers(sub)
    commands.add_parsers(sub)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except ConfigError as e:
        return _config_error(args, str(e))


if __name__ == "__main__":
    sys.exit(main())
