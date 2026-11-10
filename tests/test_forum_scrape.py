"""The shipped forum_scrape config against the shipped fixtures.

The grader model is replaced by a local fake that fails bodies containing
obvious site-chrome phrases, so these tests are deterministic and offline.
They prove the plumbing; the rubric itself was exercised against a real model
by hand (see README, "Testing").
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "fixtures" / "forum_scrape"
CHROME = ("Log in", "SPONSORED", "cookies", "Advertisement", "register to reply")


@pytest.fixture
def scrape(proj, llm, home, monkeypatch):
    shutil.copy(REPO / "checks" / "forum_scrape.yaml", home / "checks" / "forum_scrape.yaml")
    (home / "judge.yaml").write_text(f"judges:\n  - {{provider: deepseek, model: m, base_url: '{llm.url}'}}\n")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    monkeypatch.setenv("EVALCTL_WORKER", "anthropic")
    llm.decide = lambda item: not any(c.lower() in item.lower() for c in CHROME)

    def run(name):
        shutil.copy(FIXTURES / name / "posts.jsonl", proj.dir / "posts.jsonl")
        (proj.dir / ".evalctl.yaml").write_text("config: forum_scrape\n")
        code, data = proj.check()
        return code, {c["id"]: c for c in data["checks"]}, data

    return run


def failed(by):
    return {cid for cid, c in by.items() if c["status"] == "fail"}


def test_good_sample_passes_and_gets_a_receipt(scrape, proj, llm):
    code, by, data = scrape("good")
    assert code == 0 and all(c["status"] == "pass" for c in by.values())
    assert len(llm.requests) == 10 and by["body_is_post_content"]["meta"]["total"] == 24
    rec = json.loads((proj.dir / ".eval" / "receipt.json").read_text())
    assert [a["path"] for a in rec["artifacts"]] == ["posts.jsonl"]
    assert proj.verify()[0] == 0


def test_missing_fields(scrape, llm):
    code, by, _ = scrape("missing_field")
    assert code == 1 and failed(by) == {"posts_schema"}
    assert by["posts_schema"]["failures"] == [
        "posts.jsonl:3: missing required field 'author'",
        "posts.jsonl:7: missing required field 'posted_at'",
        "posts.jsonl:12: field 'body' is empty",
    ]
    assert by["body_is_post_content"]["status"] == "skipped" and llm.requests == []


def test_duplicate_urls(scrape):
    code, by, _ = scrape("duplicate_url")
    assert code == 1 and failed(by) == {"unique_urls"}
    f = by["unique_urls"]["failures"]
    assert len(f) == 2
    assert f[0].startswith('posts.jsonl:9: duplicate url "https://forum.homelab.example/t/1054" '
                           "(first seen at posts.jsonl:2)")
    assert f[1].startswith("posts.jsonl:17: duplicate url") and "posts.jsonl:5" in f[1]


def test_too_few_posts(scrape):
    code, by, _ = scrape("too_few")
    assert code == 1 and failed(by) == {"post_count"}
    assert by["post_count"]["failures"] == ["posts.jsonl: has 5 record(s), need at least 20 (15 more)"]


def test_navigation_bodies_pass_hard_checks_but_fail_the_rubric(scrape, proj):
    code, by, data = scrape("nav_body")
    assert code == 1 and failed(by) == {"body_is_post_content"}
    f = by["body_is_post_content"]["failures"]
    assert "criterion 'real_post_body'" in f[0]
    bad_lines = [x for x in f if x.startswith("  posts.jsonl:")]
    assert bad_lines and all(int(x.split(":")[1]) % 2 == 1 for x in bad_lines)  # odd lines carry chrome
    assert "re-extract body for ALL posts" in data["text"]
    assert not (proj.dir / ".eval" / "receipt.json").exists()


def test_missing_and_malformed_url(scrape, proj):
    recs = [json.loads(l) for l in (FIXTURES / "good" / "posts.jsonl").read_text().splitlines()]
    del recs[0]["url"]
    recs[1]["url"] = ""
    recs[2]["url"] = "/t/1061"
    (proj.dir / ".evalctl.yaml").write_text("config: forum_scrape\n")
    proj.write("posts.jsonl", recs)
    code, data = proj.check()
    by = {c["id"]: c for c in data["checks"]}
    assert failed(by) == {"every_post_has_url"}
    f = by["every_post_has_url"]["failures"]
    assert f[0] == "posts.jsonl:1: missing required field 'url'"
    assert f[1] == "posts.jsonl:2: field 'url' is empty"
    assert any(x.startswith("posts.jsonl:3: field 'url' does not match pattern") for x in f)
