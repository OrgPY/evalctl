from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from ..config import CheckSpec, ConfigError
from .base import MAX_LISTED, Check, Context, Result, as_list, match_files, rel, result
from .records import detect_format, load_records, validate_schema_def, validate_value


class SchemaCheck(Check):
    """Structure of a JSON / JSONL / CSV file.

    record_schema  schema for each record (JSONL line, CSV row, element of a top-level JSON array)
    schema         JSON: schema for the whole document. JSONL/CSV: alias of record_schema.
    columns        CSV: header columns that must exist
    min_records / max_records, unique: [field, ...]
    """

    type = "schema"
    allowed = ("file", "format", "schema", "record_schema", "columns", "min_records",
               "max_records", "unique")
    required = ("file",)

    @classmethod
    def validate_extra(cls, spec: CheckSpec, config_dir: Path, workdir: Path) -> List[Path]:
        p = spec.params
        fmt = detect_format(p["file"], p.get("format"))
        for key in ("schema", "record_schema"):
            if p.get(key) is not None:
                validate_schema_def(p[key], key)
        if fmt != "json" and p.get("schema") is not None and p.get("record_schema") is not None:
            raise ConfigError(f"for {fmt}, use either `schema` or `record_schema`, not both")
        if p.get("columns") is not None and fmt != "csv":
            raise ConfigError("`columns` only applies to csv")
        for key in ("min_records", "max_records"):
            if p.get(key) is not None and (not isinstance(p[key], int) or p[key] < 0):
                raise ConfigError(f"`{key}` must be a non-negative integer")
        if not any(p.get(k) is not None for k in
                   ("schema", "record_schema", "columns", "min_records", "max_records", "unique")):
            raise ConfigError("schema check has nothing to verify; add record_schema, unique, min_records, ...")
        return []

    @classmethod
    def run(cls, spec: CheckSpec, ctx: Context) -> Result:
        p = spec.params
        fmt = detect_format(p["file"], p.get("format"))
        files = match_files(ctx.workdir, p["file"])
        if not files:
            return result(spec, [f"{p['file']}: file not found (expected a {fmt} file here)"], "",
                          "file missing")
        doc_schema = p.get("schema") if fmt == "json" else None
        rec_schema = p.get("record_schema") or (p.get("schema") if fmt != "json" else None)
        unique = [str(u) for u in as_list(p.get("unique"))]
        failures: List[str] = []
        total = 0
        for f in files:
            label = rel(ctx.workdir, f)
            loaded = load_records(f, label, fmt, rec_schema)
            failures.extend(loaded.problems)
            if fmt == "json" and loaded.document is None and loaded.problems:
                continue
            if doc_schema is not None:
                failures.extend(f"{label}: {m}" for m in validate_value(loaded.document, doc_schema))
            needs_list = rec_schema is not None or unique or p.get("min_records") is not None \
                or p.get("max_records") is not None
            if fmt == "json" and needs_list and not loaded.is_record_list:
                failures.append(f"{label}: top level must be a JSON array of records")
                continue
            for col in as_list(p.get("columns")):
                if col not in (loaded.columns or []):
                    failures.append(f"{label}:1: header is missing column '{col}'")
            n = len(loaded.records)
            total += n
            if rec_schema is not None:
                for loc, rec in loaded.records:
                    failures.extend(f"{label}{loc}: {m}" for m in validate_value(rec, rec_schema))
            for fld in unique:
                first: Dict[str, str] = {}
                for loc, rec in loaded.records:
                    if not isinstance(rec, dict) or rec.get(fld) in (None, ""):
                        continue
                    key = json.dumps(rec[fld], ensure_ascii=False, sort_keys=True)
                    if key in first:
                        failures.append(
                            f"{label}{loc}: duplicate {fld} {key} (first seen at {label}{first[key]}); "
                            "keep one, remove or replace the other"
                        )
                    else:
                        first[key] = loc
            lo, hi = p.get("min_records"), p.get("max_records")
            if lo is not None and n < lo:
                failures.append(f"{label}: has {n} record(s), need at least {lo} ({lo - n} more)")
            if hi is not None and n > hi:
                failures.append(f"{label}: has {n} record(s), at most {hi} allowed ({n - hi} too many)")
        count = len(failures)
        if count > MAX_LISTED:
            failures = failures[:MAX_LISTED] + [f"... and {count - MAX_LISTED} more problem(s) of the same kinds"]
        return result(spec, failures, f"{total} record(s) OK", f"{count} problem(s)")
