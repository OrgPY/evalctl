"""The loop switches in .evalctl.yaml: profiles, judge_repeat, repeat, and what verify asks of a receipt."""
from __future__ import annotations

import json

import pytest

from evalctl import loop as lp, store
from evalctl.config import ConfigError

SPEC_EXAMPLE = {
    "config": "forum_scrape", "profile": "daily",
    "loop": {"max_rounds": 3,
             "lint": {"when": "config_changed", "block_on": "critical"},
             "judge_repeat": {"n": 3, "agree": 2, "when": "final_round", "unstable": "exclude"},
             "repeat": {"k": 3, "when": "never", "compare": ["numbers", "check_results"], "on_disagree": "escalate"}},
    "profiles": {"high_stakes": {"judge_repeat": {"when": "always"}, "repeat": {"when": "on_pass"}},
                 "calibrate": {"judge_repeat": {"n": 4, "agree": 4, "when": "always"}}},
}
RUBRIC = {"id": "quality", "type": "llm_rubric", "file": "report.md",
          "rubric": [{"id": "clear", "text": "States a finding. It is NOT clear when no finding is stated."},
                     {"id": "nice", "text": "The report is good."}]}
HARD = {"id": "has_report", "type": "file", "path": "report.md"}


# ---------------------------------------------------------------- profiles

def test_profiles_override_single_keys(monkeypatch):
    daily = lp.resolve(SPEC_EXAMPLE)
    assert daily.profile == "daily" and daily.max_rounds == 3
    assert daily.judge_repeat == {"n": 3, "agree": 2, "when": "final_round", "unstable": "exclude"}
    assert not daily.repeat_on and daily.lint_on

    high = lp.resolve(SPEC_EXAMPLE, "high_stakes")
    assert high.judge_repeat["when"] == "always" and high.judge_repeat["n"] == 3
    assert high.repeat_on and high.repeat["k"] == 3

    monkeypatch.setenv("EVALCTL_PROFILE", "calibrate")
    cal = lp.resolve(SPEC_EXAMPLE)
    assert cal.profile == "calibrate" and (cal.judge_repeat["n"], cal.judge_repeat["agree"]) == (4, 4)
    assert lp.resolve(SPEC_EXAMPLE, "high_stakes").profile == "high_stakes"  # --profile wins over the environment


def test_without_a_loop_section_every_switch_is_off():
    off = lp.resolve({"config": "x"})
    assert not off.lint_on and not off.judge_repeat_on and not off.repeat_on and off.max_rounds is None


@pytest.mark.parametrize("data, problem", [
    ({"loop": {"lint": {"when": "sometimes"}}}, "`when` must be one of never | config_changed | always"),
    ({"loop": {"judge_repeat": {"n": 3, "agree": 4}}}, "cannot exceed"),
    ({"loop": {"judge_repeat": {"n": 4, "agree": 2}}}, "more than half"),
    ({"loop": {"repeat": {"compare": ["vibes"]}}}, "`compare` must be one of"),
    ({"loop": {"repeats": {}}}, "unknown key(s): repeats"),
    ({"loop": {"max_rounds": 0}}, "positive integer"),
])
def test_bad_loop_settings_are_rejected(data, problem):
    with pytest.raises(ConfigError) as e:
        lp.resolve(data)
    assert problem in str(e.value)


def test_unknown_profile_is_an_error_not_a_silent_default(proj):
    proj.config([HARD])
    proj.pointer(**{k: v for k, v in SPEC_EXAMPLE.items() if k != "config"})
    proj.write("report.md", "x")
    code, _, err = proj.run("check", "--dir", str(proj.dir), "--profile", "hihg_stakes")
    assert code == 2 and "profile 'hihg_stakes' is not defined" in err and "calibrate, high_stakes" in err


def test_loop_max_rounds_overrides_the_config(proj):
    proj.config([{"id": "no_todo", "type": "contains", "file": "report.md", "must_not": ["TODO"]}], max_rounds=3)
    proj.pointer(loop={"max_rounds": 5})
    proj.write("report.md", "TODO")
    assert proj.check()[1]["max_rounds"] == 5


# ---------------------------------------------------------------- judge_repeat

def votes(passes_by_criterion):
    """A grader whose n-th grading of a criterion passes iff n is in the given set."""
    seen = {}

    def verdict(item, cid, _):
        seen[cid] = seen.get(cid, 0) + 1
        return seen[cid] in passes_by_criterion[cid]
    return verdict


def test_vague_rubric_item_is_unstable_and_does_not_affect_the_verdict(proj, grader):
    """`nice` ("The report is good.") gets 2 pass, 2 fail out of 4: no verdict. `clear` is unanimous."""
    proj.config([HARD, RUBRIC])
    proj.pointer(loop={"judge_repeat": {"n": 4, "agree": 3, "when": "always", "unstable": "exclude"}})
    proj.write("report.md", "We found that the cache is too small.")
    grader.verdict = votes({"clear": {1, 2, 3, 4}, "nice": {1, 3}})
    code, data = proj.check()
    assert code == 0 and data["status"] == "pass"            # the unstable item does not block
    q = next(c for c in data["checks"] if c["id"] == "quality")
    assert q["meta"]["votes"] == {"clear": {"pass": 4, "n": 4, "verdict": "pass"},
                                  "nice": {"pass": 2, "n": 4, "verdict": "unstable"}}
    assert data["unstable"] == ["quality.nice"]
    # not in the agent's failure list: the text names no failed item, only a one-line note
    assert "[FAIL]" not in data["text"] and "nice" not in data["text"].replace("quality", "")
    assert "1 rubric item(s) had no stable verdict" in data["text"]
    # but it is in the run log, the receipt and the stats
    assert store.runs_for("t")[0]["extra"]["unstable"] == ["quality.nice"]
    rec = json.loads((proj.dir / ".eval/receipt.json").read_text())
    assert rec["ran"]["judge_repeat"] == {"ran": True, "n": 4, "agree": 3, "mode": "exclude", "unstable": ["quality.nice"]}
    cons = store.stats()["configs"]["t"]["rubric_consistency"]
    assert cons["quality.clear"] == {"runs": 1, "consistency": 1.0, "unstable_runs": 0}
    assert cons["quality.nice"] == {"runs": 1, "consistency": 0.5, "unstable_runs": 1}
    _, out, _ = proj.run("stats")
    assert "50%  quality.nice  (1 run(s), unstable in 1)" in out


def test_unstable_fail_mode_counts_it_as_failed(proj, grader):
    proj.config([HARD, RUBRIC])
    proj.pointer(loop={"judge_repeat": {"n": 4, "agree": 3, "when": "always", "unstable": "fail"}})
    proj.write("report.md", "We found that the cache is too small.")
    grader.verdict = votes({"clear": {1, 2, 3, 4}, "nice": {1, 3}})
    code, data = proj.check()
    assert code == 1 and "no stable verdict, 2 of 4 gradings passed and 3 must agree" in data["text"]


def test_agreed_failure_is_reported_with_the_vote(proj, grader):
    proj.config([HARD, RUBRIC])
    proj.pointer(loop={"judge_repeat": {"n": 3, "agree": 2, "when": "always"}})
    proj.write("report.md", "words")
    grader.verdict = votes({"clear": {2}, "nice": {1, 2, 3}})
    code, data = proj.check()
    assert code == 1 and "criterion 'clear'" in data["text"] and "(failed in 2 of 3 gradings)" in data["text"]


def test_final_round_repeats_only_when_the_result_would_be_final(proj, grader):
    proj.config([HARD, RUBRIC], max_rounds=2)
    proj.pointer(loop={"judge_repeat": {"n": 3, "agree": 2, "when": "final_round"}})
    proj.write("report.md", "draft one")
    grader.verdict = lambda item, cid, n: "finding" in item
    code, data = proj.check()                      # round 1 fails on the first grading: no repeat
    assert code == 1 and grader.gradings == 1 and data["ran"]["judge_repeat"] is None
    proj.write("report.md", "draft two")
    grader.gradings = 0
    code, data = proj.check()                      # round 2 of 2 is the last one: repeated although failing
    assert code == 1 and grader.gradings == 3 and data["ran"]["judge_repeat"]["n"] == 3
    proj.write("report.md", "a finding")
    grader.gradings = 0
    proj.config([HARD, RUBRIC], max_rounds=9, pointer=False)  # changed config: a new task
    code, data = proj.check()                      # passes the first grading: confirmed by two more
    assert code == 0 and grader.gradings == 3


def test_calibrate_only_does_not_repeat_until_a_profile_turns_it_on(proj, grader):
    proj.config([HARD, RUBRIC])
    proj.pointer(loop={"judge_repeat": {"n": 3, "agree": 2, "when": "calibrate_only"}},
                 profiles={"calibrate": {"judge_repeat": {"n": 4, "agree": 4, "when": "always"}}})
    proj.write("report.md", "a finding")
    assert proj.check()[0] == 0 and grader.gradings == 1
    grader.gradings = 0
    code, data = proj.check("--profile", "calibrate")
    assert grader.gradings == 4 and data["profile"] == "calibrate"


# ---------------------------------------------------------------- receipts and verify

def test_receipt_records_profile_and_switches(proj):
    proj.config([HARD])
    proj.pointer(profile="daily", loop={"max_rounds": 3, "repeat": {"k": 2, "when": "never"}})
    proj.write("report.md", "x")
    assert proj.check()[0] == 0
    rec = json.loads((proj.dir / ".eval/receipt.json").read_text())
    assert rec["profile"] == "daily" and rec["switches"]["repeat"]["k"] == 2
    assert rec["switches"]["judge_repeat"]["n"] == 1 and rec["ran"]["repeat"] is None
    assert proj.verify()[0] == 0


def test_verify_fails_when_the_config_requires_repeat_and_the_receipt_has_none(proj):
    proj.config([HARD])
    proj.pointer(loop={"repeat": {"k": 2, "when": "never"}}, profiles={"high_stakes": {"repeat": {"when": "on_pass"}}})
    proj.write("report.md", "x")
    code, data = proj.check("--profile", "high_stakes")
    # skill mode: repeat is ignored, and the output says so
    assert code == 0 and any("Repeat only runs under `evalctl run`" in n for n in data["notes"])
    assert "it was ignored here" in data["text"]
    assert proj.verify()[0] == 0                                   # the default profile asks for no repeat
    code, v = proj.verify("--profile", "high_stakes")
    assert code == 1 and "requires repeat (k=2, when: on_pass), but the receipt records no repeat run" in v["reasons"][0]
    # the same when the requirement is in the base loop section
    proj.pointer(loop={"repeat": {"k": 2, "when": "on_pass"}})
    assert proj.verify()[0] == 1


def test_verify_fails_when_judge_repeat_is_required_but_was_not_run(proj, grader):
    proj.config([HARD, RUBRIC])
    proj.write("report.md", "a finding")
    assert proj.check()[0] == 0 and proj.verify()[0] == 0          # written without any repetition
    proj.pointer(loop={"judge_repeat": {"n": 3, "agree": 2, "when": "always"}})
    code, v = proj.verify()
    assert code == 1 and "requires judge_repeat (n=3, agree=2)" in v["reasons"][0]
    assert proj.check()[0] == 0 and proj.verify()[0] == 0          # re-checked under the new settings


def test_switch_records_are_covered_by_the_signature(proj):
    proj.config([HARD])
    proj.pointer(loop={"repeat": {"k": 2, "when": "on_pass"}})
    proj.write("report.md", "x")
    proj.check()
    rp = proj.dir / ".eval/receipt.json"
    rec = json.loads(rp.read_text())
    rec["ran"]["repeat"] = {"ran": True, "k": 2, "agreed": True}     # the agent claims the repeat happened
    rp.write_text(json.dumps(rec))
    code, v = proj.verify()
    assert code == 1 and "signature does not match" in v["reasons"][0]


# ---------------------------------------------------------------- repeat (evalctl run)

CHECKS = [{"id": "has_report", "type": "file", "path": "report.md", "min_size": 1},
          {"id": "no_todo", "type": "contains", "file": "report.md", "must_not": ["TODO"]}]


def run(proj, *extra):
    code, out, _ = proj.run("run", "--agent", "fake", "--dir", str(proj.dir), "--json", "Write the report", *extra)
    return code, json.loads(out)


def test_repeat_that_agrees_writes_a_receipt_that_verifies(proj, fake_agent):
    proj.config(CHECKS)
    proj.pointer(loop={"repeat": {"k": 2, "when": "on_pass"}})
    proj.write("brief.md", "the brief")
    same = [{"write": {"report.md": "total 42 items\n"}}]
    fake_agent.script(same, same, same)
    code, data = run(proj)
    assert code == 0 and data["status"] == "pass"
    assert data["repeat"]["agreed"] and data["repeat"]["k"] == 2 and data["repeat"]["passes"] == 2
    assert proj.verify()[0] == 0
    rec = json.loads((proj.dir / ".eval/receipt.json").read_text())
    assert rec["ran"]["repeat"]["agreed"] is True
    # the repeats ran in copies of the starting files, never in the directory itself
    cwds = [c["cwd"] for c in fake_agent.calls()]
    assert len(set(cwds)) == 3 and cwds[0] == str(proj.dir.resolve())
    assert (proj.dir / ".eval/repeat/2/output/report.md").read_text() == "total 42 items\n"
    # repeat copies are not tasks of their own in the log
    assert store.stats()["configs"]["t"]["tasks"] == 1


def test_repeat_with_different_numbers_needs_human_judgement(proj, fake_agent):
    proj.config(CHECKS)
    proj.pointer(loop={"repeat": {"k": 2, "when": "on_pass"}})
    fake_agent.script([{"write": {"report.md": "total 42 items\n"}}], [{"write": {"report.md": "total 42 items\n"}}],
                      [{"write": {"report.md": "total 57 items\n"}}])
    code, data = run(proj)
    assert code == 1 and data["status"] == "needs_human"
    assert "NEEDS HUMAN JUDGEMENT" in data["reason"]
    assert data["repeat"]["differences"] == ["repeat 2: numbers in report.md differ; only in the first run: 42; only in the repeat: 57"]
    assert not (proj.dir / ".eval/receipt.json").exists()
    assert proj.verify()[0] == 1
    s = store.stats()["configs"]["t"]
    assert s["tasks_needing_human_judgement"] == 1 and s["compliance_rate"] == 0
    code, text, _ = proj.run("run", "--agent", "fake", "--dir", str(proj.dir), "Write the report")
    assert "evalctl run: NEEDS HUMAN JUDGEMENT" in text and "differences between the runs:" in text


def test_repeat_that_fails_its_checks_is_a_disagreement(proj, fake_agent):
    proj.config(CHECKS, max_rounds=1)
    proj.pointer(loop={"repeat": {"k": 1, "when": "on_pass", "on_disagree": "fail"}})
    fake_agent.script([{"write": {"report.md": "fine\n"}}], [{"write": {"report.md": "TODO\n"}}])
    code, data = run(proj)
    assert code == 1 and data["status"] == "fail"
    assert data["repeat"]["differences"][0].startswith("repeat 1: check no_todo is FAIL (first run: PASS)")


def test_repeat_can_compare_check_results_only(proj, fake_agent):
    proj.config(CHECKS)
    proj.pointer(loop={"repeat": {"k": 1, "when": "on_pass", "compare": ["check_results"]}})
    fake_agent.script([{"write": {"report.md": "total 42\n"}}], [{"write": {"report.md": "total 57\n"}}])
    assert run(proj)[0] == 0


def test_bench_uses_the_suites_repeat_k_and_does_not_nest_repeats(proj, fake_agent, tmp_path):
    import yaml
    proj.config(CHECKS, name="report", pointer=False)
    suite = tmp_path / "s.yaml"
    suite.write_text(yaml.safe_dump({"config": "report", "loop": {"repeat": {"k": 3, "when": "on_pass"}},
                                     "tasks": [{"id": "a", "prompt": "Write"}]}))
    fake_agent.script([{"write": {"report.md": "ok"}}], [{"write": {"report.md": "TODO"}}], [{"write": {"report.md": "ok"}}])
    code, out, _ = proj.run("bench", str(suite), "--agent", "fake", "--json")
    r = json.loads(out)
    assert r["repeat"] == 3 and r["tasks"]["a"]["passes"] == 2 and r["summary"]["pass_k"] == 0
    assert r["summary"]["checks"]["no_todo"]["passed"] == 2
    assert len({c["cwd"] for c in fake_agent.calls()}) == 3
