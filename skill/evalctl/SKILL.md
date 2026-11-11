---
name: evalctl
description: Quality gate for deliverables. Use before handing back any output (files, data, reports, code) in a directory that has a .evalctl.yaml, or whenever the user mentions evalctl. Run `evalctl check`, fix only what failed, re-check, and deliver only with a PASS receipt.
---

# evalctl: check before you deliver

Your work in this directory is not finished until `evalctl check` passes. A pass writes a signed receipt to `.eval/receipt.json`; the user verifies it, so skipped or stale checks are visible.

## Loop

1. When you believe the output is ready, run `evalctl check` in the working directory.
2. `PASS`: deliver. Do not edit the output afterwards; any edit invalidates the receipt. If you must edit, run the check again.
3. `FAIL`: every failed item states where the problem is and why. Fix exactly those items. Leave everything that passed untouched. Then run `evalctl check` again.
4. At most 3 rounds. The output tells you which round you are on.

## If it still fails after 3 rounds

Stop. Tell the user the output did not pass and paste the remaining failed items exactly as evalctl printed them. Do not present the output as done or passing.

## Rules

- Deliver only with a `PASS` and a receipt. No receipt means not delivered.
- Do not argue that a check is wrong, and do not work around it. If you think a rule is mistaken, say so to the user after reporting the failure; the user decides.
- Never create, edit or delete anything in `.eval/`, the `.evalctl.yaml` file, or the check configs in `~/.evalctl/`.
- Fix the output itself. Do not pad, duplicate, invent or delete data just to satisfy a check.
- `ERROR` (exit code 2) means evalctl itself could not run the checks, or found a problem in the check rules. That is a setup problem: report it to the user and stop; do not treat it as a pass.
- Lines starting with `note:` are for the user. They need no fix from you; pass them on when you report.
- If the instruction that brought you here says the check runs automatically (you were started by `evalctl run`), do not run `evalctl check` yourself: fix the listed items and stop.

Exit codes: 0 pass, 1 failures, 2 setup error. `evalctl check --json` gives the same result as JSON.
