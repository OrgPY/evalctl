"""`.eval/reconcile.md`: how the agent settled each difference `evalctl repeat` found.

One section per difference id, written by the agent that did the work, in its own session:

    ## D1
    adopted: 157.55
    evidence: expenses.csv lines 3, 6 and 10 (38.50 + 22.75 + 96.30)

or, when the inputs do not settle it:

    ## D2
    needs_human: the inputs do not say whether the refund on 2026-11-06 belongs to this week

evalctl does not decide who is right. It checks that every difference has an entry, that an
adopted value comes with evidence naming a real file, and counts what is left for the user.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import paths

NAME = "reconcile.md"
HEADING = re.compile(r"^#{1,6}\s*(D\d+)\b", re.M)
FIELD = re.compile(r"^\s*[-*]?\s*\**(adopted|evidence|needs_human)\**\s*[:：]\s*(.*)$", re.I | re.M)
FILE_NAME = re.compile(r"[\w./-]*\w\.\w{1,6}\b")


def path(workdir: Path) -> Path:
    return workdir / paths.EVAL_DIR / NAME


def parse(text: str) -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}
    marks = list(HEADING.finditer(text))
    for i, m in enumerate(marks):
        body = text[m.end(): marks[i + 1].start() if i + 1 < len(marks) else len(text)]
        fields: Dict[str, str] = {}
        for f in FIELD.finditer(body):
            fields.setdefault(f.group(1).lower(), f.group(2).strip())
        out[m.group(1)] = fields
    return out


def review(workdir: Path, divergences: List[Dict[str, Any]], known_files: List[str]) -> Tuple[List[str], Dict[str, Any]]:
    """-> (problems to fix in reconcile.md, summary {entries, resolved, needs_human: [ids]})."""
    ids = [d["id"] for d in divergences]
    p = path(workdir)
    if not ids:
        return [], {"entries": 0, "resolved": 0, "needs_human": []}
    if not p.is_file():
        return [f"{p.relative_to(workdir)} does not exist. `evalctl repeat` found {len(ids)} difference(s) "
                f"({', '.join(ids)}); settle each one in that file (format: see the repeat report)"], \
            {"entries": 0, "resolved": 0, "needs_human": []}
    entries = parse(p.read_text(encoding="utf-8", errors="replace"))
    names = {os.path.basename(f) for f in known_files}
    problems: List[str] = []
    resolved, needs = 0, []
    rel = p.relative_to(workdir)
    for d in divergences:
        e = entries.get(d["id"])
        if e is None:
            problems.append(f"{rel}: no section for {d['id']} ({d['summary']}). Add `## {d['id']}` with `adopted:` and "
                            "`evidence:`, or `needs_human:` if the inputs do not settle it")
            continue
        if e.get("needs_human"):
            needs.append(d["id"])
            continue
        if "needs_human" in e:
            problems.append(f"{rel}: {d['id']}: `needs_human:` is empty; say what could not be established and why")
            continue
        if not e.get("adopted"):
            problems.append(f"{rel}: {d['id']}: no `adopted:` line (the value or version you kept), and no `needs_human:`")
            continue
        cited = [os.path.basename(n) for n in FILE_NAME.findall(e.get("evidence", ""))]
        if not e.get("evidence"):
            problems.append(f"{rel}: {d['id']}: no `evidence:` line. Name the input file and the place in it "
                            "(line, row, section) that shows the adopted value is right")
        elif not any(n in names for n in cited):
            problems.append(f"{rel}: {d['id']}: the evidence names no file that exists in the task's files "
                            f"(\"{e['evidence'][:120]}\"). Cite the input file and the place in it; if nothing in the "
                            "inputs settles it, write `needs_human:` instead of choosing")
        else:
            resolved += 1
    return problems, {"entries": len([i for i in ids if i in entries]), "resolved": resolved, "needs_human": needs}
