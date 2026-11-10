from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import List, Optional

from . import __version__, config as cfg, receipt, report, store
from .checks import ERROR, FAIL, PASS, REGISTRY, SKIPPED, SOFT_TYPES, Context, Result
from .config import ConfigError
from .judge import detect_worker

EXIT_PASS, EXIT_FAIL, EXIT_CONFIG = 0, 1, 2


def run_checks(conf: cfg.Config, workdir: Path, worker: Optional[str]) -> List[Result]:
    """Hard checks first; model-graded checks only when every hard check passed."""
    ctx = Context(workdir=workdir, config=conf, worker=worker)
    results: dict = {}
    hard = [c for c in conf.checks if c.type not in SOFT_TYPES]
    soft = [c for c in conf.checks if c.type in SOFT_TYPES]
    for spec in hard:
        try:
            results[spec.id] = REGISTRY[spec.type].run(spec, ctx)
        except ConfigError as e:
            results[spec.id] = Result(spec.id, spec.type, ERROR, str(e), [str(e)])
    hard_ok = all(results[s.id].status == PASS for s in hard)
    for spec in soft:
        if not hard_ok:
            results[spec.id] = Result(spec.id, spec.type, SKIPPED,
                                      "not run: runs only after all other checks pass")
            continue
        try:
            results[spec.id] = REGISTRY[spec.type].run(spec, ctx)
        except ConfigError as e:
            results[spec.id] = Result(spec.id, spec.type, ERROR, str(e), [str(e)])
    return [results[c.id] for c in conf.checks]


def _config_error(args: argparse.Namespace, message: str, workdir: Optional[Path] = None,
                  name: str = "?") -> int:
    if getattr(args, "json", False):
        print(json.dumps({"status": "error", "passed": False, "error": message,
                          "text": f"evalctl: configuration error\n{message}"}, ensure_ascii=False, indent=2))
    else:
        print(f"evalctl: configuration error\n{message}", file=sys.stderr)
    if workdir is not None and args.cmd == "check":
        try:
            store.record(workdir, name, None, None, None, None, "error", args.source, None, 0.0,
                         [{"id": "config", "type": "config", "status": "error", "summary": message}])
        except Exception:
            pass
    return EXIT_CONFIG


def cmd_check(args: argparse.Namespace) -> int:
    started = time.time()
    try:
        workdir, cpath, _ = cfg.select(args.dir, args.config)
    except ConfigError as e:
        return _config_error(args, str(e))
    try:
        conf = cfg.load(cpath, workdir)
    except ConfigError as e:
        return _config_error(args, str(e), workdir, cpath.stem)

    worker = detect_worker(args.worker, conf.worker_family)
    digest = receipt.digest(receipt.hash_artifacts(workdir, conf.artifacts))
    task_id, round_no = store.plan_round(workdir, conf.name, conf.sha256, digest)

    results = run_checks(conf, workdir, worker)
    status = report.overall(results)
    receipt_file = None
    if status == PASS:
        try:
            receipt_file = str(receipt.write(workdir, conf, results, round_no))
        except ConfigError as e:
            results.append(Result("receipt", "receipt", ERROR, str(e), [str(e)]))
            status = ERROR
    if status == FAIL:
        receipt.invalidate(workdir)

    is_error = status == ERROR
    store.record(workdir, conf.name, conf.sha256, digest,
                 None if is_error else task_id, None if is_error else round_no,
                 status, args.source, worker, time.time() - started, [r.to_dict() for r in results])

    shown_round = None if is_error else round_no
    text = report.render_text(conf, workdir, results, status, shown_round, receipt_file)
    if args.json:
        print(json.dumps(report.to_json(conf, workdir, results, status, shown_round, receipt_file, text),
                         ensure_ascii=False, indent=2))
    else:
        print(text)
    return {PASS: EXIT_PASS, FAIL: EXIT_FAIL, ERROR: EXIT_CONFIG}[status]


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
    except ConfigError as e:
        return _config_error(args, str(e))

    valid, reasons, rec = receipt.verify(workdir, conf)
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
    data = store.stats(args.config)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    if not data["configs"]:
        print("No runs recorded yet.")
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
    c.add_argument("--source", default="cli", choices=["cli", "hook"], help=argparse.SUPPRESS)
    c.set_defaults(fn=cmd_check)

    v = sub.add_parser("verify", help="is the receipt valid for the files and config as they are now?")
    v.add_argument("--dir")
    v.add_argument("--config")
    v.add_argument("--json", action="store_true")
    v.set_defaults(fn=cmd_verify)

    s = sub.add_parser("stats", help="pass rate, rounds, failing checks and compliance per config")
    s.add_argument("--config")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_stats)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except ConfigError as e:
        return _config_error(args, str(e))


if __name__ == "__main__":
    sys.exit(main())
