#!/usr/bin/env python3
"""Regenerate the multi-final example for `evalctl learn --finals`: three weekly expense reports.

    python3 fixtures/expense_reports_multi/build.py

Each week_*/ has inputs/expenses.csv and final/ (report.md, summary.json, items.csv), all correct.
The three weeks share structure, formats and relations (sections, ISO dates, two decimals, totals
that add up, every figure traceable to the inputs). They differ in what is specific to a week:
the number of expenses (10, 7, 13), the categories, and week_47 has an extra "## Notes" section.
"""
import csv
import json
from collections import OrderedDict
from pathlib import Path

HERE = Path(__file__).parent
WEEKS = {
    "week_45": ("2026-11-02", "2026-11-06", None, [
        ("2026-11-02", "Travel", "Delta Air", "412.80"), ("2026-11-02", "Meals", "Blue Door Cafe", "38.50"),
        ("2026-11-03", "Software", "JetBrains", "249.00"), ("2026-11-03", "Travel", "City Cab", "27.40"),
        ("2026-11-04", "Meals", "Noodle Bar", "22.75"), ("2026-11-04", "Office", "Paper Co", "64.20"),
        ("2026-11-05", "Software", "Figma", "45.00"), ("2026-11-05", "Travel", "Hotel Lumen", "356.00"),
        ("2026-11-06", "Meals", "Team Lunch", "96.30"), ("2026-11-06", "Office", "Desk Lamp", "39.99")]),
    "week_46": ("2026-11-09", "2026-11-13", None, [
        ("2026-11-09", "Meals", "Blue Door Cafe", "41.20"), ("2026-11-10", "Software", "Linear", "96.00"),
        ("2026-11-10", "Travel", "City Cab", "31.65"), ("2026-11-11", "Meals", "Noodle Bar", "19.80"),
        ("2026-11-12", "Travel", "Amtrak", "128.00"), ("2026-11-12", "Software", "Notion", "60.00"),
        ("2026-11-13", "Meals", "Team Lunch", "112.45")]),
    "week_47": ("2026-11-16", "2026-11-20", "The Training line is the conference workshop approved in October.", [
        ("2026-11-16", "Travel", "United", "538.10"), ("2026-11-16", "Meals", "Airport Deli", "17.25"),
        ("2026-11-17", "Training", "DevConf Workshop", "450.00"), ("2026-11-17", "Travel", "Hotel Aster", "289.00"),
        ("2026-11-17", "Meals", "Blue Door Cafe", "36.90"), ("2026-11-18", "Travel", "Hotel Aster", "289.00"),
        ("2026-11-18", "Meals", "Conference Dinner", "74.60"), ("2026-11-19", "Software", "JetBrains", "249.00"),
        ("2026-11-19", "Office", "Paper Co", "58.35"), ("2026-11-19", "Travel", "City Cab", "44.80"),
        ("2026-11-20", "Meals", "Team Lunch", "88.15"), ("2026-11-20", "Office", "Monitor Arm", "119.00"),
        ("2026-11-20", "Software", "Figma", "45.00")]),
}


def money(cents, sep=True):
    return f"{cents / 100:,.2f}" if sep else f"{cents / 100:.2f}"


def main():
    for week, (start, end, note, rows) in WEEKS.items():
        inputs, final = HERE / week / "inputs", HERE / week / "final"
        inputs.mkdir(parents=True, exist_ok=True)
        final.mkdir(parents=True, exist_ok=True)
        with open(inputs / "expenses.csv", "w", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["date", "category", "vendor", "amount"])
            w.writerows(rows)
        cents = lambda a: int(a.replace(".", ""))
        cats = OrderedDict()
        for _, cat, _, amount in rows:
            n, total = cats.get(cat, (0, 0))
            cats[cat] = (n + 1, total + cents(amount))
        ordered = sorted(cats.items(), key=lambda kv: -kv[1][1])
        total = sum(v[1] for v in cats.values())
        top = max(rows, key=lambda r: cents(r[3]))
        lines = ["# Weekly expense report", "", f"Period: {start} to {end}", "", "## Summary", "",
                 f"{len(rows)} expenses were filed this week across {len(cats)} categories. "
                 f"{ordered[0][0]} was the largest category.", "", "## Breakdown", "",
                 "| Category | Items | Amount |", "| --- | --- | --- |"]
        lines += [f"| {c} | {n} | {money(t)} |" for c, (n, t) in ordered]
        lines += [f"| Total | {len(rows)} | {money(total)} |", "", "## Largest expense", "",
                  f"{top[2]}, {top[3]}, on {top[0]}.", ""]
        if note:
            lines += ["## Notes", "", note, ""]
        lines += ["## Sources", "", "- expenses.csv", ""]
        (final / "report.md").write_text("\n".join(lines))
        (final / "summary.json").write_text(json.dumps({
            "period_start": start, "period_end": end, "count": len(rows), "total": total / 100,
            "by_category": [{"category": c, "items": n, "amount": t / 100} for c, (n, t) in ordered]}, indent=2) + "\n")
        with open(final / "items.csv", "w", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["date", "category", "vendor", "amount"])
            w.writerows(sorted(rows))


if __name__ == "__main__":
    main()
