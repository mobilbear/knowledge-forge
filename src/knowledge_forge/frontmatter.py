"""Frontmatter handling built on PyYAML (safe loader) — replaces the fragile
regex parser from v1.0. Round-trips notes as (meta: dict, body: str) and
validates meta against JSON Schemas in schemas/.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Tuple

import yaml

from .config import DATA_ROOT

try:
    import jsonschema
except ImportError:  # pragma: no cover
    jsonschema = None

SCHEMA_DIR = DATA_ROOT / "schemas"
_FM_DELIM = "---"


class FrontmatterError(ValueError):
    pass


def parse(text: str) -> Tuple[dict, str]:
    """Return (meta, body). Files without frontmatter → ({}, text).
    Malformed YAML raises FrontmatterError (callers may quarantine)."""
    if not text.startswith(_FM_DELIM):
        return {}, text
    # find the closing delimiter on its own line
    lines = text.splitlines(keepends=True)
    end = None
    for i, ln in enumerate(lines[1:], start=1):
        if ln.strip() == _FM_DELIM:
            end = i
            break
    if end is None:
        return {}, text
    fm_text = "".join(lines[1:end])
    body = "".join(lines[end + 1:]).lstrip("\n")
    try:
        meta = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as e:
        raise FrontmatterError(f"invalid YAML frontmatter: {e}") from e
    if not isinstance(meta, dict):
        raise FrontmatterError("frontmatter did not parse to a mapping")
    return _stringify_dates(meta), body


def _stringify_dates(meta: dict) -> dict:
    out = {}
    for k, v in meta.items():
        if isinstance(v, (dt.date, dt.datetime)):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


# canonical field order for readable diffs (superset of v1.0 order)
_ORDER = [
    "id", "note_type", "type", "title", "description", "tags", "lang", "status",
    "entity_kind", "aliases", "source_url", "url", "canonical_url",
    "source_platform", "platform", "authors", "author", "published_at", "published",
    "captured_at", "capture_status", "capture_method", "content_completeness",
    "needs_retry", "missing_elements", "quality_reasons", "content_hash",
    "primary_category_id", "secondary_category_ids", "images", "sources",
    "model_profile", "prompt_version", "run_id", "schema_version",
    "created", "updated",
]


def dump(meta: dict, body: str, *, preserve_body: bool = False) -> str:
    keys = [k for k in _ORDER if k in meta] + [k for k in meta if k not in _ORDER]
    ordered = {k: meta[k] for k in keys}
    fm = yaml.safe_dump(
        ordered, allow_unicode=True, sort_keys=False, default_flow_style=None, width=1000
    ).rstrip("\n")
    content = body if preserve_body else body.strip() + "\n"
    return f"{_FM_DELIM}\n{fm}\n{_FM_DELIM}\n\n{content}"


def read_note(path: Path) -> Tuple[dict, str]:
    return parse(path.read_text(encoding="utf-8"))


# ---- schema validation ---------------------------------------------------------------
_schema_cache: dict[str, dict] = {}


def load_schema(name: str) -> dict:
    if name not in _schema_cache:
        p = SCHEMA_DIR / f"{name}.schema.json"
        _schema_cache[name] = json.loads(p.read_text(encoding="utf-8"))
    return _schema_cache[name]


def validate(instance: dict, schema_name: str) -> list[str]:
    """Return a list of human-readable validation errors (empty = valid)."""
    if jsonschema is None:  # pragma: no cover
        return []
    schema = load_schema(schema_name)
    validator = jsonschema.Draft202012Validator(schema)
    return [
        f"{'/'.join(str(x) for x in e.absolute_path) or '<root>'}: {e.message}"
        for e in validator.iter_errors(instance)
    ]
