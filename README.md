# evalctl

[中文说明](README.zh.md)

A small quality gate for AI agent output. The agent runs `evalctl check` before it hands work back, fixes what failed, and re-checks. When everything passes, evalctl writes a signed receipt. No valid receipt means the check was skipped, or the output was edited after it passed.

- Works with any agent that can run a shell command (Claude Code, Codex, Hermes, your own harness).
- Check rules live outside the agent's working directory, so the agent cannot edit them.
- With Claude Code, a Stop hook turns the request into enforcement.

v0 scope: `check`, `verify`, `stats`, one skill, one Stop hook, one example workflow (forum scraping). No proxy, no file watcher, no GUI.

## Install

Requires Python 3.9+. The only dependency is PyYAML.

```bash
cd ~/evalctl
uv tool install --editable .        # or: pipx install -e .   or: pip install -e .
evalctl --version
```

State lives in `~/.evalctl/`:

| Path | What |
| --- | --- |
| `checks/<name>.yaml` | check configs |
| `judge.yaml` | grader models for `llm_rubric` checks |
| `key` | HMAC signing key, created on the first pass, mode 600 |
| `runs.sqlite` | log of every check run |

## Try it

```bash
mkdir -p ~/.evalctl/checks
cp checks/forum_scrape.yaml ~/.evalctl/checks/
cp judge.example.yaml ~/.evalctl/judge.yaml      # then edit: pick the graders you can use

evalctl check --dir fixtures/forum_scrape/duplicate_url --config forum_scrape
```

```text
evalctl check: FAIL  (4/6 checks passed, 1 failed, 1 skipped)
config: forum_scrape   dir: .../fixtures/forum_scrape/duplicate_url   round: 1/3

[FAIL] unique_urls (schema): 2 problem(s)
    - posts.jsonl:9: duplicate url "https://forum.homelab.example/t/1054" (first seen at posts.jsonl:2); keep one, remove or replace the other
    - posts.jsonl:17: duplicate url "https://forum.homelab.example/t/1075" (first seen at posts.jsonl:5); keep one, remove or replace the other
    how to fix: Each post may appear once. Remove the repeated line, or fix the url if two different posts got the same one.
[SKIP] body_is_post_content (llm_rubric): not run: runs only after all other checks pass
[PASS] posts_file (file): 1 file(s) OK
...
Round 1 of 3. Fix only the FAIL items above, leave everything that passed untouched, then run `evalctl check` again.
```

The other fixtures: `good` (passes), `missing_field`, `too_few`, `nav_body` (structurally fine, bodies are navigation and ads; only the grader model catches it).

## Commands

```text
evalctl check  [--dir DIR] [--config NAME] [--json] [--worker FAMILY]
evalctl verify [--dir DIR] [--config NAME] [--json]
evalctl stats  [--config NAME] [--json]
```

**check.** Picks the config from `--config`, else from the nearest `.evalctl.yaml` in the directory or its parents. Without `--dir`, the directory that holds `.evalctl.yaml` is the one checked, so the command works from a subdirectory. Hard checks run first; `llm_rubric` checks run only when all hard checks pass. Output is a text checklist, or JSON with `--json` (the JSON also carries the text).

| Exit code | Meaning |
| --- | --- |
| 0 | all checks passed, receipt written |
| 1 | at least one check failed |
| 2 | setup error: no or broken config, no usable grader model, a check that could not run |

**verify.** Exit 0 only if `.eval/receipt.json` has a valid signature, every covered file still has the recorded hash (none changed, deleted or added), and the config, including any Python check modules, is unchanged. It also fails when `.evalctl.yaml` now points to a different config, or the receipt was copied from another directory.

**stats.** Per config: tasks, pass rate, first-round pass rate, average rounds, most often failing checks, and compliance rate.

- A *round* goes up each time `check` runs on changed output. Re-running on identical files stays in the same round.
- A *task* is the series of runs for one directory and config, up to the first pass. A failing task left alone for 6 hours counts as abandoned.
- *Compliance rate* = tasks with a valid receipt / all tasks. A task that passed and was then edited without a re-check does not count.
- Blind spot: a task where the agent never ran `check` at all leaves no trace here unless the Stop hook caught it. For agents without a hook, run `evalctl verify --dir <output dir>` yourself before accepting the work.

## Writing a check config for a new workflow

1. Create `~/.evalctl/checks/<name>.yaml`. Start from `checks/forum_scrape.yaml`.
2. In the workflow's output directory, create `.evalctl.yaml` with one line: `config: <name>`.
3. Produce one good output and a few bad ones by hand, and run `evalctl check` on each until the failures read like instructions you would give yourself.

```yaml
name: weekly_report
artifacts: [report.md, "charts/*.png"]   # files the receipt covers; default: every file in the directory
max_rounds: 3                            # default 3

checks:
  - id: report_exists          # ids appear in the output and in stats
    type: file
    path: report.md
    min_size: 500
    hint: Text shown under the failure as "how to fix".   # optional on every check

  - id: no_placeholders
    type: contains
    file: report.md
    must: ["## Summary", "## Sources"]
    must_not: ["TODO", "lorem ipsum"]
    ignore_case: true
```

### Check types

| Type | Options | Passes when |
| --- | --- | --- |
| `shell` | `command`, `timeout` (120), `expect_exit` (0) | the command, run in the directory, exits with the expected code. On failure the last 30 output lines are shown. `$EVALCTL_DIR` is set. |
| `schema` | `file`, `format` (json/jsonl/csv, inferred from the extension), `record_schema`, `schema`, `columns`, `min_records`, `max_records`, `unique` | every record matches `record_schema` (a JSONL line, a CSV row, an element of a top-level JSON array); the whole JSON document matches `schema`; the CSV header has `columns`; the record count is in range; `unique` fields have no repeated values. |
| `contains` | `file` (glob), `must`, `must_not`, `ignore_case` | every `must` string appears in each matching file and no `must_not` string appears. Literal text. |
| `regex` | same as `contains` | same, with Python regular expressions (multiline mode). |
| `file` | `path` (glob), `exists` (true), `min_count`, `max_count`, `name_regex`, `min_size`, `max_size` | files exist (or with `exists: false`, do not), the number of matches is in range, each basename fully matches `name_regex`, each size is in range. Sizes are bytes or `10KB` / `2MB`. |
| `python` | `module`, `function`, `args` | `function(dir, **args)` returns `(True, detail)`. `module` is a `.py` file next to the config. An exception or a wrong return shape is a setup error (exit 2), not a verdict. |
| `llm_rubric` | `file`, `rubric`, `sample`, `fields`, `min_pass_rate` (1.0), `max_chars` (6000), `judge` | a grader model passes every rubric line for every graded item. |

The schema language is a subset of JSON Schema: `type`, `required`, `properties`, `additionalProperties: false`, `items`, `enum`, `const`, `minLength`, `maxLength`, `pattern`, `minimum`, `maximum`, `minItems`, `maxItems`, `format` (`uri`, `date-time`, `date`, `email`). Any other keyword is rejected when the config loads, so a rule is never silently ignored. For CSV, empty cells count as missing and cells are converted to the number or boolean type the schema declares.

A custom Python check:

```python
# ~/.evalctl/checks/report_checks.py
import os

def charts_referenced(dir, min_charts=1):
    text = open(os.path.join(dir, "report.md")).read()
    missing = [f for f in os.listdir(os.path.join(dir, "charts")) if f not in text]
    if missing:
        return False, "\n".join(f"report.md: chart {f} is never referenced" for f in missing)
    return True, "all charts referenced"
```

```yaml
  - id: charts_referenced
    type: python
    module: report_checks.py
    function: charts_referenced
    args: {min_charts: 2}
```

Each line of the returned detail becomes one failure item.

### llm_rubric and grader models

```yaml
  - id: body_is_post_content
    type: llm_rubric
    file: posts.jsonl
    sample: 10                  # omit to grade every record
    fields: [title, url, body]  # what the grader sees
    rubric:
      - id: real_post_body
        text: The body is the actual text of the post, not navigation, banners or ads.
```

- For JSONL, CSV and JSON-array files each record is an item. Any other file is graded as one item.
- The sample is random but depends only on the file content: the same file gives the same sample, a changed file gives a new one.
- The grader gets one request per item and must answer every rubric line with pass or fail plus a reason. When the answer is ambiguous the item fails. An unreachable grader or an unusable answer is a setup error (exit 2), never a pass.
- An item longer than `max_chars` is shortened in the middle, and the grader is told so.

Graders are looked up on the check (`judge:`), then on the config (`judge:`), then in `~/.evalctl/judge.yaml`. Each place may hold one judge or a list. See `judge.example.yaml`.

| Provider | Needs | Notes |
| --- | --- | --- |
| `anthropic` | `model`, `ANTHROPIC_API_KEY` | Messages API |
| `openai` | `model`, `OPENAI_API_KEY` | Chat Completions |
| `deepseek` | `model`, `DEEPSEEK_API_KEY` | OpenAI-compatible, `https://api.deepseek.com` |
| `openai_compatible` | `model`, `base_url`, `api_key_env`, `family` | any other `/chat/completions` endpoint |
| `command` | `command`, `family` | prompt on stdin, answer on stdout, run in an empty temp directory. Lets you grade with an agent CLI you are logged in to, without an API key. |

**The grader must be from a different model family than the worker.** evalctl uses the first judge in the list that is from another family and is usable (key exported, command installed). List two families and every agent you use has a grader. The worker family comes from `--worker`, then `$EVALCTL_WORKER`, then detection (Claude Code sets `CLAUDECODE`, which means `anthropic`), then `worker_family:` in the config. For agents other than Claude Code, export `EVALCTL_WORKER` (`openai`, `deepseek`, ...) in the environment they run commands in. If the family is unknown, evalctl still grades and prints a note that the rule was not enforced. To accept same-family grading for one judge, set `allow_same_family: true` on it.

## The skill

`skill/evalctl/SKILL.md` is the same for every workflow and every agent. It tells the agent to run `evalctl check` before delivering, fix only the failed items, stop after 3 rounds, report remaining failures as printed, and never touch `.eval/` or the configs.

| Agent | Install |
| --- | --- |
| Claude Code | `mkdir -p ~/.claude/skills && cp -r skill/evalctl ~/.claude/skills/` (all projects), or into `<project>/.claude/skills/` |
| Codex | `cp -r skill/evalctl ~/.codex/skills/`. If your Codex version does not load skills, append the body of `SKILL.md` to `~/.codex/AGENTS.md` or the project's `AGENTS.md`, and export `EVALCTL_WORKER=openai`. |
| Hermes | `cp -r skill/evalctl ~/.hermes/skills/` |
| Anything else | paste the body of `SKILL.md` into the system prompt or the agent's instructions file. The only requirement is that the agent can run shell commands. For a GUI agent with no shell, run `evalctl check` yourself and paste the output back as the fix list. |

A skill can only ask. What makes skipping visible is the receipt: before you accept work from an agent without a hook, run `evalctl verify --dir <output dir>`.

## The Claude Code Stop hook

`hooks/evalctl_stop_hook.py` runs every time Claude wants to end its turn.

| Situation | Result |
| --- | --- |
| no `.evalctl.yaml` in the session directory or its parents | Claude stops as usual. Unrelated sessions are not affected. |
| `evalctl verify` passes | Claude stops. |
| no valid receipt, `evalctl check` passes | the hook's check wrote the receipt; Claude stops. |
| `evalctl check` fails | Claude is blocked and receives the failure checklist as the reason. |
| still failing at the round limit, or evalctl cannot run | Claude is blocked once more so that it reports this to you, then released. You get a warning that there is no receipt. |

Add the block from `hooks/settings.example.json` to `~/.claude/settings.json` (all projects) or `<project>/.claude/settings.json`, and adjust the path to where this repository lives:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$HOME/evalctl/hooks/evalctl_stop_hook.py\"",
            "timeout": 600
          }
        ]
      }
    ]
  }
}
```

The hook script uses only the standard library and finds `evalctl` on `PATH`, in `~/.local/bin`, or at `$EVALCTL_BIN`. To switch it off for one session, start Claude Code with `EVALCTL_HOOK=off claude`.

Things to know:

- In a gated directory **every** turn is gated, including turns where you only asked a question. Claude gets the failure list and starts fixing. Add `.evalctl.yaml` when the directory is ready to be held to the checks, or use `EVALCTL_HOOK=off`.
- Claude Code itself ends the turn after 8 consecutive blocks by Stop hooks.
- When the hook blocks, Claude Code shows a "Stop hook error" notice. That is how a block is displayed, not a malfunction.
- The hook follows the Stop hook contract in the [hooks reference](https://code.claude.com/docs/en/hooks): input `cwd` and `stop_hook_active` on stdin, output `{"decision": "block", "reason": ...}` on stdout with exit code 0.

## What the receipt does and does not prove

`.eval/receipt.json` records the path, size and SHA-256 of every covered file, the config name, path and hash, the time, the round, and a one-line result per check. It is signed with HMAC-SHA256 using `~/.evalctl/key`.

It catches: skipping the check, editing output after a pass, editing or swapping the config, editing the receipt, copying a receipt from elsewhere, and running evalctl under a different `EVALCTL_HOME`.

It does not stop an agent that deliberately reads `~/.evalctl/key` and forges a signature, or one that edits `.evalctl.yaml` to point to an easier existing config and re-runs the check (the receipt then names that config, and so does `stats`). The agent runs as you, so v0 makes cheating visible and deliberate; it does not make it impossible. If you need more, keep `.evalctl.yaml` in a parent directory the agent does not work in, or run agents under a separate OS user.

A passing check also says nothing about facts the rules do not test. A scraper that invents a plausible URL passes a URL format check.

## Testing

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/python -m pytest
```

73 tests cover every check type, config selection, exit codes, receipts and tampering, rounds, stats, and the hook script driven with Stop-hook JSON. Model-graded checks are tested against a local fake server that speaks the Anthropic and OpenAI wire formats, so the suite runs offline. To see the rubric judged by a real model, run `evalctl check` on `fixtures/forum_scrape/nav_body` and `fixtures/forum_scrape/good` with a judge configured.

`python3 fixtures/forum_scrape/build.py` regenerates the fixtures.
