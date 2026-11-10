"""The commands that build and tune a check config from examples: learn, review, feedback."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from . import config as cfg, learn as lrn, paths, receipt, report, store
from .checks import ERROR, FAIL, PASS, REGISTRY
from .config import ConfigError
from .judge import detect_worker


def _dir(value: Optional[str], flag: str, required: bool = True) -> Optional[Path]:
    if not value:
        if required:
            raise ConfigError(f"{flag} is required")
        return None
    p = Path(value).expanduser()
    if not p.is_dir():
        raise ConfigError(f"{flag}: not a directory: {p}")
    return p.resolve()


# ---------------------------------------------------------------- learn

def cmd_learn(args: argparse.Namespace) -> int:
    name = lrn.check_name(args.name)
    inputs = _dir(args.inputs, "--inputs", required=False)
    draft, final = _dir(args.draft, "--draft"), _dir(args.final, "--final")
    if draft == final:
        raise ConfigError("--draft and --final are the same directory; learn needs the output before "
                          "and after the human edits")
    if not lrn.list_files(final):
        raise ConfigError(f"--final: {final} has no files")
    doc = lrn.learn(inputs, draft, final, name)
    saved = lrn.save(doc)
    if args.json:
        print(json.dumps(dict(doc, saved_to=str(saved)), ensure_ascii=False, indent=2))
    else:
        if inputs is None:
            print("note: no --inputs given, so no input/output relation checks were looked for.\n")
        print(lrn.render(doc, saved))
    return 0


# ---------------------------------------------------------------- review

def _spec_error(check: Any) -> Optional[str]:
    if not isinstance(check, dict) or "type" not in check:
        return "a check is a mapping with at least `type`"
    if check["type"] not in REGISTRY:
        return f"unknown check type '{check['type']}' (supported: {', '.join(sorted(REGISTRY))})"
    spec = cfg.CheckSpec("edited", check["type"], {k: v for k, v in check.items() if k not in ("id", "type", "hint")})
    try:
        with tempfile.TemporaryDirectory() as scratch:
            REGISTRY[check["type"]].validate(spec, paths.checks_dir(), Path(scratch))
    except ConfigError as e:
        return str(e)
    return None


def _edit(doc: Dict[str, Any], c: Dict[str, Any], ask) -> None:
    """Open the check in $EDITOR; keep the result only if it is a valid check."""
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    with tempfile.TemporaryDirectory() as scratch:
        f = Path(scratch) / f"{c['id']}.yaml"
        f.write_text(f"# {c['id']}: edit the check, save and quit. Lines starting with # are ignored.\n"
                     f"# basis: {c['basis']}\n"
                     + yaml.safe_dump(c["check"], sort_keys=False, allow_unicode=True, width=110), encoding="utf-8")
        try:
            subprocess.call(shlex.split(editor) + [str(f)])
            new = yaml.safe_load(f.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as e:
            print(f"  could not edit: {e}. The check is unchanged.")
            return
    if new == c["check"]:
        print("  no change.")
        return
    problem = _spec_error(new)
    if problem:
        print(f"  not a valid check: {problem}\n  The check is unchanged.")
        return
    old, old_validation = c["check"], c.get("validation")
    new.pop("id", None)
    c["check"] = new
    status, why = lrn.revalidate(doc, c)
    if status is None:
        print("  edited. (The example it was learned from is gone, so it was not re-validated.)")
    elif status != PASS:
        print(f"  the edited check does not pass on the final output: {why}")
        if ask("  keep the edit anyway? [y/N] ").strip().lower() != "y":
            c["check"], c["validation"] = old, old_validation
            print("  edit dropped.")
            return
    else:
        print(f"  edited: final {status}, draft {c['validation']['draft'] or '?'}")
    c["edited"] = True


def cmd_review(args: argparse.Namespace) -> int:
    doc = lrn.load(args.name)
    cands: List[Dict[str, Any]] = doc["candidates"]
    by_id = {c["id"]: c for c in cands}
    for flag, value in (("--enable", True), ("--disable", False)):
        for cid in getattr(args, "enable" if value else "disable") or []:
            if cid not in by_id:
                raise ConfigError(f"{flag} {cid}: no such candidate. Candidates: {', '.join(by_id) or '(none)'}")
            by_id[cid]["enabled"] = value
    if not cands:
        print(f"'{args.name}' has no candidate checks. Run `evalctl learn` on a draft/final pair first.")
        return 1

    def ask(prompt: str) -> str:
        return input(prompt)

    target = paths.checks_dir() / f"{doc['name']}.yaml"
    try:
        if not args.yes:
            print(f"Reviewing {len(cands)} candidate check(s) for '{doc['name']}', learned {doc.get('learned_at', '?')}.")
            print("For each: e = enable, d = disable, i = edit in $EDITOR, Enter = keep as shown, q = quit without saving.\n")
            i = 0
            while i < len(cands):
                c = cands[i]
                print(f"({i + 1}/{len(cands)}) " + "\n".join(lrn.candidate_lines(c, indent="      ")))
                for e in doc.get("edits", []):
                    if e["id"] in (c.get("edits") or []):
                        print(f"      {e['id']} {e['where']}: {e['summary']}")
                a = ask(f"  [e]nable [d]isable ed[i]t [Enter]=keep {'on' if c['enabled'] else 'off'} [q]uit > ").strip().lower()
                if a == "q":
                    print("Quit. Nothing was written.")
                    return 1
                if a == "i":
                    _edit(doc, c, ask)
                    print()
                    continue  # show it again
                if a in ("e", "d"):
                    c["enabled"] = a == "e"
                elif a:
                    print("  please answer e, d, i, q or just Enter")
                    continue
                print()
                i += 1
        on = [c for c in cands if c["enabled"]]
        print(f"{len(on)} of {len(cands)} check(s) enabled:")
        for c in cands:
            print(f"  {'[on ]' if c['enabled'] else '[off]'} {c['id']}  ({c['kind']}, {lrn.SCOPE_LABEL.get(c['scope'], c['scope'])})")
        if not args.yes:
            verb = "Update" if target.is_file() else "Create"
            if ask(f"{verb} {target} with the enabled checks? [y/N] ").strip().lower() != "y":
                print("Nothing was written.")
                return 1
    except EOFError:
        print("\nNo answer (input closed). Nothing was written. Use --yes to accept without prompting.")
        return 1

    path, written, kept, backup = lrn.write_config(doc)
    print(f"Wrote {path}: {written} learned check(s)" + (f", {kept} existing check(s) kept" if kept else "") + ".")
    if backup:
        print(f"The previous version is at {backup}.")
    print(f"To gate a directory with it, put `config: {doc['name']}` in its .evalctl.yaml"
          + (" (add `inputs: <dir>` for the checks that read the task inputs)"
             if any(getattr(REGISTRY.get(c["check"].get("type")), "needs_inputs", False) for c in on) else "") + ".")
    return 0


# ---------------------------------------------------------------- feedback

def cmd_feedback(args: argparse.Namespace) -> int:
    workdir, cpath, pointer = cfg.select(args.dir, args.config)
    conf = cfg.load(cpath, workdir)
    inputs = cfg.resolve_inputs(args.inputs, pointer)
    digest = receipt.digest(receipt.hash_artifacts(workdir, conf.artifacts))

    prior = store.last_results(workdir, conf.name, conf.sha256, digest)
    if prior:
        status, results = prior
        ran_now = False
    else:  # nobody has checked these exact files yet: check them now, without a receipt or a run record
        from .cli import run_checks
        res = run_checks(conf, workdir, detect_worker(None, conf.worker_family), inputs)
        status, results, ran_now = report.overall(res), [r.to_dict() for r in res], True
    failed = [r for r in results if r["status"] == FAIL]
    store.record_feedback(workdir, conf.name, digest, args.verdict, status, [r["id"] for r in failed], args.note)

    out: Dict[str, Any] = {"dir": str(workdir), "config": conf.name, "verdict": args.verdict,
                           "check_status": status, "agrees": None, "failed_checks": [r["id"] for r in failed]}
    lines = [f"evalctl feedback: recorded \"{args.verdict}\" for {workdir} (config {conf.name})"
             + (f", note: {args.note}" if args.note else ""),
             f"checks on the same files: {status.upper()}" + (" (run just now; no receipt written)" if ran_now else "")]

    if status == ERROR:
        lines.append("The checks could not run, so there is nothing to compare your verdict with. "
                     "Run `evalctl check` to see the setup problem.")
    elif status == PASS and args.verdict == "bad":
        out["agrees"] = False
        snap = paths.home() / "feedback" / conf.name / time.strftime("%Y%m%d-%H%M%S") / "draft"
        snap.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(workdir, snap, ignore=shutil.ignore_patterns(paths.EVAL_DIR, ".git"))
        out.update(action="learn", snapshot=str(snap))
        lines += [
            "",
            "DISAGREEMENT: the checks passed this output, but you judged it bad. The checks have a blind spot.",
            f"The output as it is now was copied to {snap}",
            f"Fix the output in {workdir} by hand, then let evalctl learn from your edits:",
            f"    evalctl learn --inputs {inputs or '<inputs dir>'} --draft {snap} --final {workdir} --name {conf.name}",
            f"    evalctl review {conf.name}",
        ]
    elif status == FAIL and args.verdict == "good":
        out["agrees"] = False
        overruled = store.feedback_summary(conf.name).get(conf.name, {}).get("overruled", {})
        learned = lrn.learned_path(conf.name).is_file()
        out.update(action="relax", config_path=str(conf.path))
        lines += ["", "DISAGREEMENT: the checks failed this output, but you judged it good. "
                      f"{'This check is' if len(failed) == 1 else 'These checks are'} stricter than you are:"]
        for r in failed:
            n = overruled.get(r["id"], 1)
            lines.append(f"  - {r['id']} ({r['type']}): {r.get('summary', '')}"
                         + (f"   [overruled {n} times]" if n > 1 else ""))
            lines += [f"      {f}" for f in (r.get("failures") or [])[:3]]
        lines += [f"Relax or delete {'it' if len(failed) == 1 else 'them'} in {conf.path}"
                  + (f", or run `evalctl review {conf.name}` and disable or edit {'it' if len(failed) == 1 else 'them'} there."
                     if learned else ".")]
    else:
        out["agrees"] = True
        lines.append("Your verdict and the checks agree.")
        if args.verdict == "bad":
            lines.append("Failing checks: " + ", ".join(r["id"] for r in failed))

    if args.json:
        print(json.dumps(dict(out, text="\n".join(lines)), ensure_ascii=False, indent=2))
    else:
        print("\n".join(lines))
    return 0


def add_parsers(sub: Any) -> None:
    l = sub.add_parser("learn", help="propose checks from one example: the inputs, the agent's draft, your corrected final")
    l.add_argument("--inputs", help="directory with the files the task started from")
    l.add_argument("--draft", required=True, help="the output as the agent produced it")
    l.add_argument("--final", required=True, help="the same output after you corrected it")
    l.add_argument("--name", required=True, help="name of the check config to build")
    l.add_argument("--json", action="store_true")
    l.set_defaults(fn=cmd_learn)

    r = sub.add_parser("review", help="go through learned candidate checks, enable / disable / edit, write the config")
    r.add_argument("name")
    r.add_argument("--enable", action="append", metavar="ID", help="enable this candidate (repeatable)")
    r.add_argument("--disable", action="append", metavar="ID", help="disable this candidate (repeatable)")
    r.add_argument("--yes", action="store_true", help="do not prompt: write the config with the current choices")
    r.set_defaults(fn=cmd_review)

    f = sub.add_parser("feedback", help="record your own verdict on an output and compare it with the checks")
    f.add_argument("--dir", help="the output directory (default: the one holding .evalctl.yaml, else cwd)")
    f.add_argument("--verdict", required=True, choices=["good", "bad"])
    f.add_argument("--note", help="what you saw, in your own words")
    f.add_argument("--config")
    f.add_argument("--inputs")
    f.add_argument("--json", action="store_true")
    f.set_defaults(fn=cmd_feedback)
