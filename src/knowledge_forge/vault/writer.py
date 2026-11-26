"""Deterministic note construction and idempotent source filing.

Rules enforced here (not by any model):
- A source note's identity is its stable ID (from canonical URL / content hash).
- Re-sharing the same URL with identical content → noop (idempotent).
- Same URL, changed content → a new *version section* is appended to the same
  note with the new hash; the original capture text is never rewritten.
- No silent overwrite of any existing file with different content: staging +
  transaction diff makes every change explicit.
- Quarantined material (metadata_only / failed / retry_required) is written to
  80_queues/, never to 10_sources/ or the taxonomy tree.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Optional

from .. import frontmatter as fm
from .. import ids
from ..config import KFConfig
from ..discovery import VaultIndex
from ..maps.desk import briefing_fields


def today() -> str:
    return dt.date.today().isoformat()


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def verify_vault(cfg: KFConfig) -> None:
    """Ported v1.0 stamp guard: refuse to write into an unstamped directory."""
    if not cfg.vault_marker.exists():
        raise RuntimeError(
            f"KF_VAULT={cfg.vault} is not marked as a Knowledge Forge vault "
            f"(missing {cfg.vault_marker}). Stamp it once with `kf doctor --stamp` "
            "if this is the intended vault. Never write into an unstamped vault."
        )


def stamp_vault(cfg: KFConfig, name: str = "") -> None:
    cfg.vault_marker.parent.mkdir(parents=True, exist_ok=True)
    cfg.vault_marker.write_text((name or cfg.vault.name) + "\n", encoding="utf-8")


def source_note_rel(record: dict) -> str:
    d = record["captured_at"][:10]
    return (f"10_sources/{d}-{record['platform']}-"
            f"{ids.slugify(record['title'])}--{record['id'][4:12]}.md")


def quarantine_rel(record: dict) -> str:
    queue = {"metadata_only": "capture_retry", "failed": "capture_retry",
             "usable_partial": "review_required"}.get(record["capture_status"], "capture_retry")
    return (f"80_queues/{queue}/{record['captured_at'][:10]}-"
            f"{ids.slugify(record['title'])}--{record['id'][4:12]}.md")


def build_source_note(record: dict, tldr: str = "", description: str = "",
                      tags: Optional[list] = None,
                      primary_category_id: Optional[str] = None,
                      secondary_category_ids: Optional[list] = None,
                      topic: Optional[str] = None, analysis: Optional[dict] = None,
                      user_context: Optional[str] = None,
                      classification: Optional[dict] = None) -> tuple[dict, str]:
    meta = {
        "id": record["id"],
        "note_type": "source",
        "title": record["title"],
        "description": description or "",
        "tags": tags or [],
        "lang": record.get("lang", "en"),
        "source_url": record.get("source_url"),
        "canonical_url": record.get("canonical_url"),
        "source_platform": record["platform"],
        "authors": record.get("authors") or [],
        "published_at": record.get("published_at"),
        "captured_at": record["captured_at"],
        "capture_status": record["capture_status"],
        "capture_method": record["capture_method"],
        "reading_status": "To review",
        "reading_abstract": (tldr or "").strip(),
        "content_kind": record.get("content_kind", "article"),
        "transcript_origin": record.get("transcript_origin"),
        "attachments": record.get("attachments") or [],
        "missing_elements": record.get("missing_elements") or [],
        "content_completeness": record.get("content_completeness", 0.0),
        "content_hash": record["content_hash"],
        "primary_category_id": primary_category_id,
        "secondary_category_ids": secondary_category_ids or [],
        "topic": topic,
        "classification_status": "review" if not primary_category_id else "classified",
        "classification_reason": (classification or {}).get("reason"),
        "analysis_status": "needs_review" if not analysis or analysis.get("queued") else "ready",
        "run_id": record.get("run_id"),
        "schema_version": "1.5.1",
        "created": today(),
        "updated": today(),
    }
    parts = []
    links = ["[[#Source text|Read archived text]]"] if record.get("text") else []
    if record.get("ocr_text"):
        links.append("[[#OCR (from images)|Read OCR text]]")
    links.extend(f"[[{path}|Open {Path(path).name}]]" for path in meta["attachments"])
    if links:
        parts.append(" · ".join(links))
    if meta["missing_elements"]:
        parts.append("> [!warning] Partial capture · " + ", ".join(meta["missing_elements"]))
    if tldr:
        parts.append(f"## TL;DR\n\n{tldr.strip()}")
    parts.extend(build_analysis_sections(analysis or {}))
    text = literal_source(record.get("text") or "")
    parts.append(f"## Source text\n\n{text.strip()}")
    if record.get("ocr_text"):
        parts.append(f"## OCR (from images)\n\n{literal_source(record['ocr_text']).strip()}")
    if record.get("images"):
        parts.append("## Images\n\n" + "\n".join(f"![[{i}]]" for i in record["images"]))
    parts.append("## My notes" + (f"\n\n{user_context.strip()}" if user_context else ""))
    body = "\n\n".join(parts)
    meta.update(briefing_fields(meta, body))
    return meta, body


def build_analysis_sections(analysis: dict) -> list[str]:
    points = analysis.get("points") or []
    sections = []
    if points:
        label = "Key takeaways" if analysis.get("via_model") else "Key source excerpts"
        lines = [f"## {label}", ""]
        for i, item in enumerate(points, 1):
            lines.append(f"- {literal_source(item['point'])} · [[#Evidence {i}|Evidence {i}]]")
        sections.append("\n".join(lines))
    why = analysis.get("why")
    if why:
        sections.append(f"## Why this matters · Agent interpretation\n\n{why}")
    if points:
        references = ["## Evidence"]
        for i, item in enumerate(points, 1):
            quote = literal_source(" ".join(item["quote"].split()))
            location = item.get("location", "")
            references.append(f"### Evidence {i}\n\n{location}\n\n> {quote}")
        sections.append("\n\n".join(references))
    return sections


def literal_source(value: str) -> str:
    """Display wiki bracket examples from a source as text in this vault."""
    return value.replace("[[", r"\[\[").replace("]]", r"\]\]")


def build_quarantine_note(record: dict) -> tuple[dict, str]:
    meta = {
        "id": record["id"],
        "note_type": "quarantined_capture",
        "title": record["title"],
        "capture_status": record["capture_status"],
        "capture_method": record["capture_method"],
        "content_kind": record.get("content_kind", "article"),
        "attachments": record.get("attachments") or [],
        "content_completeness": record.get("content_completeness", 0.0),
        "needs_retry": record.get("needs_retry", True),
        "missing_elements": record.get("missing_elements", []),
        "quality_reasons": record.get("quality_reasons", []),
        "source_url": record.get("source_url"),
        "canonical_url": record.get("canonical_url"),
        "source_platform": record["platform"],
        "captured_at": record["captured_at"],
        "content_hash": record["content_hash"],
        "run_id": record.get("run_id"),
        "schema_version": "1.5.1",
        "created": today(),
        "updated": today(),
    }
    body = (
        "> [!warning] 未完整收录 / Incomplete capture\n"
        f"> status: **{record['capture_status']}** · completeness: "
        f"{record.get('content_completeness', 0):.0%}\n"
        f"> reasons: {', '.join(record.get('quality_reasons') or ['unknown'])}\n\n"
        "## What was obtained\n\n"
        f"{literal_source((record.get('text') or '(no body text)').strip()[:4000])}\n"
    )
    body += "\n\n" + "\n".join(f"- [[{path}|Open {Path(path).name}]]" for path in meta["attachments"])
    return meta, body


def plan_source_filing(cfg: KFConfig, vi: VaultIndex, record: dict, **enrich) -> dict:
    """Decide what to do with a capture record given the current vault.
    Returns {action, rel, meta, body} without writing anything.
    Actions: new | noop_duplicate | new_version | quarantine."""
    if record["capture_status"] in ("metadata_only", "failed"):
        meta, body = build_quarantine_note(record)
        return {"action": "quarantine", "rel": quarantine_rel(record), "meta": meta, "body": body}

    existing = vi.by_id.get(record["id"])
    if existing is None:
        # secondary check: same canonical URL under a different ID (pre-1.5 notes)
        for n in vi.notes:
            if n.meta.get("canonical_url") and n.meta["canonical_url"] == record.get("canonical_url"):
                existing = n
                break
    if existing is None:
        meta, body = build_source_note(record, **enrich)
        return {"action": "new", "rel": source_note_rel(record), "meta": meta, "body": body}

    from ..discovery import rel as _rel
    existing_rel = _rel(cfg.vault, existing.path)

    # A quarantined capture is NOT a filed source. A now-usable re-capture
    # PROMOTES it: a source note is created and the receipt is marked resolved.
    if existing.note_type == "quarantined_capture" or existing_rel.startswith("80_queues/"):
        meta, body = build_source_note(record, **enrich)
        return {"action": "promote", "rel": source_note_rel(record),
                "meta": meta, "body": body,
                "resolve_queue_note": existing_rel + ".md"}

    analysis_improved = (existing.meta.get("analysis_status") != "ready"
                         and enrich.get("analysis") and not enrich["analysis"].get("queued"))
    topic_improved = not existing.meta.get("primary_category_id") and enrich.get("primary_category_id")
    if (not analysis_improved and not topic_improved
            and existing.meta.get("content_hash") == record["content_hash"]
            and existing.meta.get("capture_status") == record["capture_status"]
            and set(record.get("attachments") or []) <= set(existing.meta.get("attachments") or [])):
        return {"action": "noop_duplicate", "rel": existing_rel,
                "meta": existing.meta, "body": existing.body}

    # content changed → append a version section, keep original text intact
    meta = dict(existing.meta)
    current, current_body = build_source_note(record, **enrich)
    for field in ("id", "content_kind", "capture_method", "capture_status", "content_completeness",
                  "description", "missing_elements", "analysis_status", "canonical_url"):
        meta[field] = current[field]
    if (existing.meta.get("reading_guide_version") and existing.meta.get("analysis_status") == "ready"
            and existing.meta.get("capture_status") == "complete"):
        # The reviewed guide still refers to the archived, complete version above the new capture.
        meta["analysis_status"] = "ready"
    meta["attachments"] = list(dict.fromkeys((meta.get("attachments") or []) + current["attachments"]))
    if not meta.get("primary_category_id"):
        for field in ("primary_category_id", "secondary_category_ids", "topic",
                      "classification_status", "classification_reason"):
            meta[field] = current[field]
    if current["reading_abstract"] and not meta.get("reading_abstract"):
        meta["reading_abstract"] = current["reading_abstract"]
    meta["updated"] = today()
    meta["last_captured_at"] = record["captured_at"]
    meta["content_hash"] = record["content_hash"]
    versions = meta.get("version_history") or []
    versions.append({"hash": existing.meta.get("content_hash"), "superseded_at": now_iso()})
    meta["version_history"] = versions
    body = (existing.body.rstrip()
            + f"\n\n## Updated capture ({record['captured_at'][:10]}) · Version {len(versions) + 1}\n\n"
            + current_body + "\n")
    added_files = [path for path in current["attachments"] if path not in (existing.meta.get("attachments") or [])]
    if (added_files and existing.meta.get("content_hash") == record["content_hash"]
            and not analysis_improved):
        body = existing.body.rstrip() + "\n\n## Additional archive files\n\n" + "\n".join(
            f"- [[{path}|Open {Path(path).name}]]" for path in added_files)
    meta.update(briefing_fields(meta, body))
    # existing_rel is extension-stripped (discovery.rel → .with_suffix("")); the
    # written path must carry .md or new_version silently creates an extensionless
    # sibling that Obsidian and every *.md scan ignore while the note goes stale.
    return {"action": "new_version", "rel": existing_rel + ".md",
            "meta": meta, "body": body}


def render(meta: dict, body: str) -> str:
    return fm.dump(meta, body)
