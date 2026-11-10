# expense_report: a draft + final pair for `evalctl learn`

- `inputs/`  what the task started from
- `draft/`   the report as an agent produced it
- `final/`   the same report after a human corrected it

```bash
evalctl learn --inputs fixtures/expense_report/inputs --draft fixtures/expense_report/draft \
              --final fixtures/expense_report/final --name expense_report
```

What the human changed, and the check `learn` is expected to turn it into:

| Edit | Expected check |
| --- | --- |
| dates `11/02/2026` rewritten as `2026-11-02` | `report_md_dates_iso` (format) |
| `TODO: confirm ...` line deleted | `report_md_no_placeholders` (content) |
| Meals amount 175.55 corrected to 157.55 (the draft's figure is in no input) | `report_md_numbers_from_inputs` (relation) |
| missing Office row added, Total 1,341.94 corrected to 1,351.94 | `report_md_totals_add_up`, `report_md_covers_category` (relation) |
| `## Sources` section added | `report_md_sections` (structure) |
| `summary.json`: `count` added, `total` changed from string to number | `summary_json_structure` (structure) |
| `notes.tmp` deleted | `notes_tmp_absent` (structure) |
| "biggest spend bucket by far" reworded | case-specific wording checks, disabled by default |
| largest expense changed from Hotel Lumen 356.00 to Delta Air 412.80 | none: both figures are in the inputs and no supported relation tells them apart, so the numeric part is reported as not converted |
