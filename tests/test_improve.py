"""`evalctl improve`: a manual tool that proposes config changes from the record and applies what is approved."""
from __future__ import annotations

import json

import yaml

from evalctl import store

CHECKS = [{"id": "has_report", "type": "file", "path": "report.md"},
          {"id": "no_draft", "type": "contains", "file": "report.md", "must_not": ["draft"]},
          {"id": "quality", "type": "llm_rubric", "file": "report.md",
           "rubric": [{"id": "clear", "text": "States a finding. NOT clear without one."}, {"id": "nice", "text": "The report is good."}]}]


def test_proposals_come_from_the_record_and_nothing_changes_without_approval(proj, grader, monkeypatch):
    path = proj.config(CHECKS)
    proj.pointer(loop={"judge_repeat": {"n": 4, "agree": 3, "when": "always"}})
    before = path.read_text()
    # three tasks: `nice` never gets a stable verdict; and twice the user overrules no_draft
    for i in range(3):
        seen = {}

        def verdict(item, cid, n, seen=seen):
            seen[cid] = seen.get(cid, 0) + 1
            return cid == "clear" or seen[cid] % 2 == 1
        grader.verdict = verdict
        proj.write("report.md", f"finding {i}")
        assert proj.check()[0] == 0
    for i in range(2):
        proj.write("report.md", f"first draft of finding {i}")
        proj.run("feedback", "--dir", str(proj.dir), "--verdict", "good")
    code, out, _ = proj.run("improve", "t", "--json")
    props = {(p["action"], p["target"]): p for p in json.loads(out)}
    assert code == 0 and ("remove_check", "no_draft") in props and ("remove_criterion", "quality.nice") in props
    assert "you judged the output good 2 times" in props[("remove_check", "no_draft")]["why"]
    assert "agree only 50% of the time" in props[("remove_criterion", "quality.nice")]["why"]
    assert ("remove_criterion", "quality.clear") not in props
    assert path.read_text() == before                                  # proposing changes nothing

    code, text, _ = proj.run("improve", "t")
    assert "Nothing is changed until you approve" in text and "evalctl improve t --apply" in text
    answers = iter(["n", "y"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    code, text, _ = proj.run("improve", "t", "--apply")                # reject the first, approve the second
    assert code == 0 and "Applied 1 change(s)" in text
    after = yaml.safe_load(path.read_text())
    assert [c["id"] for c in after["checks"]] == ["has_report", "no_draft", "quality"]
    assert [r["id"] for r in after["checks"][2]["rubric"]] == ["clear"]
    assert path.with_name("t.yaml.bak").read_text() == before
    code, text, _ = proj.run("improve", "t", "--approve", "P1")        # already rejected: not pending any more
    assert code == 2


def test_nothing_to_propose_and_improve_is_not_part_of_any_flow(proj):
    proj.config(CHECKS[:1])
    proj.write("report.md", "x")
    proj.check()
    code, text, _ = proj.run("improve", "t")
    assert code == 0 and "nothing to propose" in text
    assert not (proj.home / "improve" / "t.yaml").read_text().count("remove_")
