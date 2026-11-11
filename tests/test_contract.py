"""Acceptance contracts from the task prompt, the in-session repeat, and reconciliation."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from evalctl import contract as ct, reconcile

REPO = Path(__file__).resolve().parents[1]
TASK = ("Write the incident report to report.md. It must have a Summary section, list every affected service, "
        "and state the root cause.")
DELIVERABLES = [{"id": "report_file", "file": "report.md", "text": "The report is written to report.md.",
                 "pass": "report.md exists and holds the report.", "fail": "report.md is missing or is not the report."}]
REQUIREMENTS = [
    {"id": "summary_section", "text": "The report has a Summary section.", "pass": "A section titled Summary exists.", "fail": "No Summary section."},
    {"id": "affected_services", "text": "Every affected service is listed.", "pass": "The services are listed.", "fail": "No list of services."},
    {"id": "root_cause", "text": "The root cause is stated.", "pass": "A root cause is named.", "fail": "No root cause is given."},
]
FULL = "## Summary\nThe cache node ran out of memory.\n\n## Affected services\n- api\n- search\n\n## Root cause\nA leak in the session store.\n"
NO_CAUSE = "## Summary\nThe cache node ran out of memory.\n\n## Affected services\n- api\n- search\n"
KEYWORD = {"summary_section": "## Summary", "affected_services": "## Affected services", "root_cause": "## Root cause",
           "timeline": "## Timeline"}


@pytest.fixture
def model(task_model):
    task_model.deliverables, task_model.requirements = DELIVERABLES, REQUIREMENTS
    task_model.grade = lambda item, outputs: item not in KEYWORD or KEYWORD[item] in outputs
    return task_model


def contract(proj, *extra, task=TASK):
    code, out, err = proj.run("contract", "--task", task, "--dir", str(proj.dir), "--json", *extra)
    assert code == 0, err
    return json.loads(out)


def statuses(data):
    return {c["id"]: c["status"] for c in data["checks"]}


# ---------------------------------------------------------------- a task with no check config at all

def test_contract_covers_each_requirement_and_adds_generic_items(proj, model, home):
    c = contract(proj)["contract"]
    ids = [i["id"] for i in c["items"]]
    assert ids == ["report_file", "summary_section", "affected_services", "root_cause",
                   "g_files_exist", "g_numbers_traceable", "g_nothing_unfinished"]
    assert all(i["pass"] and i["fail"] for i in c["items"])                      # every item says what passes and what fails
    assert [i["kind"] for i in c["items"]] == ["deliverable"] + ["requirement"] * 3 + ["generic"] * 3
    # written by the grader, from the task word for word, stored outside the working directory
    assert TASK in model.prompts[0] and c["model"] == "deepseek:m"
    path = home / "contracts" / f"{c['hash']}.yaml"
    assert path.is_file() and proj.dir not in path.parents and not (proj.dir / ".eval").exists()


def test_missing_requirement_fails_and_is_named_then_passes_with_a_receipt(proj, model):
    """The acceptance case: no check config anywhere, three explicit requirements, one not delivered."""
    contract(proj)
    proj.write("report.md", NO_CAUSE)
    code, data = proj.check()
    s = statuses(data)
    assert code == 1 and s["contract.root_cause"] == "fail"
    assert [k for k, v in s.items() if v == "fail"] == ["contract.root_cause"]     # exactly the one that is missing
    assert s["contract.summary_section"] == s["contract.affected_services"] == s["contract.g_files_exist"] == "pass"
    assert "[FAIL] contract.root_cause (contract): The root cause is stated." in data["text"]
    assert "Required by the task: The root cause is stated." in data["text"]
    assert not (proj.dir / ".eval/receipt.json").exists()

    proj.write("report.md", FULL)
    code, data = proj.check()
    assert code == 0 and all(v == "pass" for v in statuses(data).values())
    assert "acceptance contract: 7 of 7 item(s) passed (no check config for this directory" in data["text"]
    rec = json.loads((proj.dir / ".eval/receipt.json").read_text())
    h = ct.task_hash(TASK)
    assert rec["contracts"] == [{"hash": h, "sha256": ct.file_sha(h), "items": 7}] and rec["config"]["name"] == "contract"
    code, text, _ = proj.run("verify", "--dir", str(proj.dir))
    assert code == 0 and "acceptance contract: 7 item(s), all passed" in text


def test_items_a_script_can_verify_need_no_model_and_block_the_graded_ones(proj, model):
    contract(proj)
    n = len(model.prompts)
    code, data = proj.check()                                   # nothing written yet
    s = statuses(data)
    assert code == 1 and s["contract.g_files_exist"] == "fail" and s["contract.root_cause"] == "skipped"
    assert "report.md: the task asks for this file and it is missing" in data["text"] and len(model.prompts) == n
    proj.write("report.md", FULL + "TODO: confirm the timeline\n")
    code, data = proj.check()
    assert statuses(data)["contract.g_nothing_unfinished"] == "fail" and "report.md:10: unfinished marker" in data["text"]


def test_without_config_and_without_contract_check_says_how_to_get_either(proj):
    code, _, err = proj.run("check", "--dir", str(proj.dir))
    assert code == 2 and "No check config selected" in err and 'evalctl contract --task "<the request, word for word>"' in err


def test_contract_and_configured_checks_stack(proj, model):
    proj.config([{"id": "short", "type": "file", "path": "report.md", "max_size": 5000}], artifacts=["report.md"])
    contract(proj)
    proj.write("report.md", NO_CAUSE)
    code, data = proj.check()
    s = statuses(data)
    assert code == 1 and s["short"] == "pass" and s["contract.root_cause"] == "fail" and data["config"]["name"] == "t"
    proj.write("report.md", FULL)
    assert proj.check()[0] == 0 and proj.verify()[0] == 0
    # a hard check that fails keeps the graded contract items from running, like llm_rubric
    proj.write("report.md", FULL + "x" * 6000)
    assert statuses(proj.check()[1])["contract.root_cause"] == "skipped"


def test_follow_up_request_adds_to_the_contract_and_new_replaces_it(proj, model):
    contract(proj)
    model.deliverables, model.requirements = [], [{"id": "timeline", "text": "A timeline is included.", "pass": "p", "fail": "f"}]
    out = contract(proj, task="Also add a timeline of the incident to the report.")
    assert len(out["active"]) == 2
    proj.write("report.md", FULL)
    code, data = proj.check()
    ids = list(statuses(data))
    assert code == 1 and any(i.endswith(".root_cause") for i in ids) and any(i.endswith(".timeline") for i in ids)
    out = contract(proj, "--new", task="Also add a timeline of the incident to the report.")
    assert len(out["active"]) == 1
    code, text, _ = proj.run("contract", "--clear", "--dir", str(proj.dir))
    assert code == 0 and proj.run("contract", "--dir", str(proj.dir))[0] == 1


def test_the_same_task_reuses_its_contract(proj, model):
    a = contract(proj)
    n = len(model.prompts)
    b = contract(proj)
    assert a["contract"]["hash"] == b["contract"]["hash"] and len(model.prompts) == n and len(b["active"]) == 1


def test_contract_needs_a_model_from_another_family_than_the_worker(proj, model, home, llm):
    (home / "judge.yaml").write_text(f"judges:\n  - {{provider: anthropic, model: claude-haiku-5-5, base_url: '{llm.url}'}}\n")
    code, _, err = proj.run("contract", "--task", TASK, "--dir", str(proj.dir), "--worker", "anthropic")
    assert code == 2 and "same model family as the worker" in err


# ---------------------------------------------------------------- the agent cannot change its own acceptance criteria

def test_edited_contract_is_refused_by_check_and_fails_verify(proj, model, home):
    contract(proj)
    proj.write("report.md", FULL)
    assert proj.check()[0] == 0 and proj.verify()[0] == 0
    path = home / "contracts" / f"{ct.task_hash(TASK)}.yaml"
    path.write_text(path.read_text().replace("The root cause is stated.", "Anything goes."))   # the agent relaxes an item
    code, v = proj.verify()
    assert code == 1 and "was changed after evalctl wrote it" in v["reasons"][0]
    code, _, err = proj.run("check", "--dir", str(proj.dir))
    assert code == 2 and "The agent may not edit its own acceptance criteria" in err


def test_dropping_or_swapping_the_contract_fails_verify(proj, model, home):
    contract(proj)
    proj.write("report.md", FULL)
    assert proj.check()[0] == 0
    state = ct.state_path(proj.dir)
    saved = state.read_text()
    data = json.loads(saved)
    data["contracts"] = []                                               # edit the state by hand
    state.write_text(json.dumps(data))
    code, v = proj.verify()
    assert code == 1 and "changed outside evalctl" in v["reasons"][0]
    state.unlink()                                                       # or remove it altogether
    code, v = proj.verify()
    assert code == 1 and "no longer the active one" in v["reasons"][0]
    state.write_text(saved)
    assert proj.verify()[0] == 0
    model.requirements = [{"id": "easy", "text": "Something is written.", "pass": "p", "fail": "f"}]
    contract(proj, task="Write anything at all into report.md, any content is fine.")   # the agent adds an easier task
    code, v = proj.verify()
    assert code == 1 and any("is active for this task but the receipt does not cover it" in r for r in v["reasons"])
    assert any(i.endswith(".root_cause") for i in statuses(proj.check()[1]))             # and the original items still count


def test_session_started_by_evalctl_cannot_write_its_own_contract(proj, model, monkeypatch):
    monkeypatch.setenv("EVALCTL_CHILD", "1")
    code, text, _ = proj.run("contract", "--task", "an easier task than the real one, for sure", "--dir", str(proj.dir))
    assert code == 0 and "already wrote the acceptance contract" in text and not ct.has_state(proj.dir)


# ---------------------------------------------------------------- written in the background (prompt hook)

def test_background_contract_is_awaited_by_check(proj, model, home):
    code, out, _ = proj.run("contract", "--task", TASK, "--dir", str(proj.dir), "--background", "--json")
    assert json.loads(out)["pending"] is True
    proj.write("report.md", NO_CAUSE)
    code, data = proj.check()                                   # waits for the detached writer
    assert code == 1 and statuses(data)["contract.root_cause"] == "fail"
    assert ct.load_state(proj.dir)["pending"] == []


def test_pending_contract_whose_writer_died_is_written_by_check(proj, model, monkeypatch):
    state, h = ct.start(proj.dir.resolve(), TASK)
    assert state["pending"] == [h]
    monkeypatch.setattr(ct.time, "time", lambda t=time.time: t() + 200)   # the pending mark is stale
    proj.write("report.md", FULL)
    assert proj.check()[0] == 0


# ---------------------------------------------------------------- profiles by trigger word

def test_trigger_word_in_the_task_selects_high_stakes(proj, model, home):
    out = contract(proj, task=TASK + " 这个任务很重要。")
    assert out["profile"] == "high_stakes"
    proj.write("report.md", FULL)
    code, data = proj.check()
    assert data["profile"] == "high_stakes" and code == 1 and statuses(data)["repeat"] == "fail"
    assert "Run `evalctl repeat -k 3` now" in data["text"]
    assert proj.check("--profile", "daily")[0] == 0                       # --profile wins
    # the trigger words are configuration
    assert "triggers:" in (home / "settings.yaml").read_text()
    (home / "settings.yaml").write_text("triggers:\n  high_stakes: [board meeting]\n")
    assert contract(proj, "--new", task=TASK + " 这个任务很重要。")["profile"] is None
    assert contract(proj, "--new", task=TASK + " It is for the board meeting.")["profile"] == "high_stakes"


def test_daily_profile_is_contract_and_check_only(proj, model):
    contract(proj)
    proj.write("report.md", FULL)
    code, data = proj.check()
    assert code == 0 and "repeat" not in statuses(data) and data["ran"]["repeat"] is None


# ---------------------------------------------------------------- repeat in the session, and reconciliation

NUMBERS_TASK = "Write totals.md with the total of amounts.csv. This is important."


@pytest.fixture
def important(proj, model, fake_agent):
    """A high-stakes task: the calling agent wrote total 42; the independent runs will write 57."""
    model.deliverables = [{"id": "totals", "file": "totals.md", "text": "totals.md holds the total.", "pass": "p", "fail": "f"}]
    model.requirements = []
    model.grade = lambda item, outputs: True
    proj.write("amounts.csv", "item,amount\na,20\nb,37\n")
    contract(proj, task=NUMBERS_TASK)
    proj.write("totals.md", "The total is 42.\n")
    fake_agent.script([{"write": {"totals.md": "The total is 57.\n"}}])
    return proj


def repeat(proj, *extra):
    code, out, err = proj.run("repeat", "--dir", str(proj.dir), "--agent", "fake", "--json", *extra)
    assert code == 0, err
    return json.loads(out)


def test_repeat_runs_k_independent_copies_and_returns_differences_not_a_winner(important, fake_agent, model):
    proj = important
    r = repeat(proj)
    assert r["k"] == 3 and [x["status"] for x in r["runs"]] == ["pass"] * 3      # k from the high_stakes profile
    calls = fake_agent.calls()
    cwds = {c["cwd"] for c in calls}
    assert len(calls) == 3 and len(cwds) == 3 and str(proj.dir.resolve()) not in cwds
    for c in calls:
        assert c["env"]["EVALCTL_CHILD"] == "1"                                  # each run knows it is a repeat
        assert NUMBERS_TASK in c["prompt"] and str(proj.dir.resolve()) not in c["prompt"]   # and is not shown the caller's directory
    [d] = r["divergences"]
    assert d["id"] == "D1" and d["kind"] == "number" and d["where"] == "totals.md"
    assert d["main"] == "only in yours: 42 (line 1)"
    assert d["others"] == {f"run {i}": "has 57, which yours does not; does not have your 42" for i in (1, 2, 3)}
    # every run agrees with each other and disagrees with the caller, and still nobody is declared right
    assert "These are not verdicts: the other runs can be the wrong ones." in r["text"]
    assert "Do not choose a" in r["text"] and "value because more runs have it" in r["text"]
    assert "contract.totals" in r["consistent"] and "## D1" in r["text"] and "needs_human:" in r["text"]
    # the copies started from the task's starting files, without the caller's output
    assert (proj.dir / ".eval/repeat/1/output/totals.md").read_text() == "The total is 57.\n"
    assert (proj.dir / "totals.md").read_text() == "The total is 42.\n"          # nothing was merged
    assert (proj.dir / ".eval/repeat/report.md").read_text().startswith("evalctl repeat: 3 independent run(s)")


def test_differences_must_be_reconciled_with_evidence_before_the_receipt(important):
    proj = important
    repeat(proj)
    code, data = proj.check()
    assert code == 1 and statuses(data)["reconcile"] == "fail" and ".eval/reconcile.md does not exist" in data["text"]
    proj.write(".eval/reconcile.md", "## D1\nadopted: 57\n")
    code, data = proj.check()
    assert code == 1 and "D1: no `evidence:` line" in data["text"]
    proj.write(".eval/reconcile.md", "## D1\nadopted: 57\nevidence: the other runs all say 57\n")
    code, data = proj.check()
    assert code == 1 and "the evidence names no file that exists in the task's files" in data["text"]
    proj.write("totals.md", "The total is 57.\n")
    proj.write(".eval/reconcile.md", "## D1\nadopted: 57\nevidence: amounts.csv rows 2-3, 20 + 37\n")
    code, data = proj.check()
    assert code == 0 and statuses(data)["reconcile"] == "pass"
    rec = json.loads((proj.dir / ".eval/receipt.json").read_text())
    assert (rec["repeated"], rec["reconciled"], rec["divergences"], rec["needs_human"]) == (3, True, 1, 0)
    assert rec["profile"] == "high_stakes"
    code, text, _ = proj.run("verify", "--dir", str(proj.dir))
    assert code == 0 and "repeated: 3   reconciled: true   differences: 1   left for you: 0" in text


def test_what_the_inputs_do_not_settle_is_left_for_the_user_not_picked(important):
    proj = important
    repeat(proj)
    proj.write(".eval/reconcile.md", "## D1\nneeds_human: amounts.csv does not say whether row b is a refund\n")
    code, data = proj.check()
    assert code == 0 and "1 difference(s) between the repeat runs are left for the user (D1)" in data["text"]
    rec = json.loads((proj.dir / ".eval/receipt.json").read_text())
    assert (rec["repeated"], rec["reconciled"], rec["needs_human"]) == (3, False, 1)
    assert proj.verify()[0] == 0
    proj.write(".eval/reconcile.md", "## D1\nneeds_human:\n")
    assert proj.check()[0] == 1


def test_repeat_without_differences_goes_straight_to_the_receipt(important, fake_agent):
    proj = important
    proj.write("totals.md", "The total is 57.\n")
    r = repeat(proj, "-k", "2")
    assert r["divergences"] == [] and "No differences found" in r["text"] and len(fake_agent.calls()) == 2
    code, data = proj.check("--profile", "daily")
    rec = json.loads((proj.dir / ".eval/receipt.json").read_text())
    assert code == 0 and (rec["repeated"], rec["reconciled"], rec["divergences"]) == (2, True, 0)
    # under high_stakes (k=3) two runs are not enough
    code, v = proj.verify()
    assert code == 1 and "requires repeat (k=3" in v["reasons"][0] and "only k=2" in v["reasons"][0]


def test_repeat_reports_checks_that_failed_elsewhere_and_differences_only_a_reader_sees(important, fake_agent, model):
    proj = important
    proj.write("totals.md", "The total is 57.\n")
    model.grade = lambda item, outputs: "57" in outputs
    fake_agent.script([{"write": {"totals.md": "The total is unknown.\n"}}])
    model.differences = [{"item": "totals", "what": "whether a total is given", "main": "totals.md line 1: 57",
                          "others": {"run 1": "says the total is unknown"}}]
    r = repeat(proj, "-k", "1")
    assert r["runs"][0]["status"] == "fail" and "contract.totals" in r["failed_elsewhere"]
    kinds = [d["kind"] for d in r["divergences"]]
    assert kinds == ["number", "conclusion"] and r["divergences"][1]["item"] == "totals"
    assert "contract.totals" not in r["consistent"]
    assert "Checks that failed in other runs" in r["text"]
    compare = next(p for p in model.prompts if "Attempt MAIN" in p)
    assert "The total is 57." in compare and "The total is unknown." in compare and "Do not say which one is right" not in compare


def test_child_run_does_not_start_repeats_of_its_own(important, fake_agent, monkeypatch):
    proj = important
    monkeypatch.setenv("EVALCTL_CHILD", "1")
    code, text, _ = proj.run("repeat", "--dir", str(proj.dir), "--agent", "fake")
    assert code == 0 and "skipped" in text and "does not start repeats of its own" in text
    assert fake_agent.calls() == []
    # and its check neither asks for a repeat nor waits for one
    code, data = proj.check()
    assert code == 0 and "repeat" not in statuses(data)


def test_a_new_request_voids_the_earlier_repeat(important, model):
    proj = important
    proj.write("totals.md", "The total is 57.\n")
    repeat(proj, "-k", "3")
    assert proj.check()[0] == 0
    contract(proj, task="Also state the number of rows in totals.md. Important.")
    code, data = proj.check()
    assert code == 1 and statuses(data)["repeat"] == "fail"


def test_repeat_needs_the_starting_files(proj, model, fake_agent):
    fake_agent.script([{}])
    code, _, err = proj.run("repeat", "--dir", str(proj.dir), "--agent", "fake")
    assert code == 2 and "No check config selected" in err
    proj.config([{"id": "f", "type": "file", "path": "x.md"}])
    code, _, err = proj.run("repeat", "--dir", str(proj.dir), "--agent", "fake")
    assert code == 2 and "no record of the files the task started from" in err


def test_reconcile_file_format():
    text = "# Reconciliation\n\n## D1: totals\n- **adopted**: 57\n- evidence: amounts.csv rows 2-3\n\n### D2\nneeds_human： 输入里没有说明\n\n## D3\nadopted: x\n"
    assert reconcile.parse(text) == {"D1": {"adopted": "57", "evidence": "amounts.csv rows 2-3"},
                                     "D2": {"needs_human": "输入里没有说明"}, "D3": {"adopted": "x"}}


# ---------------------------------------------------------------- evalctl run passes the task in

def test_run_writes_the_contract_from_its_task_before_calling_the_agent(proj, model, fake_agent):
    fake_agent.script([{"write": {"report.md": NO_CAUSE}}, {"write": {"report.md": FULL}}])
    code, out, err = proj.run("run", "--agent", "fake", "--dir", str(proj.dir), "--json", TASK)
    data = json.loads(out)
    assert code == 0, err
    assert [r["failed_checks"] for r in data["rounds"]] == [["contract.root_cause"], []]
    assert "contract.root_cause" in fake_agent.calls()[1]["prompt"]          # the missing item went back to the agent
    assert json.loads((proj.dir / ".eval/receipt.json").read_text())["contracts"][0]["items"] == 7
    assert ct.load_state(proj.dir)["source"] == "run"


# ---------------------------------------------------------------- hooks

def hook(script, payload, **env):
    full = {**os.environ, "EVALCTL_BIN": str(Path(sys.executable).parent / "evalctl"), **env}
    p = subprocess.run([sys.executable, str(REPO / "hooks" / script)], input=json.dumps(payload).encode(),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=full, timeout=120)
    assert p.returncode == 0, p.stderr.decode()
    return json.loads(p.stdout.decode()) if p.stdout.strip() else None


def prompt_hook(cwd, prompt, session="s1", **env):
    return hook("evalctl_prompt_hook.py", {"session_id": session, "cwd": str(cwd), "hook_event_name": "UserPromptSubmit",
                                           "prompt": prompt, "transcript_path": "/tmp/t.jsonl"}, **env)


def stop_hook(cwd, session="s1", active=False):
    return hook("evalctl_stop_hook.py", {"session_id": session, "cwd": str(cwd), "hook_event_name": "Stop",
                                         "stop_hook_active": active, "transcript_path": "/no/such/file"})


def test_prompt_hook_leaves_ordinary_sessions_alone(proj, model):
    assert prompt_hook(proj.dir, "Please refactor the parser module and add tests for the edge cases.") is None
    assert not ct.has_state(proj.dir) and model.prompts == []
    assert stop_hook(proj.dir) is None


def test_prompt_hook_gates_on_a_trigger_word_and_the_stop_hook_enforces_it(proj, model):
    out = prompt_hook(proj.dir, TASK + " 这个任务很重要。")
    ctx = out["hookSpecificOutput"]
    assert ctx["hookEventName"] == "UserPromptSubmit"
    text = ctx["additionalContext"]
    assert "quality-gated" in text and "(profile: high_stakes)" in text and "You do not need to run `evalctl contract`" in text
    assert "requires 3 independent repeats" in text and "run `evalctl check`" in text
    state = ct.load_state(proj.dir)
    assert state["source"] == "hook" and state["session"] == "s1" and state["profile"] == "high_stakes"
    # the agent tries to stop with nothing written: blocked, with the contract's failure list
    out = stop_hook(proj.dir)
    assert out["decision"] == "block" and "contract.g_files_exist" in out["reason"]
    proj.write("report.md", FULL)
    out = stop_hook(proj.dir)                                   # everything passes, but high_stakes wants the repeat
    assert out["decision"] == "block" and "Run `evalctl repeat -k 3` now" in out["reason"]
    assert stop_hook(proj.dir, session="another-session") is None          # someone else's session in the same directory


def test_prompt_hook_in_a_gated_directory_short_follow_ups_and_new_sessions(proj, model):
    proj.config([{"id": "f", "type": "file", "path": "report.md"}])
    out = prompt_hook(proj.dir, TASK)
    assert "(profile:" not in out["hookSpecificOutput"]["additionalContext"] and "independent repeats" not in out["hookSpecificOutput"]["additionalContext"]
    out = prompt_hook(proj.dir, "yes, go on")                    # too short to be a task: gated, no new contract
    assert "You do not need to run" not in out["hookSpecificOutput"]["additionalContext"]
    proj.write("report.md", NO_CAUSE)
    code, data = proj.check()
    assert code == 1 and [k for k, v in statuses(data).items() if v == "fail"] == ["contract.root_cause"]
    assert len(ct.load_state(proj.dir)["contracts"]) == 1
    model.deliverables, model.requirements = [], [{"id": "anything", "text": "Something exists.", "pass": "p", "fail": "f"}]
    prompt_hook(proj.dir, "Now a completely different task in a brand new session, please.", session="s2")
    state = ct.wait_pending(proj.dir.resolve(), None)
    assert state["session"] == "s2" and len(state["contracts"]) == 1 and state["contracts"][0] != ct.task_hash(TASK)


def test_prompt_hook_is_silent_in_runs_started_by_evalctl(proj, model):
    for var in ("EVALCTL_CHILD", "EVALCTL_OUTER"):
        assert prompt_hook(proj.dir, TASK + " important", **{var: "1"}) is None
    assert prompt_hook(proj.dir, TASK + " important", EVALCTL_HOOK="off") is None
    assert not ct.has_state(proj.dir)


def test_stop_hook_is_gated_by_a_contract_the_agent_wrote_through_the_skill(proj, model):
    contract(proj)
    out = stop_hook(proj.dir, session="whatever")
    assert out["decision"] == "block" and "contract.g_files_exist" in out["reason"]
    proj.write("report.md", FULL)
    assert stop_hook(proj.dir) is None and (proj.dir / ".eval/receipt.json").is_file()


def test_delivered_but_uncheckable_items_pass_and_are_reported_as_unverified(proj, model, llm):
    """With no source material in the directory a grader cannot confirm content; that is flagged, not failed."""
    contract(proj)
    proj.write("report.md", FULL)
    inner = llm.reply

    def reply(prompt):
        if "Checklist items:" not in prompt:
            return inner(prompt)
        data = json.loads(inner(prompt))
        for v in data["verdicts"]:
            v["unverified"] = v["id"] == "root_cause"
        return json.dumps(data)
    llm.reply = reply
    code, data = proj.check()
    assert code == 0
    rc = next(c for c in data["checks"] if c["id"] == "contract.root_cause")
    assert rc["status"] == "pass" and rc["summary"].startswith("delivered, not cross-checked against any source")
    assert "1 contract item(s) were delivered but could not be cross-checked against any source file (contract.root_cause)" in data["text"]
    assert 'set "unverified" to true' in llm.requests[-1]["body"]["messages"][0]["content"]


def test_numbers_are_never_shown_in_scientific_notation():
    from decimal import Decimal
    from evalctl.sessionrepeat import plain
    assert [plain(Decimal(x)) for x in ("120.00", "1448.240", "10", "0.50", "1E+2")] == ["120", "1448.24", "10", "0.5", "100"]
