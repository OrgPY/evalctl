"""Checks about relations: between numbers inside the output, and between the output and the task inputs."""
from __future__ import annotations

import re
from bisect import bisect_left
from decimal import Decimal
from pathlib import Path
from typing import List, Optional, Tuple

from ..config import CheckSpec, ConfigError
from .base import MAX_LISTED, Check, Context, Result, as_list, error, match_files, rel, result
from .numbers import (Num, Table, derived_values, file_numbers, fmt, numeric_columns, parse_number,
                      read_text, shown_as, tables_in)

DEFAULT_TOTAL_LABEL = r"^\W*(grand\s+|sub)?(total|sum)\b|^\W*(合计|总计|小计|总和)"
NO_INPUTS = ("this check compares the output with the task inputs, and no inputs directory is known. "
             "Pass `--inputs DIR`, set $EVALCTL_INPUTS, or add `inputs: <dir>` to .evalctl.yaml")


def _regexes(patterns: List[str], what: str) -> None:
    for pat in patterns:
        try:
            re.compile(pat)
        except (re.error, TypeError) as e:
            raise ConfigError(f"bad {what} {pat!r}: {e}")


def _cap(failures: List[str]) -> List[str]:
    n = len(failures)
    return failures if n <= MAX_LISTED else failures[:MAX_LISTED] + [f"... and {n - MAX_LISTED} more"]


def _input_files(ctx: Context, pattern: str) -> List[Path]:
    assert ctx.inputs is not None
    return [f for f in match_files(ctx.inputs, pattern) if f.is_file()]


class TableSumCheck(Check):
    """In every table with a total row, the total equals the sum of the other rows.

    file          glob; markdown tables in text files, or CSV files
    total_label   regex matched against the first cell of a row (default: Total, Sum, 合计, 总计, ...)
    columns       header names to check (default: every column that is numeric in all rows)
    tolerance     allowed absolute difference (default 0.01)
    require_total fail when a matched file has no table with a total row (default true)
    """

    type = "table_sum"
    allowed = ("file", "total_label", "columns", "tolerance", "require_total")
    required = ("file",)

    @classmethod
    def validate_extra(cls, spec: CheckSpec, config_dir: Path, workdir: Path) -> List[Path]:
        p = spec.params
        _regexes([p.get("total_label", DEFAULT_TOTAL_LABEL)], "total_label")
        tol = p.get("tolerance", 0.01)
        if isinstance(tol, bool) or not isinstance(tol, (int, float)) or tol < 0:
            raise ConfigError("`tolerance` must be a non-negative number")
        if not isinstance(p.get("require_total", True), bool):
            raise ConfigError("`require_total` must be true or false")
        return []

    @classmethod
    def run(cls, spec: CheckSpec, ctx: Context) -> Result:
        p = spec.params
        label_rx = re.compile(p.get("total_label", DEFAULT_TOTAL_LABEL), re.I)
        tol = Decimal(str(p.get("tolerance", 0.01)))
        want = [str(c) for c in as_list(p.get("columns"))]
        files = [f for f in match_files(ctx.workdir, p["file"]) if f.is_file()]
        if not files:
            return result(spec, [f"{p['file']}: no file matches"], "", "file missing")
        failures: List[str] = []
        checked = 0
        for f in files:
            label = rel(ctx.workdir, f)
            found = False
            for t in tables_in(f):
                is_total = [bool(r and label_rx.search(r[0])) for r in t.rows]
                if not any(is_total):
                    continue
                found = True
                parts = Table(t.header, [r for r, tot in zip(t.rows, is_total) if not tot])
                numeric = numeric_columns(parts)
                for col in want:
                    if col not in t.header:
                        failures.append(f"{label}:{t.line}: table has no column \"{col}\"")
                for col in (want or list(numeric)):
                    if col not in numeric or len(numeric[col]) != len(parts.rows) or not parts.rows:
                        if col in want and col in t.header:
                            failures.append(f"{label}:{t.line}: column \"{col}\" has cells that are not numbers")
                        continue
                    total = sum((n.value for n in numeric[col]), Decimal(0))
                    i = t.header.index(col)
                    for row, line, tot in zip(t.rows, t.lines, is_total):
                        if not tot:
                            continue
                        cell = row[i] if i < len(row) else ""
                        shown = parse_number(cell)
                        if shown is None:
                            if col in want or cell.strip():
                                failures.append(f"{label}:{line}: total row has no number in column \"{col}\" "
                                                f"(found \"{cell}\")")
                            continue
                        checked += 1
                        if abs(shown.value - total) > tol:
                            failures.append(
                                f"{label}:{line}: column \"{col}\": the total row says {cell.strip()} but the "
                                f"{len(parts.rows)} other row(s) sum to {fmt(total)} "
                                f"(difference {fmt(shown.value - total)}); correct the total or the rows")
            if not found and p.get("require_total", True):
                failures.append(f"{label}: no table with a total row found; add a total row to the table")
        return result(spec, _cap(failures), f"{checked} total(s) equal the sum of their rows",
                      f"{len(failures)} problem(s)")


class NumbersInInputsCheck(Check):
    """Every number in the output can be traced to the task inputs.

    A number is accepted when it appears in an input file, or (derived: true) when it is
    a row count, a column sum, or a per-group sum or count of an input table, or the sum
    of a table column in the same output file. Dates and times are not numbers.

    file     glob of output files
    inputs   glob inside the inputs directory (default: every file)
    derived  accept sums and counts (default true)
    ignore_small_integers  N: whole numbers with absolute value <= N are not checked
    ignore   regexes; matching text is skipped (e.g. '\\d+%' to skip percentages)
    """

    type = "numbers_in_inputs"
    allowed = ("file", "inputs", "derived", "ignore_small_integers", "ignore")
    required = ("file",)
    needs_inputs = True

    @classmethod
    def validate_extra(cls, spec: CheckSpec, config_dir: Path, workdir: Path) -> List[Path]:
        p = spec.params
        _regexes(as_list(p.get("ignore")), "ignore pattern")
        n = p.get("ignore_small_integers")
        if n is not None and (isinstance(n, bool) or not isinstance(n, int) or n < 0):
            raise ConfigError("`ignore_small_integers` must be a non-negative integer")
        if not isinstance(p.get("derived", True), bool):
            raise ConfigError("`derived` must be true or false")
        return []

    @classmethod
    def run(cls, spec: CheckSpec, ctx: Context) -> Result:
        p = spec.params
        if ctx.inputs is None:
            return error(spec, NO_INPUTS)
        derived = p.get("derived", True)
        known: List[Decimal] = []
        sources = _input_files(ctx, p.get("inputs", "**/*"))
        if not sources:
            return error(spec, f"no input file matches {p.get('inputs', '**/*')} in {ctx.inputs}")
        for f in sources:
            text = read_text(f)
            if text is None:
                continue
            known.extend(n.value for n in file_numbers(f, text))
            if derived:
                for t in tables_in(f, text):
                    known.extend(v for v, _ in derived_values(t))
        files = [f for f in match_files(ctx.workdir, p["file"]) if f.is_file()]
        if not files:
            return result(spec, [f"{p['file']}: no file matches"], "", "file missing")
        small = p.get("ignore_small_integers")
        failures: List[str] = []
        checked = 0
        for f in files:
            label = rel(ctx.workdir, f)
            text = read_text(f)
            if text is None:
                continue
            local = list(known)
            if derived:
                for t in tables_in(f, text):
                    rows = [r for r in t.rows if not (r and re.search(DEFAULT_TOTAL_LABEL, r[0], re.I))]
                    local.extend(v for v, what in derived_values(Table(t.header, rows))
                                 if what.startswith("sum of") and " where " not in what)
            local.sort()
            for n in file_numbers(f, text, as_list(p.get("ignore"))):
                if small is not None and n.decimals == 0 and abs(n.value) <= small:
                    continue
                checked += 1
                if not _traceable(n, local):
                    failures.append(
                        f"{label}{':' + str(n.line) if n.line else ''}: {n.text} does not appear in the inputs"
                        + (" and is not a sum or count of input rows" if derived else "")
                        + "; use the figure from the inputs or remove it")
        return result(spec, _cap(failures), f"{checked} number(s) traced to the inputs",
                      f"{len(failures)} number(s) not found in the inputs")


def _traceable(n: Num, known: List[Decimal]) -> bool:
    i = bisect_left(known, n.value)
    return any(shown_as(n, known[j]) for j in (i - 1, i, i + 1) if 0 <= j < len(known))


class CoversInputValuesCheck(Check):
    """Every distinct value of one column of an input table appears in the output.

    file    glob of output files; a value may appear in any of them
    input   glob inside the inputs directory (CSV, JSONL, JSON array, or markdown tables)
    column  column / field name in the input
    """

    type = "covers_input_values"
    allowed = ("file", "input", "column", "ignore_case")
    required = ("file", "input", "column")
    needs_inputs = True

    @classmethod
    def run(cls, spec: CheckSpec, ctx: Context) -> Result:
        p = spec.params
        if ctx.inputs is None:
            return error(spec, NO_INPUTS)
        col = str(p["column"])
        values: List[Tuple[str, str]] = []
        seen = set()
        sources = _input_files(ctx, p["input"])
        for f in sources:
            for t in tables_in(f):
                if col in t.header:
                    for v in t.column(col):
                        if v and v not in seen:
                            seen.add(v)
                            values.append((v, rel(ctx.inputs, f)))
        if not values:
            return error(spec, f"no input table matching {p['input']} in {ctx.inputs} has a column \"{col}\"")
        files = [f for f in match_files(ctx.workdir, p["file"]) if f.is_file()]
        if not files:
            return result(spec, [f"{p['file']}: no file matches"], "", "file missing")
        text = "\n".join(read_text(f) or "" for f in files)
        if p.get("ignore_case"):
            text = text.lower()
        missing = [(v, src) for v, src in values if (v.lower() if p.get("ignore_case") else v) not in text]
        failures = [f"{p['file']}: \"{v}\" ({src}, column {col}) is not mentioned; add it" for v, src in missing]
        return result(spec, _cap(failures), f"all {len(values)} value(s) of {col} are covered",
                      f"{len(failures)} of {len(values)} value(s) of {col} missing")
