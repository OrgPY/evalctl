"""Numbers and tables in text, shared by the relation checks and `evalctl learn`."""
from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Dates and times are not quantities; they are blanked out before numbers are read.
DATE_ISO = r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?"
DATE_SLASH = r"\b\d{1,4}[/.]\d{1,2}[/.]\d{2,4}\b"
TIME = r"\b\d{1,2}:\d{2}(?::\d{2})?\b"
_NOT_QUANTITY = re.compile(f"{DATE_ISO}|{DATE_SLASH}|{TIME}")
_LIST_MARKER = re.compile(r"^(\s*)\d+([.)]\s)", re.M)
NUMBER = re.compile(
    r"(?<![A-Za-z0-9_.,])-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?![A-Za-z0-9_]|[.,]\d)"
)


@dataclass
class Num:
    value: Decimal
    decimals: int
    text: str
    line: int


def parse_number(s: str) -> Optional[Num]:
    """A whole cell as a number: '1,234.50', '$12', '45%', '**10**'. None if it is not one."""
    t = re.sub(r"[*_`\s$€£¥%]", "", str(s))
    if not NUMBER.fullmatch(t):
        return None
    return _num(t, 0)


def _num(text: str, line: int) -> Optional[Num]:
    plain = text.replace(",", "")
    try:
        v = Decimal(plain)
    except InvalidOperation:
        return None
    return Num(v, len(plain.split(".")[1]) if "." in plain else 0, text, line)


def _blank(m: "re.Match[str]") -> str:
    return " " * len(m.group(0))


def extract_numbers(text: str, ignore: Iterable[str] = ()) -> List[Num]:
    """Every quantity in `text`, with its line. Dates, times and list markers are skipped."""
    text = _NOT_QUANTITY.sub(_blank, text)
    text = _LIST_MARKER.sub(lambda m: m.group(1) + " " * (len(m.group(0)) - len(m.group(1))), text)
    for pat in ignore:
        text = re.sub(pat, _blank, text)
    out = []
    for m in NUMBER.finditer(text):
        n = _num(m.group(0), text.count("\n", 0, m.start()) + 1)
        if n is not None:
            out.append(n)
    return out


def file_numbers(path: Path, text: str, ignore: Iterable[str] = ()) -> List[Num]:
    """Quantities in a file. CSV and JSON are read by cell / value, so separators are not mistaken
    for thousands commas; anything else is read as text. `line` is 0 where it is not known."""
    ext = path.suffix.lower()
    if ext == ".csv":
        out: List[Num] = []
        reader = csv.reader(io.StringIO(text, newline=""))
        for row in reader:
            for cell in row:
                for n in extract_numbers(cell, ignore):
                    n.line = reader.line_num
                    out.append(n)
        return out
    if ext in (".json", ".jsonl", ".ndjson"):
        docs = [(i, ln) for i, ln in enumerate(text.splitlines(), 1) if ln.strip()] if ext != ".json" else [(0, text)]
        out = []

        def walk(v: Any, line: int) -> None:
            if isinstance(v, Num):
                v.line = line
                out.append(v)
            elif isinstance(v, str):
                for n in extract_numbers(v, ignore):
                    n.line = line
                    out.append(n)
            elif isinstance(v, dict):
                for x in v.values():
                    walk(x, line)
            elif isinstance(v, list):
                for x in v:
                    walk(x, line)

        try:
            for line, src in docs:
                walk(json.loads(src, parse_float=lambda t: _num(t, 0), parse_int=lambda t: _num(t, 0)), line)
            return [n for n in out if n is not None]
        except (json.JSONDecodeError, InvalidOperation):
            pass
    return extract_numbers(text, ignore)


def shown_as(shown: Num, actual: Decimal) -> bool:
    """True if `actual`, rounded to the decimals `shown` is written with, gives `shown`."""
    return abs(actual - shown.value) <= Decimal(5) / (Decimal(10) ** (shown.decimals + 1))


def fmt(v: Decimal, decimals: int = 2) -> str:
    q = v.quantize(Decimal(1).scaleb(-decimals)) if v != v.to_integral_value() else v.to_integral_value()
    return f"{q:,}"


@dataclass
class Table:
    header: List[str]
    rows: List[List[str]]
    lines: List[int] = field(default_factory=list)  # 1-based line of each row
    line: int = 1                                    # line of the header

    def column(self, name: str) -> List[str]:
        i = self.header.index(name)
        return [r[i] if i < len(r) else "" for r in self.rows]


def _md_cells(line: str) -> List[str]:
    s = line.strip()
    s = s[1:] if s.startswith("|") else s
    s = s[:-1] if s.endswith("|") else s
    return [c.strip() for c in re.split(r"(?<!\\)\|", s)]


def markdown_tables(text: str) -> List[Table]:
    out: List[Table] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines) - 1:
        if "|" in lines[i] and re.fullmatch(r"\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*", lines[i + 1]) \
                and "|" in lines[i + 1]:
            t = Table(_md_cells(lines[i]), [], [], i + 1)
            j = i + 2
            while j < len(lines) and "|" in lines[j] and lines[j].strip():
                t.rows.append(_md_cells(lines[j]))
                t.lines.append(j + 1)
                j += 1
            out.append(t)
            i = j
        else:
            i += 1
    return out


def csv_table(text: str) -> Optional[Table]:
    reader = csv.reader(io.StringIO(text, newline=""))
    t: Optional[Table] = None
    for row in reader:
        if t is None:
            t = Table([c.strip() for c in row], [], [], reader.line_num)
        elif any(c.strip() for c in row):
            t.rows.append([c.strip() for c in row])
            t.lines.append(reader.line_num)
    return t


def _records_table(records: List[Any]) -> Optional[Table]:
    recs = [r for r in records if isinstance(r, dict)]
    if not recs or len(recs) != len(records):
        return None
    header: List[str] = []
    for r in recs:
        header.extend(k for k in r if k not in header)
    cell = lambda v: "" if v is None else (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))
    return Table(header, [[cell(r.get(k)) for k in header] for r in recs], list(range(1, len(recs) + 1)))


def read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8-sig")
    except (UnicodeDecodeError, OSError):
        return None


def tables_in(path: Path, text: Optional[str] = None) -> List[Table]:
    """Tables in a file: a CSV, a JSON array / JSONL of flat records, or markdown tables in text."""
    text = read_text(path) if text is None else text
    if text is None:
        return []
    ext = path.suffix.lower()
    if ext == ".csv":
        t = csv_table(text)
        return [t] if t and t.header else []
    if ext in (".jsonl", ".ndjson"):
        try:
            t = _records_table([json.loads(ln) for ln in text.splitlines() if ln.strip()])
        except json.JSONDecodeError:
            return []
        return [t] if t else []
    if ext == ".json":
        try:
            doc = json.loads(text)
        except json.JSONDecodeError:
            return []
        lists = [doc] if isinstance(doc, list) else \
            [v for v in doc.values() if isinstance(v, list)] if isinstance(doc, dict) else []
        return [t for t in (_records_table(l) for l in lists) if t]
    return markdown_tables(text)


def numeric_columns(t: Table) -> Dict[str, List[Num]]:
    """Columns where every non-empty cell is a number."""
    out: Dict[str, List[Num]] = {}
    for name in t.header:
        if not name or t.header.count(name) > 1:
            continue
        cells = [c for c in t.column(name) if c != ""]
        nums = [parse_number(c) for c in cells]
        if cells and all(n is not None for n in nums):
            out[name] = nums  # type: ignore[assignment]
    return out


def derived_values(t: Table, max_groups: int = 200) -> List[Tuple[Decimal, str]]:
    """Numbers that can be computed from a table: row count, column sums, and per-group sums and counts."""
    out: List[Tuple[Decimal, str]] = [(Decimal(len(t.rows)), "row count")]
    numeric = numeric_columns(t)
    full = {k: v for k, v in numeric.items() if len(v) == len(t.rows)}
    for name, nums in full.items():
        out.append((sum((n.value for n in nums), Decimal(0)), f"sum of {name}"))
    for name in t.header:
        if not name or name in numeric or t.header.count(name) > 1:
            continue
        keys = t.column(name)
        groups: Dict[str, List[int]] = {}
        for i, k in enumerate(keys):
            groups.setdefault(k, []).append(i)
        if len(groups) > max_groups:
            continue
        out.append((Decimal(len(groups)), f"number of distinct {name}"))
        if len(groups) == len(keys):
            continue
        for k, idx in groups.items():
            out.append((Decimal(len(idx)), f"rows with {name}={k}"))
            for col, nums in full.items():
                out.append((sum((nums[i].value for i in idx), Decimal(0)), f"sum of {col} where {name}={k}"))
    return out
