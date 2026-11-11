#!/usr/bin/env python3
"""Regenerate the failed runs with a known cause that `evalctl explain` is tested on.

    python3 fixtures/explain/build.py

Each case directory looks like a round directory of `evalctl run` (.eval/rounds/N/):
  input.md          the task
  trajectory.jsonl  what the agent did, in the real output format of Claude Code
                    (`claude -p --output-format stream-json`) or Codex (`codex exec --json`)
  check.json        the failed check items
  expected.json     the cause a correct explanation gives: category, and the step(s) accepted
                    as "the earliest step that went wrong"

Explain one:  evalctl explain --round fixtures/explain/missed_material
"""
import json
from pathlib import Path

HERE = Path(__file__).parent
W = "/work/task"


class Claude:
    """Events as Claude Code prints them in stream-json mode."""

    def __init__(self):
        self.events = [{"type": "system", "subtype": "init", "session_id": "s-fixture", "cwd": W}]
        self.n = 0

    def say(self, text):
        self.events.append({"type": "assistant", "session_id": "s-fixture",
                            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})

    def tool(self, name, input, result, error=False):
        self.n += 1
        tid = f"toolu_{self.n:02d}"
        self.events.append({"type": "assistant", "session_id": "s-fixture", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": name, "input": input}]}})
        self.events.append({"type": "user", "session_id": "s-fixture", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": result, "is_error": error}]}})

    def done(self, text):
        self.say(text)
        self.events.append({"type": "result", "subtype": "success", "result": text, "session_id": "s-fixture"})
        return self.events


class Codex:
    """Events as `codex exec --json` prints them."""

    def __init__(self):
        self.events = [{"type": "thread.started", "thread_id": "t-fixture"}, {"type": "turn.started"}]
        self.n = 0

    def _item(self, **item):
        self.events.append({"type": "item.completed", "item": {"id": f"item_{self.n}", **item}})
        self.n += 1

    def say(self, text):
        self._item(type="agent_message", text=text)

    def run(self, command, output, exit_code=0):
        self._item(type="command_execution", command=f"/bin/zsh -lc {json.dumps(command)}", aggregated_output=output,
                   exit_code=exit_code, status="completed" if exit_code == 0 else "failed")

    def done(self, text):
        self.say(text)
        self.events.append({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}})
        return self.events


def check(*failed, passed=()):
    checks = [dict(c, status="fail") for c in failed] + [{"id": p, "type": "file", "status": "pass", "summary": "ok"} for p in passed]
    return {"status": "fail", "passed": False, "round": 1, "max_rounds": 3, "checks": checks}


EXPENSES = """date,category,vendor,amount
2026-11-02,Travel,Delta Air,412.80
2026-11-02,Meals,Blue Door Cafe,38.50
2026-11-03,Software,JetBrains,249.00
2026-11-03,Travel,City Cab,27.40
2026-11-04,Meals,Noodle Bar,22.75
2026-11-04,Office,Paper Co,64.20
2026-11-05,Software,Figma,45.00
2026-11-05,Travel,Hotel Lumen,356.00
2026-11-06,Meals,Team Lunch,96.30
2026-11-06,Office,Desk Lamp,39.99
"""
REPORT_TASK = ("Write the weekly expense report for 2026-11-02 to 2026-11-06 from expenses.csv, following brief.md. "
               "Deliver report.md with a Breakdown table (one row per category and a Total row).")
BRIEF = "# Brief\n\nAudience: the finance lead. Keep it to one page. Amounts with two decimals.\n"
SCRAPE_TASK = ("page.html is a saved listing page from forum.homelab.example. Extract every post on it into posts.jsonl, "
               "one JSON object per line, with title, url, author, posted_at (ISO 8601) and body.")


def table(travel, meals, software, office, total):
    return ("# Weekly expense report\n\nPeriod: 2026-11-02 to 2026-11-06\n\n## Breakdown\n\n"
            "| Category | Items | Amount |\n| --- | --- | --- |\n"
            f"| Travel | 3 | {travel} |\n| Meals | 3 | {meals} |\n| Software | 2 | {software} |\n| Office | 2 | {office} |\n"
            f"| Total | 10 | {total} |\n")


def missed_material():
    """The agent reads the brief but never opens expenses.csv, and invents plausible figures."""
    t = Claude()
    t.say("I'll start by reading the brief.")                                                    # 1
    t.tool("Read", {"file_path": f"{W}/brief.md"}, BRIEF)                                        # 2
    t.tool("Glob", {"pattern": "*"}, "brief.md\nexpenses.csv")                                    # 3
    t.say("The brief asks for a one-page report with a category breakdown. I have what I need; writing the report now.")  # 4
    t.tool("Write", {"file_path": f"{W}/report.md", "content": table("820.00", "150.00", "300.00", "100.00", "1,370.00")},
           f"File created successfully at: {W}/report.md")                                       # 5
    events = t.done("report.md is written: Travel is the largest category at 820.00, total 1,370.00.")   # 6
    return events, REPORT_TASK, check(
        {"id": "report_md_numbers_from_inputs", "type": "numbers_in_inputs", "summary": "5 problem(s)", "failures": [
            "report.md:9: 820.00 is not in the inputs and is not a sum or count of input rows",
            "report.md:10: 150.00 is not in the inputs and is not a sum or count of input rows",
            "report.md:11: 300.00 is not in the inputs and is not a sum or count of input rows",
            "report.md:12: 100.00 is not in the inputs and is not a sum or count of input rows",
            "report.md:13: 1,370.00 is not in the inputs and is not a sum or count of input rows"]},
        passed=["report_md_exists"]), {"category": "missed_material", "steps": [4, 5]}


def reasoning_error():
    """The agent reads every row, then adds the Meals rows up wrongly (38.50 + 22.75 + 96.30 is 157.55, not 147.55)."""
    t = Claude()
    t.tool("Read", {"file_path": f"{W}/brief.md"}, BRIEF)                                        # 1
    t.tool("Read", {"file_path": f"{W}/expenses.csv"}, EXPENSES)                                 # 2
    t.say("Adding up per category. Travel: 412.80 + 27.40 + 356.00 = 796.20. Meals: 38.50 + 22.75 + 96.30 = 147.55. "
          "Software: 249.00 + 45.00 = 294.00. Office: 64.20 + 39.99 = 104.19. Total: 1,351.94.")  # 3
    t.tool("Write", {"file_path": f"{W}/report.md", "content": table("796.20", "147.55", "294.00", "104.19", "1,351.94")},
           f"File created successfully at: {W}/report.md")                                       # 4
    events = t.done("Done: report.md has the breakdown and the total of 1,351.94.")              # 5
    return events, REPORT_TASK, check(
        {"id": "report_md_totals_add_up", "type": "table_sum", "summary": "1 problem(s)", "failures": [
            "report.md:13: column Amount: Total row says 1,351.94 but the rows above add up to 1,341.94"],
         "hint": "Recompute the total row from the rows above it."},
        {"id": "report_md_numbers_from_inputs", "type": "numbers_in_inputs", "summary": "1 problem(s)", "failures": [
            "report.md:10: 147.55 is not in the inputs and is not a sum or count of input rows"]},
        passed=["report_md_exists"]), {"category": "reasoning_error", "steps": [3]}


def format_mismatch():
    """Every post is extracted correctly, but with the field names and date format of the page, not the required ones."""
    t = Claude()
    page = ('<article class="post"><h2><a href="/t/1047">Proxmox cluster loses quorum after every power cut</a></h2>'
            '<a class="author">nas_gardener</a> <time datetime="2026-10-02T09:07:00Z">Oct 2, 2026 at 09:07</time>'
            '<div class="post-body"><p>I run three NUCs in a Proxmox cluster ...</p></div></article>\n'
            '<article class="post"><h2><a href="/t/1054">ZFS scrub taking 31 hours on 4x12TB raidz1, is that normal?</a></h2>'
            '<a class="author">blockpointer</a> <time datetime="2026-10-03T10:14:00Z">Oct 3, 2026 at 10:14</time>'
            '<div class="post-body"><p>The pool is about 70 percent full ...</p></div></article>\n... (24 posts)')
    t.tool("Read", {"file_path": f"{W}/page.html"}, page)                                        # 1
    t.say("The page has 24 posts. I'll write one JSON object per post with its title, link, author, date and text.")   # 2
    rows = "\n".join(json.dumps(r) for r in [
        {"title": "Proxmox cluster loses quorum after every power cut", "link": "/t/1047", "author": "nas_gardener",
         "date": "Oct 2, 2026 at 09:07", "text": "I run three NUCs in a Proxmox cluster ..."},
        {"title": "ZFS scrub taking 31 hours on 4x12TB raidz1, is that normal?", "link": "/t/1054", "author": "blockpointer",
         "date": "Oct 3, 2026 at 10:14", "text": "The pool is about 70 percent full ..."}]) + "\n... (24 lines)"
    t.tool("Write", {"file_path": f"{W}/posts.jsonl", "content": rows}, f"File created successfully at: {W}/posts.jsonl")   # 3
    events = t.done("All 24 posts are in posts.jsonl.")                                           # 4
    return events, SCRAPE_TASK, check(
        {"id": "posts_schema", "type": "schema", "summary": "48 problem(s)", "failures": [
            "posts.jsonl:1: missing required field 'posted_at'", "posts.jsonl:1: missing required field 'body'",
            "posts.jsonl:2: missing required field 'posted_at'", "posts.jsonl:2: missing required field 'body'"],
         "hint": "Every line needs title, url, author, posted_at (ISO 8601 date-time) and body as non-empty strings."},
        {"id": "every_post_has_url", "type": "schema", "summary": "24 problem(s)", "failures": [
            "posts.jsonl:1: missing required field 'url'", "posts.jsonl:2: missing required field 'url'"]},
        passed=["posts_file", "unique_urls", "post_count"]), {"category": "format_mismatch", "steps": [2, 3]}


def tool_error():
    """The extraction script crashes after 5 posts; the agent sees the traceback and delivers the partial file anyway."""
    t = Codex()
    t.say("I'll write a small extraction script.")                                               # 1
    t.run("ls", "extract.py\npage.html\n")                                                       # 2
    t.run("python3 extract.py page.html > posts.jsonl",
          'Traceback (most recent call last):\n  File "extract.py", line 19, in <module>\n    author = post.find("a", class_="author").text\n'
          "AttributeError: 'NoneType' object has no attribute 'text'\n", exit_code=1)             # 3
    t.run("wc -l posts.jsonl", "       5 posts.jsonl\n")                                         # 4
    events = t.done("posts.jsonl has been written with the extracted posts.")                    # 5
    return events, SCRAPE_TASK, check(
        {"id": "post_count", "type": "schema", "summary": "1 problem(s)", "failures": [
            "posts.jsonl: 5 record(s), expected at least 20"],
         "hint": "Too few usually means pagination stopped early; scrape the remaining pages."},
        passed=["posts_file", "posts_schema", "every_post_has_url", "unique_urls"]), {"category": "tool_error", "steps": [3]}


def environment():
    """The forum cannot be reached at all (DNS failure in the sandbox); the agent delivers an empty file."""
    t = Codex()
    t.say("I'll fetch the listing page first.")                                                  # 1
    t.run("curl -sS https://forum.homelab.example/latest -o page.html",
          "curl: (6) Could not resolve host: forum.homelab.example\n", exit_code=6)               # 2
    t.run("curl -sS --retry 2 https://forum.homelab.example/latest -o page.html",
          "curl: (6) Could not resolve host: forum.homelab.example\n", exit_code=6)               # 3
    t.run("touch posts.jsonl", "")                                                                # 4
    events = t.done("The site could not be reached from this environment, so posts.jsonl is empty.")   # 5
    task = ("Scrape the latest posts from https://forum.homelab.example/latest into posts.jsonl, one JSON object per "
            "line, with title, url, author, posted_at (ISO 8601) and body.")
    return events, task, check(
        {"id": "posts_file", "type": "file", "summary": "1 problem(s)", "failures": ["posts.jsonl: 0 bytes, minimum is 1"]},
        {"id": "post_count", "type": "schema", "summary": "1 problem(s)", "failures": ["posts.jsonl: 0 record(s), expected at least 20"]},
    ), {"category": "environment", "steps": [2]}


def checker_false_positive():
    """The report is right. The placeholder check trips over a vendor that is really called "TBD Logistics"."""
    csv = EXPENSES.replace("Paper Co", "TBD Logistics")
    t = Claude()
    t.tool("Read", {"file_path": f"{W}/brief.md"}, BRIEF)                                        # 1
    t.tool("Read", {"file_path": f"{W}/expenses.csv"}, csv)                                      # 2
    t.say("Totals per category: Travel 796.20, Meals 157.55, Software 294.00, Office 104.19; overall 1,351.94. "
          "The largest Office expense is TBD Logistics, 64.20 (that is the vendor's name in expenses.csv).")   # 3
    report = table("796.20", "157.55", "294.00", "104.19", "1,351.94") + "\n## Largest Office expense\n\nTBD Logistics, 64.20, on 2026-11-04.\n"
    t.tool("Write", {"file_path": f"{W}/report.md", "content": report}, f"File created successfully at: {W}/report.md")   # 4
    events = t.done("report.md is written; all figures are taken from expenses.csv.")            # 5
    return events, REPORT_TASK, check(
        {"id": "report_md_no_placeholders", "type": "regex", "summary": "1 problem(s)", "failures": [
            "report.md:17: forbidden pattern '\\b(TODO|TBD|FIXME|XXX)\\b' found: \"TBD Logistics, 64.20, on 2026-11-04.\""],
         "hint": "Replace every placeholder with real content, or remove the line."},
        passed=["report_md_exists", "report_md_totals_add_up", "report_md_numbers_from_inputs"]), \
        {"category": "checker_false_positive", "steps": [4]}


CASES = [missed_material, reasoning_error, format_mismatch, tool_error, environment, checker_false_positive]


def main():
    for case in CASES:
        events, task, checks, expected = case()
        d = HERE / case.__name__
        d.mkdir(exist_ok=True)
        (d / "trajectory.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events))
        (d / "input.md").write_text(task + "\n")
        (d / "check.json").write_text(json.dumps(checks, indent=2, ensure_ascii=False) + "\n")
        (d / "expected.json").write_text(json.dumps(dict(expected, why=case.__doc__.strip()), indent=2) + "\n")


if __name__ == "__main__":
    main()
