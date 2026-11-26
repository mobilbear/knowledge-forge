"""Turn any raw adapter output into a schema-validated capture record with a
deterministic stable ID. This is the identity boundary: everything downstream
keys on record['id'].
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from .. import frontmatter as fm
from .. import ids
from ..capture.adapters import guess_lang
from ..urlnorm import canonicalize


def build_record(raw: dict, source_url: Optional[str], run: str) -> dict:
    text = raw.get("text") or ""
    canonical = canonicalize(raw.get("resolved_url") or source_url) if source_url else None
    content_hash = ids.content_sha256(text or (raw.get("ocr_text") or "") or (source_url or raw.get("title") or "local"))
    rec = {
        "id": ids.source_id(canonical, content_hash),
        "source_url": source_url,
        "canonical_url": canonical,
        "canonical_url_or_local": canonical or content_hash,
        "platform": raw.get("platform") or "web",
        "title": (raw.get("title") or "untitled").strip(),
        "authors": raw.get("authors") or [],
        "published_at": raw.get("published_at"),
        "captured_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "lang": raw.get("lang") or guess_lang(text),
        "text": text,
        "ocr_text": raw.get("ocr_text"),
        "images": raw.get("images") or [],
        "attachments": raw.get("attachments") or [],
        "body_verified": bool(raw.get("body_verified")),
        "partial_reason": raw.get("partial_reason"),
        "transcript_origin": raw.get("transcript_origin"),
        "content_kind": raw.get("content_kind") or (
            "transcript" if raw.get("capture_method") in ("transcript", "whisper") else
            "image post" if raw.get("ocr_text") else
            "abstract" if raw.get("platform") == "arxiv" else "article"),
        "content_hash": content_hash,
        "capture_method": raw.get("capture_method") or "none",
        "capture_status": "failed",       # provisional until quality gate runs
        "content_completeness": 0.0,
        "needs_retry": False,
        "missing_elements": [],
        "quality_reasons": [],
        "state": "received",
        "via": raw.get("via"),
        "run_id": run,
    }
    if raw.get("adapter_error"):
        rec["adapter_error"] = raw["adapter_error"]
    errors = fm.validate({k: v for k, v in rec.items() if k != "adapter_error"},
                         "capture_record")
    if errors:
        raise ValueError(f"capture record failed schema validation: {errors}")
    return rec
