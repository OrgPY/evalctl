"""`evalctl learn`: turn one worked example into candidate checks.

Given the task inputs, the agent's draft and the version a human corrected
(final), this module

  1. lists every human edit (draft -> final),
  2. proposes checks from what the final output looks like and how it relates
     to the inputs (structure, format, relations),
  3. validates each proposal: it must pass on the final output; to count as
     "from an edit" it must also fail on the draft,
  4. attributes checks to edits by reverting one edit at a time in a copy of
     the final output and seeing which checks start to fail,
  5. for edits no general rule explains, falls back to literal, case-specific
     checks (disabled by default). Literal numbers are never written into a
     check: a numeric edit is either explained by a relation or reported as
     not converted.

Nothing here calls a model; the same example always gives the same candidates.
"""
from __future__ import annotations

import difflib
import glob as _glob
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from . import paths, receipt
from .checks import ERROR, FAIL, PASS, REGISTRY, Context, Result
from .checks.base import clip
from .checks.numbers import DATE_ISO, extract_numbers, file_numbers, numeric_columns, parse_number, read_text, tables_in
from .checks.records import _format_ok, detect_format, load_records
from .checks.relations import DEFAULT_TOTAL_LABEL
from .config import CheckSpec, Config, ConfigError

GENERAL, SPECIFIC = "general", "case-specific"
SCOPE_LABEL = {GENERAL: "general rule", SPECIFIC: "case-specific"}
RECORD_EXTS = {".json", ".jsonl", ".ndjson", ".csv"}
TEMP_SUFFIXES = (".tmp", ".bak", ".orig", ".log", ".swp", "~")
PLACEHOLDERS = [r"\b(TODO|TBD|FIXME|XXX)\b", r"(?i)lorem ipsum", r"待补充|待定|待确认", r"\?\?\?"]
SLASH_DATES = [r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", r"\b\d{4}/\d{1,2}/\d{1,2}\b"]
NOT_TWO_DECIMALS = r"(?<![\w.,])\d[\d,]*\.(?:\d|\d{3,})(?!\w|\.\d)"
ID_LIKE = re.compile(r"(^|_)(id|url|uri|key|slug|email)$", re.I)
NAME_RX = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def learned_path(name: str) -> Path:
    return paths.home() / "learned" / f"{name}.yaml"


def check_name(name: str) -> str:
    if not NAME_RX.fullmatch(name):
        raise ConfigError(f"bad name {name!r}: use letters, digits, '_', '-' and '.'")
    return name


# ---------------------------------------------------------------- edits

@dataclass
class Edit:
    id: str
    file: str
    kind: str                 # changed | added | removed | file_added | file_removed
    line: int = 0             # line in the final file
    before: List[str] = field(default_factory=list)
    after: List[str] = field(default_factory=list)
    span: Tuple[int, int] = (0, 0)   # [b0, b1) in the final file's lines
    numeric: bool = False     # the edit changes a number
    trivial: bool = False     # whitespace only

    def describe(self) -> str:
        if self.kind == "file_added":
            return "file added"
        if self.kind == "file_removed":
            return "file removed"
        b = clip(" / ".join(s.strip() for s in self.before if s.strip()), 90)
        a = clip(" / ".join(s.strip() for s in self.after if s.strip()), 90)
        if self.kind == "added":
            return f'added "{a}"'
        if self.kind == "removed":
            return f'removed "{b}"'
        return f'"{b}" -> "{a}"'

    def where(self) -> str:
        return f"{self.file}:{self.line}" if self.line else self.file

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "file": self.file, "line": self.line, "kind": self.kind,
                "before": "".join(self.before), "after": "".join(self.after)}


def list_files(root: Path) -> List[str]:
    return [p.relative_to(root).as_posix() for p in receipt.list_artifacts(root, None)]


def _lines(p: Path) -> Optional[List[str]]:
    t = read_text(p)
    return None if t is None else t.splitlines(keepends=True)


def diff_dirs(draft: Path, final: Path) -> List[Edit]:
    d_files, f_files = set(list_files(draft)), set(list_files(final))
    edits: List[Edit] = []
    new = lambda **kw: edits.append(Edit(id=f"E{len(edits) + 1}", **kw))
    for f in sorted(d_files | f_files):
        if f not in d_files:
            new(file=f, kind="file_added")
        elif f not in f_files:
            new(file=f, kind="file_removed")
        elif (draft / f).read_bytes() != (final / f).read_bytes():
            a, b = _lines(draft / f), _lines(final / f)
            if a is None or b is None:
                new(file=f, kind="changed", trivial=False)
                continue
            for tag, a0, a1, b0, b1 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
                if tag == "equal":
                    continue
                for before, after, pos in _split(a[a0:a1], b[b0:b1], b0):
                    kind = "changed" if before and after else "added" if after else "removed"
                    new(file=f, kind=kind, line=min(pos + 1, max(len(b), 1)), before=before, after=after,
                        span=(pos, pos + len(after)),
                        trivial="".join(before).split() == "".join(after).split())
    return edits


def _split(before: List[str], after: List[str], pos: int) -> List[Tuple[List[str], List[str], int]]:
    """Split one replaced block into separate edits: a line rewritten in place is its own edit,
    and so is a run of lines that was only added or only removed. `pos` is the index in the final file."""
    best, bi, bj = 0.5, -1, -1
    for i, x in enumerate(before):
        for j, y in enumerate(after):
            if x.strip() and y.strip():
                r = difflib.SequenceMatcher(None, x, y, autojunk=False).ratio()
                if r > best:
                    best, bi, bj = r, i, j
    if bi < 0:
        if before and after and any(x.strip() for x in before) and any(y.strip() for y in after):
            return [(before, after, pos)]  # nothing lines up: one rewrite
        return [(before, after, pos)] if (before or after) else []
    return (_split(before[:bi], after[:bj], pos)
            + [([before[bi]], [after[bj]], pos + bj)]
            + _split(before[bi + 1:], after[bj + 1:], pos + bj + 1))


def revert(final: Path, draft: Path, edit: Edit, dest: Path) -> None:
    """Copy `final` to `dest` with exactly one edit undone."""
    shutil.copytree(final, dest, ignore=shutil.ignore_patterns(paths.EVAL_DIR))
    target = dest / edit.file
    if edit.kind == "file_added":
        target.unlink()
    elif edit.kind == "file_removed" or not (edit.before or edit.after):
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(draft / edit.file, target)
    else:
        lines = _lines(final / edit.file) or []
        b0, b1 = edit.span
        target.write_text("".join(lines[:b0] + edit.before + lines[b1:]), encoding="utf-8")


# ---------------------------------------------------------------- candidates

@dataclass
class Candidate:
    id: str
    kind: str                  # structure | format | relation | content
    scope: str                 # general | case-specific
    check: Dict[str, Any]      # type + params + hint, as it would appear in a config
    basis: str                 # the feature of the final output, or the edit, it comes from
    source: str = "feature"    # feature | edit
    edits: List[str] = field(default_factory=list)
    enabled: bool = True
    final: str = ""
    draft: str = ""
    alternatives: List[Dict[str, Any]] = field(default_factory=list)  # looser variants, tried in order
    from_edit: bool = False    # proposed from an edit: must fail on the draft or be discarded
    key: str = ""              # same feature of the same (globbed) file, whichever final it was seen in

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "scope": self.scope, "enabled": self.enabled,
                "source": self.source, "edits": self.edits, "basis": self.basis,
                "validation": {"final": self.final, "draft": self.draft}, "check": self.check}


def slug(s: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:n].strip("_") or "x"


def globify(rel: str) -> str:
    """report_2026-11-03.md -> report_[0-9][0-9]...: the date in a file name is not part of the rule."""
    return re.sub(r"\d", "[0-9]", _glob.escape(rel))


def run_check(check: Dict[str, Any], workdir: Path, inputs: Optional[Path], cid: str = "candidate") -> Result:
    ctype = check.get("type")
    if ctype not in REGISTRY:
        return Result(cid, str(ctype), ERROR, f"unknown check type '{ctype}'")
    spec = CheckSpec(id=cid, type=ctype, hint=check.get("hint"),
                     params={k: v for k, v in check.items() if k not in ("id", "type", "hint")})
    conf = Config(name="learn", path=paths.checks_dir() / "learn.yaml", sha256="", hashed_files=[], checks=[spec])
    try:
        REGISTRY[ctype].validate(spec, conf.path.parent, workdir)
        return REGISTRY[ctype].run(spec, Context(workdir=workdir, config=conf, inputs=inputs))
    except ConfigError as e:
        return Result(cid, ctype, ERROR, str(e), [str(e)])


def _induce(values: List[Any]) -> Dict[str, Any]:
    """Smallest schema (in evalctl's subset) that all values satisfy. Never records concrete values."""
    def tname(v: Any) -> str:
        if v is None:
            return "null"
        if isinstance(v, bool):
            return "boolean"
        if isinstance(v, (int, float)):
            return "number"
        if isinstance(v, str):
            return "string"
        return "array" if isinstance(v, list) else "object"

    names = sorted({tname(v) for v in values})
    if len(names) != 1:
        return {"type": names} if names else {}
    out: Dict[str, Any] = {"type": names[0]}
    if names[0] == "object":
        keys: List[str] = []
        for v in values:
            keys.extend(k for k in v if k not in keys)
        required = [k for k in keys if all(k in v for v in values)]
        if required:
            out["required"] = required
        out["properties"] = {k: _induce([v[k] for v in values if k in v]) for k in keys}
    elif names[0] == "array":
        items = [x for v in values for x in v]
        if items:
            out["items"] = _induce(items)
    elif names[0] == "string" and all(v.strip() for v in values):
        out["minLength"] = 1
        for f in ("date", "date-time", "uri", "email"):
            if all(_format_ok(v, f) for v in values):
                out["format"] = f
                break
    return out


def _schema_check(path: Any, pattern: str) -> Optional[Tuple[Dict[str, Any], str]]:
    """Schema that every given file satisfies. `path` is one file or, for several final outputs, a list."""
    files = path if isinstance(path, list) else [path]
    try:
        fmt = detect_format(files[0].name, None)
    except ConfigError:
        return None
    loaded = [load_records(f, f.name, fmt) for f in files]
    if any(l.problems for l in loaded):
        return None
    check: Dict[str, Any] = {"type": "schema", "file": pattern}
    names = files[0].name if len(files) == 1 else f"{len(files)} files matching {pattern}"
    if fmt == "json" and not any(l.is_record_list for l in loaded):
        check["schema"] = _induce([l.document for l in loaded])
        return check, f"field names, nesting and value types of {names}"
    per_file = [[r for _, r in l.records] for l in loaded]
    recs = [r for rs in per_file for r in rs]
    if not all(per_file) or not all(isinstance(r, dict) for r in recs):
        return None
    if fmt == "csv":
        columns = [list(l.columns or []) for l in loaded]
        if all(c == columns[0] for c in columns):
            check["columns"] = columns[0]
        for r in recs:  # cells are strings; type a column as number when every cell is one
            for k, v in list(r.items()):
                n = parse_number(v) if isinstance(v, str) and "," not in v else None
                if n is not None and all(parse_number(str(o.get(k, ""))) is not None for o in recs if k in o):
                    r[k] = float(n.value)
    check["record_schema"] = _induce(recs)
    check["min_records"] = 1
    unique = [k for k in recs[0] if ID_LIKE.search(str(k)) and all(
        len(rs) >= 2 and all(k in r and isinstance(r[k], (str, int)) for r in rs)
        and len({r[k] for r in rs}) == len(rs) for rs in per_file)]
    if unique:
        check["unique"] = unique
    return check, (f"columns and value types of the {len(recs)} record(s) in {names}"
                   + (f"; {', '.join(unique)} never repeat" if unique else ""))


def propose(final: Path, inputs: Optional[Path]) -> List[Candidate]:
    """Checks suggested by the final output alone and by how it relates to the inputs."""
    out: List[Candidate] = []
    input_tables = []
    if inputs is not None:
        for f in list_files(inputs):
            for t in tables_in(inputs / f):
                input_tables.append((f, t))

    for f in list_files(final):
        _propose_file(final, inputs, f, input_tables, out)
    return out


def _propose_file(final: Path, inputs: Optional[Path], f: str, input_tables: List[Any], out: List[Candidate]) -> None:
    first = len(out)
    path, pat, s = final / f, globify(f), slug(f)
    try:
        _propose_for(final, inputs, f, input_tables, out)
    finally:
        for c in out[first:]:
            c.key = f"{pat}|{c.id[len(s):]}"


def _propose_for(final: Path, inputs: Optional[Path], f: str, input_tables: List[Any], out: List[Candidate]) -> None:
    if True:
        path, pat, s = final / f, globify(f), slug(f)
        named = f"{f}" if pat == f else f"{f} (as {pat})"
        out.append(Candidate(f"{s}_exists", "structure", GENERAL,
                             {"type": "file", "path": pat, "min_size": 1,
                              "hint": f"The deliverable must include {pat}, and it must not be empty."},
                             f"the final output contains {named}"))
        text = read_text(path)
        if text is None:
            return
        is_records = path.suffix.lower() in RECORD_EXTS

        if is_records:
            sc = _schema_check(path, pat)
            if sc:
                out.append(Candidate(f"{s}_structure", "structure", GENERAL,
                                     dict(sc[0], hint="Keep the same fields and value types as the reference output."),
                                     sc[1]))
        else:
            heads = [ln.strip() for ln in text.splitlines()
                     if re.match(r"#{1,6} \S", ln) and not re.search(r"\d", ln)]
            heads = list(dict.fromkeys(heads))
            if heads:
                out.append(Candidate(f"{s}_sections", "structure", GENERAL,
                                     {"type": "contains", "file": pat, "must": heads,
                                      "hint": "The document needs each of these section headings."},
                                     f"section headings of {f}: " + ", ".join(heads)))
            out.append(Candidate(f"{s}_no_placeholders", "content", GENERAL,
                                 {"type": "regex", "file": pat, "must_not": PLACEHOLDERS,
                                  "hint": "Replace every placeholder with real content, or remove the line."},
                                 f"{f} has no TODO / TBD / placeholder text left"))
            n_iso = len(re.findall(DATE_ISO, text))
            if n_iso:
                out.append(Candidate(f"{s}_dates_iso", "format", GENERAL,
                                     {"type": "regex", "file": pat, "must_not": SLASH_DATES,
                                      "hint": "Write dates as YYYY-MM-DD."},
                                     f"all {n_iso} date(s) in {f} are written YYYY-MM-DD"))
            decimals = [n for n in extract_numbers(text) if n.decimals]
            if len(decimals) >= 3 and all(n.decimals == 2 for n in decimals):
                out.append(Candidate(f"{s}_two_decimals", "format", GENERAL,
                                     {"type": "regex", "file": pat, "must_not": [NOT_TWO_DECIMALS],
                                      "hint": "Write amounts with exactly two decimals."},
                                     f"all {len(decimals)} decimal numbers in {f} have exactly two decimals"))

        totals = [t for t in tables_in(path, text)
                  if any(r and re.search(DEFAULT_TOTAL_LABEL, r[0], re.I) for r in t.rows)]
        if totals:
            out.append(Candidate(f"{s}_totals_add_up", "relation", GENERAL,
                                 {"type": "table_sum", "file": pat,
                                  "hint": "Recompute the total row from the rows above it."},
                                 f"in {f}, the total row of each table equals the sum of the other rows"))
        if inputs is None:
            return
        nums = file_numbers(path, text)
        if nums:
            base = {"type": "numbers_in_inputs", "file": pat,
                    "hint": "Every figure must come from the input files, or be a sum or count of input rows. "
                            "Do not estimate or retype numbers from memory."}
            out.append(Candidate(f"{s}_numbers_from_inputs", "relation", GENERAL, base,
                                 f"each of the {len(nums)} number(s) in {f} appears in the inputs or is a "
                                 "sum / count of input rows",
                                 alternatives=[dict(base, ignore_small_integers=10)]))
        for src, t in input_tables:
            numeric = numeric_columns(t)
            for col in t.header:
                vals = sorted(set(v for v in t.column(col) if v)) if col and col not in numeric else []
                if 2 <= len(vals) <= 50 and all(len(v) >= 2 and v in text for v in vals):
                    out.append(Candidate(
                        f"{s}_covers_{slug(col, 20)}", "relation", GENERAL,
                        {"type": "covers_input_values", "file": pat, "input": globify(src), "column": col,
                         "hint": f"Every {col} that occurs in {src} must appear in the output."},
                        f"all {len(vals)} distinct value(s) of {col} in {src} appear in {f}"))


def _phrases(before: str, after: str) -> Tuple[List[str], List[str], bool]:
    """Word-level difference: (removed phrases, added phrases, a number changed)."""
    ta = list(re.finditer(r"\w+|[^\w\s]", before))
    tb = list(re.finditer(r"\w+|[^\w\s]", after))
    sm = difflib.SequenceMatcher(None, [m.group(0) for m in ta], [m.group(0) for m in tb], autojunk=False)
    removed, added, numeric = [], [], False

    def keep(text: str, toks: List["re.Match[str]"], i: int, j: int, into: List[str]) -> None:
        nonlocal numeric
        if i >= j:
            return
        phrase = " ".join(text[toks[i].start():toks[j - 1].end()].split()).strip(" .,;:!?*_|#>-")
        if re.search(r"\d", phrase):
            numeric = True
        elif len(phrase) >= 4 and re.search(r"[^\W\d_]", phrase) and len(phrase) <= 100:
            into.append(phrase)

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "equal":
            keep(before, ta, i1, i2, removed)
            keep(after, tb, j1, j2, added)
    top = lambda xs: sorted(dict.fromkeys(xs), key=len, reverse=True)[:2]
    return top(removed), top(added), numeric


def fallbacks(edit: Edit) -> List[Candidate]:
    """Literal checks for one edit that no general rule explains. Never contain numbers."""
    f, s = edit.file, slug(edit.file)
    if edit.kind == "file_removed":
        temp = f.lower().endswith(TEMP_SUFFIXES)
        pat = f"**/*{Path(f).suffix}" if temp and Path(f).suffix else f
        return [Candidate(f"{s}_absent", "structure", GENERAL if temp else SPECIFIC,
                          {"type": "file", "path": pat, "exists": False,
                           "hint": "Remove scratch files before delivering."},
                          f"{edit.id}: the human deleted {f}", from_edit=True, edits=[edit.id])]
    if edit.kind == "file_added" or edit.trivial or not (edit.before or edit.after):
        return []
    removed, added, edit.numeric = _phrases("".join(edit.before), "".join(edit.after))
    out = []
    for ph in removed:
        out.append(Candidate(f"{s}_no_{slug(ph, 24)}", "content", SPECIFIC,
                             {"type": "contains", "file": f, "must_not": [ph]},
                             f'{edit.id} ({edit.where()}): the human removed "{ph}"',
                             from_edit=True, edits=[edit.id]))
    for ph in added:
        out.append(Candidate(f"{s}_has_{slug(ph, 24)}", "content", SPECIFIC,
                             {"type": "contains", "file": f, "must": [ph]},
                             f'{edit.id} ({edit.where()}): the human added "{ph}"',
                             from_edit=True, edits=[edit.id]))
    return out


# ---------------------------------------------------------------- learn

def _why(r: Result) -> str:
    return clip(r.failures[0] if r.failures else r.summary, 160)


def _same(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    strip = lambda c: {k: v for k, v in c.items() if k not in ("id", "hint")}
    return strip(a) == strip(b)


def learn(inputs: Optional[Path], draft: Path, final: Path, name: str) -> Dict[str, Any]:
    edits = diff_dirs(draft, final)
    kept: List[Candidate] = []
    discarded: List[Dict[str, Any]] = []
    drop = lambda c, reason: discarded.append({"id": c.id, "reason": reason, "check": c.check})

    existing = _existing_checks(name)
    stale: List[Dict[str, str]] = []
    if existing:
        for c in existing:
            if c.get("type") in ("llm_rubric", "shell", "python", "action_policy"):
                continue  # not re-run here: they cost money, run arbitrary code or need a trajectory
            r = run_check(c, final, inputs, str(c.get("id", "?")))
            if r.status != PASS:
                stale.append({"id": str(c.get("id", "?")), "reason": _why(r)})

    def validate(c: Candidate) -> bool:
        if any(_same(c.check, e) for e in existing):
            drop(c, f"already in config {name}")
            return False
        r = run_check(c.check, final, inputs, c.id)
        for alt in c.alternatives:
            if r.status == PASS:
                break
            first, c.check = r, alt
            r = run_check(alt, final, inputs, c.id)
            if r.status == PASS:
                c.basis += f" (whole numbers up to {alt.get('ignore_small_integers')} are not checked: " \
                           f"the strict rule fails on the final output: {_why(first)})"
        if r.status != PASS:
            drop(c, ("fails on the final output: " if r.status == FAIL else "cannot run: ") + _why(r))
            return False
        c.final = PASS
        c.draft = run_check(c.check, draft, inputs, c.id).status
        if c.from_edit and c.draft != FAIL:
            drop(c, "taken from an edit but the draft does not fail it, so it does not capture the edit")
            return False
        return True

    for c in propose(final, inputs):
        if validate(c):
            kept.append(c)

    # Which edit does each draft-failing check capture? Undo one edit at a time and see.
    catching = [c for c in kept if c.draft == FAIL]
    with tempfile.TemporaryDirectory(prefix="evalctl-learn-") as tmp:
        for e in edits:
            if e.trivial or not catching:
                continue
            variant = Path(tmp) / e.id
            revert(final, draft, e, variant)
            for c in catching:
                if run_check(c.check, variant, inputs, c.id).status == FAIL:
                    c.edits.append(e.id)
    for c in catching:
        c.source = "edit"
        if not c.edits:
            c.basis += " (the draft fails this, but no single edit explains it on its own)"

    covered = {eid for c in kept for eid in c.edits}
    for e in edits:
        if e.id in covered:
            continue
        for c in fallbacks(e):
            c.source, c.enabled = "edit", c.scope == GENERAL
            while any(k.id == c.id for k in kept):
                c.id += "_2"
            if validate(c):
                kept.append(c)

    by_edit: Dict[str, List[str]] = {e.id: [c.id for c in kept if e.id in c.edits] for e in edits}
    unconverted = []
    for e in edits:
        general = any(c.scope == GENERAL for c in kept if e.id in c.edits)
        if e.trivial:
            reason = "whitespace only"
        elif not by_edit[e.id]:
            reason = ("it changes a specific number and no relation (sum, count, lookup in the inputs) "
                      "explains the new value" if e.numeric else "no checkable pattern found")
        elif e.numeric and not general:
            reason = ("the numeric part was not converted: no relation (sum, count, lookup in the inputs) "
                      "explains the new value")
        else:
            continue
        unconverted.append({"id": e.id, "reason": reason})

    return {
        "name": name,
        "learned_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "inputs": str(inputs) if inputs else None,
        "draft": str(draft),
        "final": str(final),
        "edits": [dict(e.to_dict(), where=e.where(), summary=e.describe(), checks=by_edit[e.id]) for e in edits],
        "candidates": [c.to_dict() for c in kept],
        "discarded": discarded,
        "unconverted_edits": unconverted,
        "existing_checks_failing_on_final": stale,
    }


# ---------------------------------------------------------------- several finals

def _existing_checks(name: str) -> List[Dict[str, Any]]:
    cpath = paths.checks_dir() / f"{name}.yaml"
    if not cpath.is_file():
        return []
    try:
        raw = yaml.safe_load(cpath.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return []
    return [c for c in (raw.get("checks") or []) if isinstance(c, dict)] if isinstance(raw, dict) else []


def _count_features(finals: List[Path]) -> List[Dict[str, Any]]:
    """Things that are a plain count in each final: records per file, files per numbered pattern."""
    feats: Dict[str, Dict[str, Any]] = {}
    for i, final in enumerate(finals):
        by_pattern: Dict[str, List[str]] = {}
        for f in list_files(final):
            by_pattern.setdefault(globify(f), []).append(f)
        for pat, files in by_pattern.items():
            if "[0-9]" in pat:
                ft = feats.setdefault(f"{pat}|_file_count", {
                    "id": f"{slug(pat.replace('[0-9]', 'n'))}_file_count", "what": f"number of files matching {pat}",
                    "check": lambda n, pat=pat: {"type": "file", "path": pat, "min_count": n, "max_count": n},
                    "values": [0] * len(finals)})
                ft["values"][i] = len(files)
            path = final / files[0]
            if len(files) == 1 and path.suffix.lower() in RECORD_EXTS:
                try:
                    loaded = load_records(path, path.name, detect_format(path.name, None))
                except ConfigError:
                    continue
                if loaded.problems or not loaded.is_record_list:
                    continue
                ft = feats.setdefault(f"{pat}|_record_count", {
                    "id": f"{slug(pat.replace('[0-9]', 'n'))}_record_count", "what": f"number of records in {pat}",
                    "check": lambda n, pat=pat: {"type": "schema", "file": pat, "min_records": n, "max_records": n},
                    "values": [None] * len(finals)})
                ft["values"][i] = len(loaded.records)
    return [f for f in feats.values() if all(v is not None for v in f["values"])]


def learn_finals(finals: List[Path], inputs: List[Optional[Path]], name: str) -> Dict[str, Any]:
    """Several corrected outputs of the same workflow, no drafts.

    A feature becomes a candidate rule only when it holds in every final. What differs between
    the finals is case-specific: it is listed, and no check is proposed for it. An exact count
    that happens to be the same everywhere is kept as a case-specific candidate, disabled.
    """
    n = len(finals)
    existing = _existing_checks(name)
    groups: Dict[str, List[Tuple[int, Candidate]]] = {}
    for i, (final, inp) in enumerate(zip(finals, inputs)):
        for c in propose(final, inp):
            groups.setdefault(c.key, []).append((i, c))

    def holds(check: Dict[str, Any], cid: str) -> List[Result]:
        return [run_check(check, f, inp, cid) for f, inp in zip(finals, inputs)]

    kept: List[Candidate] = []
    discarded: List[Dict[str, Any]] = []
    inconsistent: List[Dict[str, Any]] = []
    for key, group in groups.items():
        c = group[0][1]
        seen = sorted({i for i, _ in group})
        if any(_same(c.check, e) for e in existing):
            discarded.append({"id": c.id, "reason": f"already in config {name}", "check": c.check})
            continue
        variants = [(c.check, "")]
        pat, suffix = key.split("|", 1)
        if suffix == "_structure" and len(seen) == n:
            merged = _schema_check([sorted(f.glob(pat))[0] for f in finals if sorted(f.glob(pat))], pat)
            if merged:
                variants.append((dict(merged[0], hint=c.check.get("hint")), merged[1]))
        if suffix == "_sections" and len(seen) == n:
            common = [h for h in c.check["must"] if all(h in o.check["must"] for _, o in group)]
            for i, o in group:
                for h in o.check["must"]:
                    if h not in common and not any(x["id"] == f"{c.id}:{h}" for x in inconsistent):
                        has = [j + 1 for j, o2 in group if h in o2.check["must"]]
                        inconsistent.append({
                            "id": f"{c.id}:{h}", "kind": "structure", "scope": SPECIFIC,
                            "feature": f"section heading \"{h}\" in {pat}", "holds_in": has,
                            "fails_in": [j + 1 for j in range(n) if j + 1 not in has],
                            "reason": "not in every final output, so it is not required"})
            if common:
                variants.append((dict(c.check, must=common), "section headings common to all final outputs: " + ", ".join(common)))
        variants += [(alt, c.basis + f" (whole numbers up to {alt.get('ignore_small_integers')} are not checked)")
                     for alt in c.alternatives]
        results: List[Result] = []
        for check, basis in variants:
            results = holds(check, c.id)
            if all(r.status == PASS for r in results):
                c.check, c.basis = check, (basis or c.basis)
                break
        if all(r.status == PASS for r in results):
            c.basis += (f" (holds in all {n} final outputs)" if len(seen) == n
                        else f" (seen in final {', '.join(str(i + 1) for i in seen)}; the other final outputs do not break it)")
            c.final, c.draft, c.source = PASS, "n/a", "feature"
            kept.append(c)
            continue
        results = holds(c.check, c.id)
        fails = [i for i, r in enumerate(results) if r.status != PASS]
        inconsistent.append({
            "id": c.id, "kind": c.kind, "scope": SPECIFIC, "feature": c.basis,
            "holds_in": [i + 1 for i in range(n) if i not in fails], "fails_in": [i + 1 for i in fails],
            "reason": f"final {fails[0] + 1}: {_why(results[fails[0]])}"})

    for ft in _count_features(finals):
        values = ft["values"]
        if len(set(values)) > 1:
            inconsistent.append({
                "id": ft["id"], "kind": "structure", "scope": SPECIFIC, "feature": ft["what"],
                "holds_in": [], "fails_in": [],
                "reason": "differs between the final outputs (" + ", ".join(str(v) for v in values) + "), so it is not a rule"})
            continue
        check = ft["check"](values[0])
        if all(r.status == PASS for r in holds(check, ft["id"])) and not any(_same(check, e) for e in existing):
            kept.append(Candidate(
                ft["id"], "structure", SPECIFIC, check,
                f"{ft['what']} is {values[0]} in all {n} final outputs. An exact count is treated as case-specific: "
                "enable it only if that number is a requirement of the task", enabled=False, final=PASS, draft="n/a"))

    stale = []
    for e in existing:
        if e.get("type") in ("llm_rubric", "shell", "python", "action_policy"):
            continue
        for i, r in enumerate(holds(e, str(e.get("id", "?")))):
            if r.status != PASS:
                stale.append({"id": str(e.get("id", "?")), "reason": f"final {i + 1}: {_why(r)}"})
                break
    return {
        "name": name, "mode": "finals",
        "learned_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "inputs": str(inputs[0]) if inputs[0] else None,
        "inputs_all": [str(i) if i else None for i in inputs],
        "draft": "", "final": str(finals[0]), "finals": [str(f) for f in finals],
        "edits": [], "candidates": [c.to_dict() for c in kept], "discarded": discarded,
        "inconsistent": inconsistent, "unconverted_edits": [],
        "existing_checks_failing_on_final": stale,
    }


def preview_config(doc: Dict[str, Any]) -> Path:
    """The config `review --yes` would write right now, as a file lint can read. Nothing is activated."""
    name = check_name(doc["name"])
    top: Dict[str, Any] = {}
    old: List[Dict[str, Any]] = []
    cpath = paths.checks_dir() / f"{name}.yaml"
    if cpath.is_file():
        raw = yaml.safe_load(cpath.read_text(encoding="utf-8")) or {}
        if isinstance(raw, dict):
            old = [c for c in (raw.pop("checks", None) or []) if isinstance(c, dict)]
            top = raw
    enabled = [c for c in doc["candidates"] if c["enabled"]]
    owned = {c["id"] for c in doc["candidates"]}
    top["name"] = name
    top.pop("reference", None)
    top["checks"] = [c for c in old if str(c.get("id")) not in owned] + [
        dict({"id": c["id"], "type": c["check"]["type"]}, **{k: v for k, v in c["check"].items() if k not in ("id", "type")})
        for c in enabled]
    p = learned_path(name).with_name(f"{name}.preview.yaml")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(top, sort_keys=False, allow_unicode=True, width=110), encoding="utf-8")
    return p


def save(doc: Dict[str, Any]) -> Path:
    p = learned_path(doc["name"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=110), encoding="utf-8")
    return p


def load(name: str) -> Dict[str, Any]:
    p = learned_path(check_name(name))
    if not p.is_file():
        raise ConfigError(f"nothing learned under the name '{name}' ({p} does not exist).\n"
                          f"Run: evalctl learn --inputs DIR --draft DIR --final DIR --name {name}")
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"{p}: invalid YAML: {e}")
    if not isinstance(doc, dict) or not isinstance(doc.get("candidates"), list):
        raise ConfigError(f"{p}: not a file written by `evalctl learn`")
    return doc


# ---------------------------------------------------------------- text

def describe_check(check: Dict[str, Any]) -> str:
    body = {k: v for k, v in check.items() if k != "hint"}
    return yaml.safe_dump(body, sort_keys=False, allow_unicode=True, width=100, default_flow_style=None).rstrip()


def candidate_lines(c: Dict[str, Any], indent: str = "  ") -> List[str]:
    src = ("from edit " + ", ".join(c["edits"]) if c.get("edits") else "from an edit") \
        if c["source"] == "edit" else "from your judgement of two outputs" if c["source"] == "judgment" \
        else "from a feature of the final output"
    v = c.get("validation") or {}
    lines = [f"{'[on ]' if c['enabled'] else '[off]'} {c['id']}",
             f"{indent}type: {c['kind']}   scope: {SCOPE_LABEL.get(c['scope'], c['scope'])}   {src}",
             f"{indent}basis: {c['basis']}",
             (f"{indent}validated: the output you preferred {v.get('final') or 'not graded'}, the other {v.get('draft') or 'not graded'}"
              if c["source"] == "judgment" else
              f"{indent}validated: passes every final output" if v.get("draft") == "n/a" and v.get("final") == PASS
              else f"{indent}validated: final {v.get('final') or '?'}, draft {v.get('draft') or '?'}"),
             f"{indent}check:"]
    lines += [f"{indent}  {ln}" for ln in describe_check(c["check"]).splitlines()]
    return lines


def render(doc: Dict[str, Any], saved: Optional[Path] = None) -> str:
    cands, edits = doc["candidates"], doc["edits"]
    why = {u["id"]: u["reason"] for u in doc["unconverted_edits"]}
    scope = {c["id"]: c["scope"] for c in cands}
    multi = doc.get("mode") == "finals"
    if multi:
        out = [f"evalctl learn: {doc['name']}  (from {len(doc['finals'])} final outputs)"]
        for i, f in enumerate(doc["finals"]):
            inp = (doc.get("inputs_all") or [None] * len(doc["finals"]))[i]
            out.append(f"final {i + 1}: {f}" + (f"   inputs: {inp}" if inp else ""))
    else:
        out = [f"evalctl learn: {doc['name']}",
               f"inputs: {doc['inputs'] or '(none)'}", f"draft:  {doc['draft']}", f"final:  {doc['final']}", "",
               f"Human edits ({len(edits)})"]
    for e in edits:
        out.append(f"  {e['id']}  {e['where']}  {e['summary']}")
        if e["checks"]:
            out.append("      -> " + ", ".join(
                cid + ("" if scope.get(cid) == GENERAL else " (case-specific)") for cid in e["checks"]))
        if e["id"] in why:
            out.append(f"      {'note' if e['checks'] else 'NOT CONVERTED'}: {why[e['id']]}")
    if not edits and not multi:
        out.append("  (draft and final are identical)")
    on = sum(c["enabled"] for c in cands)
    out += ["", f"Candidate checks ({len(cands)}: {on} enabled, {len(cands) - on} disabled)"]
    for c in cands:
        out += ["  " + ln for ln in candidate_lines(c)] + [""]
    if doc.get("inconsistent"):
        out.append(f"Not consistent across the final outputs ({len(doc['inconsistent'])}): case-specific, no check proposed")
        for x in doc["inconsistent"]:
            where = f" [holds in final {', '.join(map(str, x['holds_in']))}]" if x.get("holds_in") else ""
            out.append(f"  - {x['id']}: {x['feature']}{where}")
            out.append(f"      {x['reason']}")
        out.append("")
    if doc["discarded"]:
        out.append(f"Discarded ({len(doc['discarded'])})")
        out += [f"  - {d['id']}: {d['reason']}" for d in doc["discarded"]]
        out.append("")
    if doc.get("existing_checks_failing_on_final"):
        out.append(f"Checks already in config {doc['name']} that FAIL on this final output (relax or delete them):")
        out += [f"  - {d['id']}: {d['reason']}" for d in doc["existing_checks_failing_on_final"]]
        out.append("")
    if saved:
        out += [f"Candidates saved to {saved}. Nothing is active yet.",
                f"Next: evalctl review {doc['name']}"]
    return "\n".join(out)


# ---------------------------------------------------------------- review -> config

def revalidate(doc: Dict[str, Any], c: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Re-run one candidate on the example it was learned from, if that still exists."""
    if c.get("example"):  # a rubric made from a pairwise judgement: its own pair of outputs
        good, bad = Path(c["example"]["good"]), Path(c["example"]["bad"])
        if not good.is_dir():
            return None, None
        rg = run_check(c["check"], good, None, c["id"])
        rb = run_check(c["check"], bad, None, c["id"]) if bad.is_dir() else None
        c["validation"] = {"final": rg.status, "draft": rb.status if rb else ""}
        return rg.status, (None if rg.status == PASS else _why(rg))
    if doc.get("mode") == "finals":
        finals = [Path(f) for f in doc["finals"]]
        ins = [Path(i) if i else None for i in (doc.get("inputs_all") or [None] * len(finals))]
        if not all(f.is_dir() for f in finals) or not all(i is None or i.is_dir() for i in ins):
            return None, None
        for f, i in zip(finals, ins):
            r = run_check(c["check"], f, i, c["id"])
            if r.status != PASS:
                c["validation"] = {"final": r.status, "draft": "n/a"}
                return r.status, f"{f}: {_why(r)}"
        c["validation"] = {"final": PASS, "draft": "n/a"}
        return PASS, None
    final, draft = Path(doc["final"]), Path(doc["draft"])
    inputs = Path(doc["inputs"]) if doc.get("inputs") else None
    if not final.is_dir() or (inputs is not None and not inputs.is_dir()):
        return None, None
    rf = run_check(c["check"], final, inputs, c["id"])
    rd = run_check(c["check"], draft, inputs, c["id"]) if draft.is_dir() else None
    c["validation"] = {"final": rf.status, "draft": rd.status if rd else ""}
    problem = None if rf.status == PASS else _why(rf)
    return rf.status, problem


def write_config(doc: Dict[str, Any]) -> Tuple[Path, int, int, Optional[Path]]:
    """Write the enabled candidates to ~/.evalctl/checks/<name>.yaml.

    An existing config keeps its other settings and its hand-written checks; checks
    this file wrote earlier are replaced (or removed, when now disabled).
    -> (path, checks written from candidates, checks kept from before, backup path)
    """
    from . import config as cfg

    name = check_name(doc["name"])
    path = paths.checks_dir() / f"{name}.yaml"
    top: Dict[str, Any] = {}
    old: List[Dict[str, Any]] = []
    if path.is_file():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ConfigError(f"{path}: top level must be a mapping")
        old = [c for c in (raw.pop("checks", None) or []) if isinstance(c, dict)]
        top = raw
    enabled = [c for c in doc["candidates"] if c["enabled"]]
    owned = {c["id"] for c in enabled} | {c["id"] for c in doc["candidates"] if c.get("in_config")}
    kept = [c for c in old if str(c.get("id")) not in owned]
    if not enabled and not kept:
        raise ConfigError("no check is enabled, so there is nothing to write")
    top.setdefault("name", name)
    # A known-good output for `evalctl lint`: every check must pass on it.
    top["reference"] = {"final": doc["final"], **({"inputs": doc["inputs"]} if doc.get("inputs") else {})}

    dump = lambda v: yaml.safe_dump(v, sort_keys=False, allow_unicode=True, width=110)
    parts = [f"# Written by `evalctl review {name}` on {datetime.now():%Y-%m-%d %H:%M}.",
             (f"# Learned from {len(doc['finals'])} final outputs: {', '.join(doc['finals'])}" if doc.get("mode") == "finals"
              else f"# Learned from: draft {doc['draft']} -> final {doc['final']}"),
             dump(top).rstrip(), "", "checks:"]
    block = lambda c: ["  " + ln for ln in dump([c]).rstrip().splitlines()]
    for c in kept:
        parts += block(c) + [""]
    for c in enabled:
        src = "edit " + ", ".join(c["edits"]) if c.get("edits") else c["source"]
        parts.append(f"  # {c['kind']} | {SCOPE_LABEL.get(c['scope'], c['scope'])} | from {src}")
        parts += [f"  # basis: {ln}" for ln in str(c["basis"]).splitlines()]
        body = {"id": c["id"], "type": c["check"]["type"]}
        body.update({k: v for k, v in c["check"].items() if k not in ("id", "type")})
        parts += block(body) + [""]

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".new")
    tmp.write_text("\n".join(parts), encoding="utf-8")
    try:
        with tempfile.TemporaryDirectory() as scratch:
            cfg.load(tmp, Path(scratch))
    except ConfigError as e:
        tmp.unlink()
        raise ConfigError(f"the resulting config would not load, nothing was written:\n{e}")
    backup = None
    if path.is_file():
        backup = path.with_name(path.name + ".bak")
        shutil.copyfile(path, backup)
    tmp.replace(path)
    for c in doc["candidates"]:
        c["in_config"] = bool(c["enabled"])
    save(doc)
    return path, len(enabled), len(kept), backup
