"""Turn check results into the checklist for humans/agents and into JSON."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .checks import ERROR, FAIL, PASS, SKIPPED, Result
from .config import Config

MARK = {PASS: "[PASS]", FAIL: "[FAIL]", ERROR: "[ERROR]", SKIPPED: "[SKIP]"}


def overall(results: List[Result]) -> str:
    statuses = {r.status for r in results}
    if ERROR in statuses:
        return ERROR
    if FAIL in statuses:
        return FAIL
    return PASS


def next_step(status: str, round_no: Optional[int], max_rounds: int, mode: str = "skill",
              receipt: bool = True) -> str:
    """`mode` is skill (the agent called check itself) or run / bench (evalctl drives the agent)."""
    if status == PASS and not receipt:
        return "All checks passed. No receipt yet: the repeat runs decide whether one is written."
    if status == PASS:
        return "All checks passed. Receipt written; the output can be delivered as is. Any further edit invalidates the receipt."
    if status == ERROR:
        return ("evalctl could not evaluate the output (see ERROR items). This is a setup problem, not "
                "something to fix in the output: stop and report it to the user. No receipt was written.")
    if round_no is not None and round_no >= max_rounds:
        return (f"Round {round_no} of {max_rounds}: round limit reached. Stop iterating. Report the FAIL "
                "items above to the user exactly as listed. Do not deliver this output as passing.")
    if mode != "skill":
        return (f"Round {round_no} of {max_rounds}. Fix only the FAIL items above, leave everything that "
                "passed untouched, then stop. The check runs again automatically; do not run evalctl yourself.")
    return (f"Round {round_no} of {max_rounds}. Fix only the FAIL items above, leave everything that "
            "passed untouched, then run `evalctl check` again.")


def render_text(config: Config, workdir: Any, results: List[Result], status: str,
                round_no: Optional[int], receipt_path: Optional[str], mode: str = "skill",
                notes: Optional[List[str]] = None, run_id: Optional[int] = None,
                receipt_expected: bool = True) -> str:
    n_fail = sum(r.status == FAIL for r in results)
    n_err = sum(r.status == ERROR for r in results)
    n_skip = sum(r.status == SKIPPED for r in results)
    head = {PASS: "PASS", FAIL: "FAIL", ERROR: "ERROR"}[status]
    counts = f"{sum(r.status == PASS for r in results)}/{len(results)} checks passed"
    if n_fail:
        counts += f", {n_fail} failed"
    if n_err:
        counts += f", {n_err} could not run"
    if n_skip:
        counts += f", {n_skip} skipped"
    lines = [
        f"evalctl check: {head}  ({counts})",
        f"config: {config.name}   dir: {workdir}" + (f"   round: {round_no}/{config.max_rounds}" if round_no else "")
        + (f"   run: #{run_id}" if run_id else ""),
        "",
    ]
    order = {ERROR: 0, FAIL: 1, SKIPPED: 2, PASS: 3}
    for r in sorted(results, key=lambda r: order[r.status]):
        lines.append(f"{MARK[r.status]} {r.id} ({r.type})" + (f": {r.summary}" if r.summary else ""))
        if r.status in (FAIL, ERROR):
            for f in r.failures:
                for i, ln in enumerate(str(f).splitlines() or [""]):
                    if i == 0 and not ln.startswith("  "):
                        lines.append("    - " + ln.strip())
                    else:
                        lines.append("      " + (ln.strip() if i == 0 else ln.rstrip()))
            if r.hint and r.status == FAIL:
                lines.append(f"    how to fix: {r.hint}")
        note = r.meta.get("note") if r.meta else None
        if note:
            lines.append(f"    note: {note}")
    lines.append("")
    for n in notes or []:
        lines.append(f"note: {n}")
    if receipt_path:
        lines.append(f"receipt: {receipt_path}")
    lines.append(next_step(status, round_no, config.max_rounds, mode, receipt_expected))
    return "\n".join(lines)


def to_json(config: Config, workdir: Any, results: List[Result], status: str,
            round_no: Optional[int], receipt_path: Optional[str], text: str, **more: Any) -> Dict[str, Any]:
    out = {
        "status": status,
        "passed": status == PASS,
        "config": {"name": config.name, "path": str(config.path), "sha256": config.sha256},
        "dir": str(workdir),
        "round": round_no,
        "max_rounds": config.max_rounds,
        "receipt": receipt_path,
        "checks": [r.to_dict() for r in results],
        "next_step": text.splitlines()[-1] if text else "",
        "text": text,
    }
    out.update(more)
    return out
