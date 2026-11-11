"""`evalctl judge`: pairwise human judgement, the rubric candidate made from the reason, agreement with llm_rubric."""
from __future__ import annotations

import json

import pytest
import yaml

from evalctl import learn as lrn, pairwise

GOOD = "## Summary\n\nThe outage was caused by a full disk on db-2. We added an alert at 80% usage.\n"
BAD = "## Summary\n\nThere was an outage. It has been looked into and things are fine now.\n"
CHECKS = [{"id": "has_summary", "type": "contains", "file": "report.md", "must": ["## Summary"]},
          {"id": "quality", "type": "llm_rubric", "file": "report.md",
           "rubric": [{"id": "polite", "text": "The tone is professional. It is NOT professional when it is rude."}]}]
CRITERION = {"id": "names_the_cause", "text": "The report names the specific cause of the incident. It FAILS when the cause is vague or missing."}


@pytest.fixture
def pair(proj, tmp_path):
    a, b = tmp_path / "out_a", tmp_path / "out_b"
    for d, text in ((a, GOOD), (b, BAD)):
        d.mkdir()
        (d / "report.md").write_text(text)
    proj.config(CHECKS, name="incident", pointer=False, artifacts=["report.md"])
    return a, b


def model(grader, criterion=CRITERION, passes=lambda item, cid: True):
    grader.reply = lambda prompt: json.dumps(criterion)
    grader.verdict = lambda item, cid, n: passes(item, cid)


def judge(proj, a, b, pick="a", reason="A names the actual cause, B is vague", *extra):
    code, out, err = proj.run("judge", str(a), str(b), "--config", "incident", "--pick", pick, "--reason", reason, "--json", *extra)
    assert code == 0, err
    return json.loads(out)


def test_reason_becomes_a_validated_rubric_candidate(proj, grader, pair):
    a, b = pair
    # the new criterion passes on the preferred output and fails on the other; the existing rubric sees no difference
    model(grader, passes=lambda item, cid: cid == "polite" or "full disk" in item)
    j = judge(proj, a, b)
    c = j["candidate"]
    assert c["id"] == "judged_names_the_cause" and c["kind"] == "rubric" and c["scope"] == "general"
    assert c["enabled"] is True and c["validation"] == {"final": "pass", "draft": "fail"}
    assert c["check"]["type"] == "llm_rubric" and c["check"]["file"] == "report.md" and c["check"]["rubric"] == [CRITERION]
    assert "you preferred" in c["basis"] and "A names the actual cause" in c["basis"]
    # the model was asked to restate the reason for a single output, with a fail side
    ask = next(r["body"]["messages"][-1]["content"] for r in grader.requests if "<reason>" in r["body"]["messages"][-1]["content"])
    assert "A names the actual cause, B is vague" in ask and "State what passes AND what fails" in ask
    # it is a learned candidate, not an active check: review decides
    doc = lrn.load("incident")
    assert [x["id"] for x in doc["candidates"]] == ["judged_names_the_cause"]
    assert "names_the_cause" not in (proj.home / "checks" / "incident.yaml").read_text()
    code, text, _ = proj.run("review", "incident", "--yes", "--no-lint")
    assert code == 0
    written = yaml.safe_load((proj.home / "checks" / "incident.yaml").read_text())
    assert [c["id"] for c in written["checks"]] == ["has_summary", "quality", "judged_names_the_cause"]


def test_criterion_that_does_not_separate_the_pair_is_discarded(proj, grader, pair):
    a, b = pair
    model(grader, passes=lambda item, cid: True)                      # the other output passes it too
    j = judge(proj, a, b)
    assert j["candidate"] is None and "the other output passes it too" in j["candidate_note"]
    doc = lrn.load("incident")
    assert doc["candidates"] == [] and "does not capture the reason" in doc["discarded"][0]["reason"]
    model(grader, passes=lambda item, cid: cid == "polite")           # the preferred output fails it
    assert "the output you preferred fails it" in judge(proj, a, b)["candidate_note"]


def test_agreement_between_llm_rubric_and_the_human_is_counted_per_config(proj, grader, pair):
    a, b = pair
    # the existing rubric ("polite") prefers whichever output mentions the disk; the human's choices vary
    model(grader, passes=lambda item, cid: "full disk" in item)
    j1 = judge(proj, a, b, "a")
    assert j1["llm"] == {"a": 1.0, "b": 0.0, "checks": {"quality": {"a": 1.0, "b": 0.0}}, "winner": "a"} and j1["agrees"] is True
    j2 = judge(proj, a, b, "b", "B is shorter", "--no-candidate")
    assert j2["agrees"] is False
    model(grader, passes=lambda item, cid: True)                      # now the rubric cannot tell them apart
    j3 = judge(proj, b, a, "a", "less jargon", "--no-candidate")
    assert j3["llm"]["winner"] == "tie" and j3["agrees"] is False
    s = pairwise.summary()["incident"]
    assert (s["judgements"], s["graded"], s["agree"], s["disagree"], s["tie"]) == (3, 3, 1, 1, 1)
    assert s["agreement_rate"] == pytest.approx(1 / 3)
    code, text, _ = proj.run("judge", "--stats")
    assert "llm_rubric vs you: agreement 33%  (1 agree, 1 disagree, 1 tie, of 3 graded pairs)" in text
    assert "you chose B, the rubric chose A: B is shorter" in text and "the rubric saw no difference: less jargon" in text
    code, out, _ = proj.run("stats", "--json")
    assert json.loads(out)["judgements"]["incident"]["agreement_rate"] == pytest.approx(1 / 3)


def test_stats_shows_agreement_next_to_the_runs_of_the_config(proj, grader, pair):
    a, b = pair
    model(grader, passes=lambda item, cid: "full disk" in item)
    judge(proj, a, b, "a")
    (a / ".evalctl.yaml").write_text("config: incident\n")
    proj.run("check", "--dir", str(a))
    code, text, _ = proj.run("stats")
    assert "llm_rubric vs your pairwise judgements: agreement 100%  (1 agree, 0 disagree, 0 tie, of 1 pairs)" in text


def test_without_a_model_the_reason_is_kept_as_an_unvalidated_disabled_candidate(proj, pair):
    a, b = pair
    j = judge(proj, a, b)
    c = j["candidate"]
    assert c["enabled"] is False and "was not validated" in c["basis"]
    assert "A names the actual cause, B is vague" in c["check"]["rubric"][0]["text"] and "FAILS" in c["check"]["rubric"][0]["text"]
    assert j["llm"] is None and j["agrees"] is None
    code, text, _ = proj.run("judge", "--stats")
    assert "not measured" in text


def test_interactive_judgement_shows_both_outputs_and_asks(proj, grader, pair, monkeypatch):
    a, b = pair
    model(grader, passes=lambda item, cid: "full disk" in item)
    answers = iter(["x", "a", "", "A states the cause"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    code, text, _ = proj.run("judge", str(a), str(b), "--config", "incident")
    assert code == 0
    assert f"A: {a.resolve()}" in text and "--- report.md in A (3 lines)" in text and "full disk on db-2" in text
    assert "--- report.md in B (3 lines)" in text and "things are fine now" in text
    assert "recorded judgement #1 for config incident: A is better" in text and "reason: A states the cause" in text
    assert "They agree with you." in text and "candidate rubric from your reason: judged_names_the_cause  [on]" in text
    assert "Next: evalctl review incident" in text


def test_long_files_are_shown_as_a_diff(pair):
    a, b = pair
    lines = [f"line {i}" for i in range(200)]
    (a / "report.md").write_text("\n".join(lines))
    (b / "report.md").write_text("\n".join(lines[:100] + ["CHANGED"] + lines[101:]))
    text = pairwise.describe_pair(a, b, ["report.md"])
    assert "200 lines in A, 200 in B" in text and "-line 100" in text and "+CHANGED" in text and "line 5\n" not in text
    assert "--- report.md in A (200 lines)" in pairwise.describe_pair(a, b, ["report.md"], full=True)


def test_identical_outputs_and_argument_errors(proj, pair, tmp_path):
    a, b = pair
    (b / "report.md").write_text(GOOD)
    code, text, _ = proj.run("judge", str(a), str(b), "--config", "incident", "--pick", "a", "--reason", "x")
    assert code == 0 and "identical, nothing to judge" in text and pairwise.summary() == {}
    code, _, err = proj.run("judge", str(a))
    assert code == 2 and "usage: evalctl judge DIR_A DIR_B" in err
    (b / "report.md").write_text(BAD)
    code, _, err = proj.run("judge", str(a), str(b), "--pick", "a", "--reason", "x")
    assert code == 2 and "Pass --config NAME" in err


def test_pairs_from_two_bench_runs(proj, grader, fake_agent, tmp_path, monkeypatch):
    proj.config(CHECKS[:1], name="incident", pointer=False, artifacts=["report.md"])
    suite = tmp_path / "s.yaml"
    suite.write_text(yaml.safe_dump({"config": "incident", "tasks": [{"id": "outage", "prompt": "Write the report"},
                                                                    {"id": "same", "prompt": "Write the other report"}]}))
    ids = []
    for text in (GOOD, BAD):
        fake_agent.script([{"write": {"report.md": text}}], [{"write": {"report.md": "## Summary\nsame in both\n"}}])
        code, out, _ = proj.run("bench", str(suite), "--agent", "fake", "--json")
        ids.append(json.loads(out)["run_id"])
    model(grader, passes=lambda item, cid: "full disk" in item)
    shown = []
    answers = iter(["a", "it names the cause"])

    def fake_input(prompt=""):
        shown.append(prompt)
        return next(answers)
    monkeypatch.setattr("builtins.input", fake_input)
    code, text, _ = proj.run("judge", "--bench", *ids)
    assert code == 0
    assert "task outage: config incident" in text and "shown in random order" in text
    assert "task same: the two outputs are identical, nothing to judge." in text
    assert "A was " in text and "B was " in text and len(shown) == 2          # one pair asked, paths revealed afterwards
    assert pairwise.summary()["incident"]["judgements"] == 1


def test_a_later_learn_run_keeps_the_judgement_candidates(proj, grader, pair, tmp_path):
    a, b = pair
    model(grader, passes=lambda item, cid: cid == "polite" or "full disk" in item)
    judge(proj, a, b)
    grader.reply = lambda prompt: json.dumps({"findings": []})
    code, out, err = proj.run("learn", "--name", "incident", "--draft", str(b), "--final", str(a), "--json", "--no-lint")
    assert code == 0, err
    ids = [c["id"] for c in json.loads(out)["candidates"]]
    assert "judged_names_the_cause" in ids and "report_md_exists" in ids
