---
name: evalctl
description: Quality gate for deliverables. Use at the START of any task whose output will be handed back (files, data, reports, code) when the directory has a .evalctl.yaml, when the user's request says the task is important, or whenever the user mentions evalctl. Write an acceptance contract from the request, check the finished work against it, repeat and reconcile when the profile asks, and deliver only with a receipt.
---

# evalctl: contract, check, repeat, deliver

The work is finished when `evalctl check` passes and has written a signed receipt to `.eval/receipt.json`. The user verifies it, so skipped or stale checks are visible.

## 1. When you start: the contract

Before doing any work, run in the working directory:

    evalctl contract --task "<the user's request, word for word>"

A separate model turns the request into an acceptance checklist, stored outside the directory. Pass the request unchanged; do not summarise or soften it. Skip this step only if you were told the contract is already being written (a hook did it), or if `evalctl contract` says so.

## 2. When the work is done: check

1. Run `evalctl check`.
2. `FAIL`: every failed item says where the problem is and why. Fix exactly those items, leave what passed untouched, and run `evalctl check` again. At most 3 rounds; the output says which round you are on.
3. Still failing after 3 rounds: stop. Tell the user the output did not pass and paste the remaining failed items exactly as printed.

## 3. If check asks for a repeat

Some tasks run under a profile that requires independent repeats. `evalctl check` then fails with an item named `repeat` that tells you to run:

    evalctl repeat

It starts independent runs of the same task from the original files and takes several minutes. Use the longest command timeout you have, or run it in the background and wait for it to finish. It returns the differences between your output and the other runs. Then:

- For each difference, go back to the original input files and establish what is right. Never pick a value because more runs have it, and never copy from the other runs.
- Correct your output where you were wrong.
- Write `.eval/reconcile.md`, one section per difference: `## D1`, then `adopted: <value>` and `evidence: <input file and the place in it>`.
- If the inputs do not settle a difference, do not choose. Write `needs_human: <what could not be established and why>` in its section.
- Run `evalctl check` again.

If the environment variable `EVALCTL_CHILD` is set, you are one of those independent runs: do not run `evalctl repeat`; do the task and stop.

## 4. Deliver

Deliver only with a `PASS` and a receipt. In your final message state:

- how many acceptance contract items passed (check prints the count),
- each difference between the repeat runs and how you settled it,
- every `needs_human` item, exactly as written in `.eval/reconcile.md`.

## Rules

- Do not edit the output after the receipt is written; any edit invalidates it. If you must, run the check again.
- Do not argue that a check or a contract item is wrong, and do not work around it. If you think one is mistaken, say so to the user after reporting the failure; the user decides.
- Never create, edit or delete the acceptance contract, `.eval/receipt.json`, `.evalctl.yaml`, or anything in `~/.evalctl/`. The only file in `.eval/` you write is `reconcile.md`.
- Fix the output itself. Do not pad, duplicate, invent or delete data to satisfy a check.
- `ERROR` (exit code 2) means evalctl could not run the checks, or found a problem in the check rules. That is a setup problem: report it to the user and stop; it is not a pass.
- Lines starting with `note:` are for the user. They need no fix; pass them on when you report.
- If the instruction that brought you here says the check runs automatically (you were started by `evalctl run`), do not run `evalctl check` or `evalctl contract` yourself: do the work, fix the listed items, and stop.

Exit codes: 0 pass, 1 failures, 2 setup error. `evalctl check --json` gives the same result as JSON.
