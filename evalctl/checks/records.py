"""Load JSON / JSONL / CSV files as located records, and a small JSON-Schema subset validator.

The validator is deliberately small (no extra dependency). Unsupported
keywords are rejected when the config loads, so a rule is never silently
ignored.
"""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from ..config import ConfigError

FORMATS = ("json", "jsonl", "csv")
_EXT = {".json": "json", ".jsonl": "jsonl", ".ndjson": "jsonl", ".csv": "csv"}

SUPPORTED_KEYWORDS = {
    "type", "required", "properties", "additionalProperties", "items", "enum", "const",
    "minLength", "maxLength", "pattern", "minimum", "maximum", "minItems", "maxItems", "format",
    "description", "title",
}
SUPPORTED_TYPES = {"string", "number", "integer", "boolean", "object", "array", "null"}
SUPPORTED_FORMATS = {"uri", "date-time", "date", "email"}


def detect_format(path_pattern: str, explicit: Optional[str]) -> str:
    if explicit:
        if explicit not in FORMATS:
            raise ConfigError(f"format must be one of {', '.join(FORMATS)}, got '{explicit}'")
        return explicit
    fmt = _EXT.get(Path(path_pattern).suffix.lower())
    if not fmt:
        raise ConfigError(f"cannot infer format from '{path_pattern}'; set `format` to json, jsonl or csv")
    return fmt


def validate_schema_def(schema: Any, where: str = "schema") -> None:
    if not isinstance(schema, dict):
        raise ConfigError(f"{where}: must be a mapping")
    bad = set(schema) - SUPPORTED_KEYWORDS
    if bad:
        raise ConfigError(
            f"{where}: unsupported keyword(s) {', '.join(sorted(bad))} "
            f"(supported: {', '.join(sorted(SUPPORTED_KEYWORDS))})"
        )
    types = schema.get("type")
    for t in ([types] if isinstance(types, str) else types or []):
        if t not in SUPPORTED_TYPES:
            raise ConfigError(f"{where}: unsupported type '{t}'")
    if "format" in schema and schema["format"] not in SUPPORTED_FORMATS:
        raise ConfigError(f"{where}: unsupported format '{schema['format']}'")
    if "pattern" in schema:
        try:
            re.compile(schema["pattern"])
        except re.error as e:
            raise ConfigError(f"{where}: bad pattern: {e}")
    for name, sub in (schema.get("properties") or {}).items():
        validate_schema_def(sub, f"{where}.properties.{name}")
    if isinstance(schema.get("items"), dict):
        validate_schema_def(schema["items"], f"{where}.items")


def _type_ok(value: Any, t: str) -> bool:
    if t == "string":
        return isinstance(value, str)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "object":
        return isinstance(value, dict)
    if t == "array":
        return isinstance(value, list)
    if t == "null":
        return value is None
    return False


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _format_ok(value: str, fmt: str) -> bool:
    if fmt == "uri":
        u = urlparse(value)
        return bool(u.scheme and u.netloc) and " " not in value
    if fmt == "date-time":
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
            return "T" in value.upper() or " " in value
        except ValueError:
            return False
    if fmt == "date":
        try:
            date.fromisoformat(value)
            return True
        except ValueError:
            return False
    if fmt == "email":
        return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value))
    return True


def _short(v: Any) -> str:
    s = json.dumps(v, ensure_ascii=False)
    return s if len(s) <= 60 else s[:59] + "…"


def validate_value(value: Any, schema: Dict[str, Any], path: str = "") -> List[str]:
    """Return human-readable problems; `path` is the field path ('' for the record itself)."""
    errs: List[str] = []
    name = f"field '{path}'" if path else "record"
    types = schema.get("type")
    if types is not None:
        tl = [types] if isinstance(types, str) else list(types)
        if not any(_type_ok(value, t) for t in tl):
            errs.append(f"{name} must be {' or '.join(tl)}, got {_type_name(value)} {_short(value)}")
            return errs
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{name} must be one of {_short(schema['enum'])}, got {_short(value)}")
    if "const" in schema and value != schema["const"]:
        errs.append(f"{name} must equal {_short(schema['const'])}, got {_short(value)}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errs.append(
                f"{name} is empty" if not value.strip() and schema["minLength"] >= 1
                else f"{name} is too short ({len(value)} chars, minimum {schema['minLength']})"
            )
        elif "minLength" in schema and schema["minLength"] >= 1 and not value.strip():
            errs.append(f"{name} is only whitespace")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errs.append(f"{name} is too long ({len(value)} chars, maximum {schema['maxLength']})")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errs.append(f"{name} does not match pattern /{schema['pattern']}/: {_short(value)}")
        if "format" in schema and value and not _format_ok(value, schema["format"]):
            errs.append(f"{name} is not a valid {schema['format']}: {_short(value)}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errs.append(f"{name} is {value}, minimum is {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errs.append(f"{name} is {value}, maximum is {schema['maximum']}")
    if isinstance(value, dict):
        for req in schema.get("required") or []:
            if req not in value:
                p = f"{path}.{req}" if path else req
                errs.append(f"missing required field '{p}'")
        props = schema.get("properties") or {}
        for k, sub in props.items():
            if k in value:
                errs.extend(validate_value(value[k], sub, f"{path}.{k}" if path else k))
        if schema.get("additionalProperties") is False:
            for k in value:
                if k not in props:
                    errs.append(f"unexpected field '{path + '.' if path else ''}{k}'")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errs.append(f"{name} has {len(value)} item(s), minimum is {schema['minItems']}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errs.append(f"{name} has {len(value)} item(s), maximum is {schema['maxItems']}")
        if isinstance(schema.get("items"), dict):
            for i, item in enumerate(value):
                errs.extend(validate_value(item, schema["items"], f"{path}[{i}]"))
    return errs


def _coerce_csv(row: Dict[str, Any], schema: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """CSV cells are strings: drop empty cells and convert cells the schema declares numeric/boolean."""
    props = (schema or {}).get("properties") or {}
    out: Dict[str, Any] = {}
    for k, v in row.items():
        if k is None or v is None or v == "":
            continue
        t = props.get(k, {}).get("type")
        tl = [t] if isinstance(t, str) else list(t or [])
        conv: Any = v
        try:
            if "integer" in tl:
                conv = int(v)
            elif "number" in tl:
                conv = float(v)
            elif "boolean" in tl and v.lower() in ("true", "false"):
                conv = v.lower() == "true"
        except ValueError:
            conv = v
        out[k] = conv
    return out


class Loaded:
    def __init__(self) -> None:
        self.records: List[Tuple[str, Any]] = []  # (location suffix e.g. ":12", value)
        self.problems: List[str] = []             # parse-level problems, already located
        self.document: Any = None                 # whole JSON document (json only)
        self.is_record_list = True
        self.columns: Optional[List[str]] = None


def load_records(path: Path, label: str, fmt: str, schema: Optional[Dict[str, Any]] = None) -> Loaded:
    out = Loaded()
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as e:
        out.problems.append(f"{label}: not valid UTF-8 ({e})")
        return out
    if fmt == "jsonl":
        for n, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                out.records.append((f":{n}", json.loads(line)))
            except json.JSONDecodeError as e:
                out.problems.append(f"{label}:{n}: line is not valid JSON ({e.msg} at column {e.colno})")
    elif fmt == "json":
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as e:
            out.problems.append(f"{label}:{e.lineno}: not valid JSON ({e.msg} at column {e.colno})")
            out.is_record_list = False
            return out
        out.document = doc
        if isinstance(doc, list):
            out.records = [(f": $[{i}]", v) for i, v in enumerate(doc)]
        else:
            out.is_record_list = False
    elif fmt == "csv":
        reader = csv.DictReader(io.StringIO(text, newline=""))
        out.columns = list(reader.fieldnames or [])
        for row in reader:
            if None in row:
                out.problems.append(f"{label}:{reader.line_num}: row has more cells than the header")
            out.records.append((f":{reader.line_num}", _coerce_csv(row, schema)))
    return out
