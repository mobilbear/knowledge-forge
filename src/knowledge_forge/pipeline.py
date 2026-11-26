"""Application-owned capture pipeline. The model never orchestrates this graph;
it is invoked at exactly two bounded points (enrich, classify) and its output
is schema-validated before any effect.

capture_url / capture_images / capture_file →
  received → capturing → normalized → quality gate
  → usable  : enrich → classify → stage source note (flag unclassified notes for review)
  → other   : stage quarantine note in 80_queues/
  → stage map/HOME regeneration → validate → lock → commit → index refresh
  → verified envelope result
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from . import frontmatter as fm
from . import ids
from .config import DATA_ROOT, KFConfig
from .urlnorm import canonicalize
from .discovery import VaultIndex, rel
from .capture import adapters, jobs, references
from .normalize.record import build_record
from .quality import gate
from .enrichment import harness, tasks
from .taxonomy.registry import TaxonomyRegistry, default_registry_nodes
from .vault import library_writes
from .vault import writer
from .index import search
from .maps import generators, reading
from .observability.logs import OpLog


def _registry(cfg: KFConfig) -> TaxonomyRegistry:
    reg = TaxonomyRegistry.load(cfg.taxonomy_registry_path)
    if not reg.nodes:
        for n in default_registry_nodes():
            reg.nodes[n["category_id"]] = n
    return reg


def _provider(cfg: KFConfig, profile: str) -> harness.Provider:
    profiles = harness.load_profiles(cfg.kf_home / "config")
    if not profiles:
        profiles = harness.load_profiles(DATA_ROOT / "config")
    return harness.provider_for_profile(profile, profiles, cfg.secret)


def _quality_precheck(raw: dict) -> bool:
    """Cascade continuation check: does this raw capture look like real content?"""
    return _grade_candidate(raw)["capture_status"] == "complete"


def _grade_candidate(raw: dict) -> dict:
    candidate = build_record(raw, None, "run_candidate")
    gate.transition(candidate, "capturing")
    gate.transition(candidate, "normalized")
    return gate.assess(candidate)


def _captured_input(cfg, source, kind, image_paths, public_fallback, transcribe_preview):
    if kind == "url":
        options = {**cfg.raw, "KF_PUBLIC_READER": "firecrawl"} if public_fallback else cfg.raw
        if transcribe_preview:
            options = {**options, "KF_TRANSCRIBE_PREVIEW": "true"}
        return adapters.fetch_url(source, options, _quality_precheck, _grade_candidate)
    if kind == "file":
        return adapters.cap_file(source, cfg.raw)
    if kind != "images":
        raise ValueError(f"unknown capture kind: {kind}")

    paths = [Path(path) for path in image_paths or [source]]
    if len(paths) > 12 or not all(path.is_file() for path in paths):
        raise ValueError("supply up to twelve local images")
    if sum(path.stat().st_size for path in paths) > 24 * 1024 * 1024:
        raise ValueError("image set exceeds the local capture limit")
    formats = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    if any(path.suffix.lower() not in formats for path in paths):
        raise ValueError("supported image formats are JPEG, PNG, WebP, and GIF")
    text, warnings = adapters.ocr_images(paths, cfg.raw)
    raw = {
        "platform": "image", "title": paths[0].stem, "content_kind": "image post",
        "text": "", "ocr_text": text, "capture_method": "ocr" if text else "none",
        "artifacts": [(f"image-{i:02d}{path.suffix.lower()}", path.read_bytes())
                      for i, path in enumerate(paths, 1)],
        "body_verified": bool(text and not warnings),
    }
    if warnings:
        raw["partial_reason" if text else "adapter_error"] = " | ".join(warnings)
    return raw


def _prepare_artifacts(record, raw):
    files = {}
    record["attachments"] = []
    if raw.get("artifacts"):
        record["images"] = []
    for name, data in raw.get("artifacts", []):
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+", name) or name.startswith("."):
            raise ValueError("invalid archive attachment name")
        digest = hashlib.sha256(data).hexdigest()
        path = f"_attachments/library/{record['id']}/{digest[:16]}/{name}"
        files[path] = data
        record["attachments"].append(path)
        if name.startswith("image-"):
            record["images"].append(path)
    if files and not (record.get("text") or record.get("ocr_text")):
        basis = "|".join(hashlib.sha256(data).hexdigest() for data in files.values())
        record["content_hash"] = ids.content_sha256(basis)
    return files


def _capture_result(run, status, result, message, warnings=None, needs_input=False):
    return {"run_id": run, "status": status, "result": result,
            "warnings": warnings or [], "errors": [], "needs_user_action": needs_input,
            "user_message_zh": message}


def _existing_capture(vi, record):
    identities = {canonicalize(url) for url in (record.get("source_url"), record.get("canonical_url")) if url}
    matches = [
        note for note in vi.notes if note.note_type in ("source", "quarantined_capture")
        and (note.note_id == record["id"] or any(
            canonicalize(url) in identities for url in (
                note.meta.get("canonical_url"), note.meta.get("source_url"), note.meta.get("url")) if url
        ))
    ]
    return min(matches, key=lambda note: (
        note.note_type != "source", note.note_id != record["id"],
        note.meta.get("capture_status") != "complete",
    ), default=None)


def _stage_maps(cfg, registry, scope=None):
    vi = VaultIndex.build(cfg.vault)
    files = (generators.rebuild_all(cfg, vi, registry) if not scope else
             {**generators.build_domain_map(cfg, vi, registry, scope),
              **generators.build_topic_map(cfg, vi, registry, scope)})
    txn = library_writes.new_transaction(cfg, {"action": "library.rebuild"})
    for path, content in files.items():
        target = cfg.vault / path
        if not target.exists() or target.read_text(encoding="utf-8") != content:
            txn.stage_write(path, content)
    if txn.ops:
        txn.commit(after_commit=lambda: search.refresh_after_commit(cfg))
    else:
        search.refresh_after_commit(cfg)
    return sorted(files)


def rebuild_library(cfg, scope=None):
    writer.verify_vault(cfg)
    with library_writes.library_lock(cfg.state_home / "library.lock"):
        library_writes.recover(cfg, lambda: search.refresh_after_commit(cfg))
        return _stage_maps(cfg, _registry(cfg), scope=scope)


def initialize_library(cfg):
    """Stamp the selected vault and persist its initial registry if absent."""
    with library_writes.library_lock(cfg.state_home / "library.lock"):
        library_writes.recover(cfg, lambda: search.refresh_after_commit(cfg))
        registry = _registry(cfg)
        if not cfg.vault_marker.is_file():
            writer.stamp_vault(cfg)
        if not cfg.taxonomy_registry_path.is_file():
            transaction = library_writes.new_transaction(cfg, {"action": "library.init"})
            transaction.stage_write("90_meta/taxonomy/registry.yaml", registry.dumps())
            transaction.commit()
        _stage_maps(cfg, registry)


def apply_reading_guides(cfg, paths):
    """Validate all review inputs before updating source notes in place."""
    import datetime as dt
    import json

    writer.verify_vault(cfg)
    cards = {}
    for path in paths:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or cards.keys() & payload.keys():
            raise ValueError("reading guides must have unique source filenames")
        cards.update(payload)
    with library_writes.library_lock(cfg.state_home / "library.lock"):
        library_writes.recover(cfg, lambda: search.refresh_after_commit(cfg))
        registry = _registry(cfg)
        vi = VaultIndex.build(cfg.vault)
        transaction = library_writes.new_transaction(cfg, {"action": "library.guides"})
        for basename, card in sorted(cards.items()):
            if Path(basename).name != basename or not basename.endswith(".md"):
                raise ValueError("expected a source-note basename")
            target = "10_sources/" + basename
            note = vi.by_rel.get(target[:-3])
            if not note or note.note_type != "source" or note.parse_error:
                raise ValueError(f"source note is unavailable: {basename}")
            try:
                metadata, body = reading.render_card(cfg.vault, note, card, registry, vi)
            except ValueError as error:
                raise ValueError(f"{basename}: {error}") from error
            content = fm.dump(metadata, body, preserve_body=True)
            if note.path.read_text(encoding="utf-8") != content:
                transaction.stage_write(target, content)
        count = len(transaction.ops)
        if count:
            log_path = "90_meta/Library Activity.md"
            log = cfg.vault / log_path
            previous = log.read_text(encoding="utf-8") if log.is_file() else "# Library Activity\n\n[[HOME|Home]]\n"
            transaction.stage_write(
                log_path, previous.rstrip() + f"\n\n## [{dt.date.today()}] review | Reading guides\n\n"
                f"Updated {count} source notes. Existing source text and annotations retained.\n")
            transaction.commit()
        _stage_maps(cfg, registry)
        return count


def prepare_library(cfg):
    """Add navigation properties and extractive evidence only to older source notes."""
    writer.verify_vault(cfg)
    with library_writes.library_lock(cfg.state_home / "library.lock"):
        library_writes.recover(cfg, lambda: search.refresh_after_commit(cfg))
        registry = _registry(cfg)
        txn = library_writes.new_transaction(cfg, {"action": "library.prepare"})
        for source in VaultIndex.build(cfg.vault).notes:
            relative = rel(cfg.vault, source.path)
            if source.note_type != "source" or not relative.startswith("10_sources/"):
                continue
            meta = dict(source.meta)
            if meta.get("library_prepared") or meta.get("analysis_status"):
                continue
            meta["note_type"] = "source"
            platform = meta.get("source_platform") or meta.get("platform") or "web"
            method = meta.get("capture_method")
            category = registry.get(meta.get("primary_category_id") or "")
            meta["topic"] = category["title"] if category else "Unclassified"
            meta["source_platform"] = platform
            meta["content_kind"] = (
                "abstract" if platform == "arxiv" and method == "api" else
                "document" if platform == "arxiv" else
                "transcript" if method == "transcript" else
                "image post" if platform == "xiaohongshu" else "article")
            if meta["content_kind"] == "abstract":
                meta["capture_status"] = "usable_partial"
                meta["missing_elements"] = ["full paper"]
            evidence = re.split(r"(?m)^## (?:Source text|OCR \(from images\))\s*$", source.body)
            extras = []
            if len(evidence) > 1 and "## Key takeaways" not in source.body:
                groups = tasks.evidence_groups("\n\n".join(evidence[1:]))
                extras = writer.build_analysis_sections({"points": tasks.excerpt_points(groups)})
            meta["library_prepared"] = True
            meta["analysis_status"] = "needs_review"
            text = source.body.rstrip() + ("\n\n" + "\n\n".join(extras) if extras else "")
            txn.stage_write(relative + ".md", writer.render(meta, text))
        if txn.ops:
            txn.commit()
        _stage_maps(cfg, registry)
        return len(txn.ops)


def _resource_links(note):
    body = reading.original_body(note.body)
    versions = re.split(r"(?m)^## Updated capture \(\d{4}-\d{2}-\d{2}\)[ \t]*$", body)
    fragments = []
    for version in reversed(versions):
        content = re.split(r"(?im)^#{2,3} (?:source text|original article|raw source|full text|transcript)\s*$",
                           version, maxsplit=1)[-1]
        fragments.append(re.split(r"(?im)^#{1,3} (?:my notes|personal notes|agent interpretation)\s*$",
                                  content, maxsplit=1)[0])
    origin = note.meta.get("source_url") or note.meta.get("canonical_url") or note.meta.get("url")
    return references.discover("\n\n".join(fragments), origin)


def _archived_source(vi, source_ref):
    source = vi.by_id.get(source_ref) or vi.by_rel.get(source_ref.removesuffix(".md"))
    if not source or source.note_type != "source" or source.parse_error:
        raise ValueError("select an archived source note by ID or vault-relative path")
    return source


def _source_with_companion(source, meta, field, label):
    link = reading.companion_link(meta, field, label)
    body = source.body
    if link in body:
        return body
    if body.startswith(reading.BEGIN):
        guide, boundary, archive = body.partition(reading.END)
        if not boundary:
            raise ValueError("the source guide has an incomplete boundary marker")
        abstract = "\n## Abstract\n"
        guide = guide.replace(abstract, f"\n{link}\n\n## Abstract\n", 1) if abstract in guide else guide + link + "\n\n"
        return guide + boundary + archive
    return f"{link}\n\n{body}"


_WIKILINK = re.compile(r"(?<!\\)\[\[([^#|\[\]\n]*)(?:#([^|\[\]\n]+))?(?:\|[^\[\]\n]*)?\]\]")
_MARKDOWN_LITERAL = re.compile(
    r"<!--.*?(?:-->|\Z)|^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?(?:^ {0,3}\1[ \t]*$|\Z)"
    r"|(`+)[^\n]*?\2|^[ ]{4}[^\n]*$", re.MULTILINE | re.DOTALL)


def _visible_markdown(body):
    return _MARKDOWN_LITERAL.sub("", body)


def _headings(body):
    return set(re.findall(r"(?m)^#{1,6} ([^\n]+)$", _visible_markdown(body)))


def publish_edition(cfg, source_ref, draft_path):
    """Publish the day's edition with checked evidence links to the new source."""
    writer.verify_vault(cfg)
    draft = Path(draft_path)
    if draft.suffix.lower() != ".md" or draft.stat().st_size > 128 * 1024:
        raise ValueError("supply a Markdown edition draft below 128 KiB")
    draft_meta, body = fm.read_note(draft)
    visible = _visible_markdown(body)
    heading = re.search(r"(?m)^# ([^\n]+)$", visible)
    if not heading:
        raise ValueError("the edition should have a Markdown title")
    with library_writes.library_lock(cfg.state_home / "library.lock"):
        library_writes.recover(cfg, lambda: search.refresh_after_commit(cfg))
        vi = VaultIndex.build(cfg.vault)
        source = _archived_source(vi, source_ref)
        if source.meta.get("capture_status") not in ("complete", "usable_partial"):
            raise ValueError("the triggering source needs a usable archive")
        trigger = rel(cfg.vault, source.path)
        source_evidence = False
        for target, anchor in _WIKILINK.findall(visible):
            if not target:
                continue
            path, _ = vi.resolve_link(target)
            if path is None:
                raise ValueError(f"unresolved edition link: {target}")
            linked = vi.by_rel.get(path)
            if anchor and linked:
                if anchor not in _headings(linked.body):
                    raise ValueError(f"unresolved edition evidence heading: {target}#{anchor}")
                source_evidence |= path == trigger
        if not source_evidence:
            raise ValueError("cite an exact evidence heading in the triggering source")
        timestamp = datetime.now().astimezone()
        day = timestamp.date().isoformat()
        path = f"60_briefings/editions/{day}-rolling.md"
        note_id = f"brf_editorial_rolling_{day.replace('-', '')}"
        destination = cfg.vault / path
        if destination.is_file():
            previous, previous_body = fm.read_note(destination)
            if previous.get("id") != note_id:
                raise ValueError("the edition filename already belongs to another note")
            if previous_body == body and previous.get("edition_deck") == (draft_meta.get("edition_deck") or ""):
                _stage_maps(cfg, _registry(cfg))
                return {"edition": path, "source": trigger + ".md"}
        meta = {"id": note_id, "note_type": "briefing", "briefing_kind": "editorial", "title": heading[1],
                "edition_date": day, "edition_updated": timestamp.isoformat(timespec="seconds"),
                "edition_deck": draft_meta.get("edition_deck") or ""}
        txn = library_writes.new_transaction(cfg, {"action": "library.edition"})
        txn.stage_write(path, fm.dump(meta, body, preserve_body=True))
        txn.commit()
        _stage_maps(cfg, _registry(cfg))
    return {"edition": path, "source": trigger + ".md"}


def attach_lesson(cfg, source_ref, draft_path):
    """File a completed lesson after checking its primary evidence links."""
    writer.verify_vault(cfg)
    draft = Path(draft_path)
    if draft.suffix.lower() != ".md" or draft.stat().st_size > 256 * 1024:
        raise ValueError("supply a Markdown lesson draft below 256 KiB")
    draft_meta, body = fm.read_note(draft)
    visible = _visible_markdown(body)
    heading = re.search(r"(?m)^# ([^\n]+)$", visible)
    if not heading:
        raise ValueError("the lesson should have a Markdown title")
    minutes = draft_meta.get("read_minutes", 12)
    if not isinstance(minutes, int) or isinstance(minutes, bool) or not 1 <= minutes <= 120:
        raise ValueError("read_minutes must be between 1 and 120")
    with library_writes.library_lock(cfg.state_home / "library.lock"):
        library_writes.recover(cfg, lambda: search.refresh_after_commit(cfg))
        vi = VaultIndex.build(cfg.vault)
        source = _archived_source(vi, source_ref)
        if source.meta.get("capture_status") != "complete":
            raise ValueError("a finished guided lesson requires a complete primary archive")
        source_path = rel(cfg.vault, source.path)
        links = _WIKILINK.findall(visible)
        cited_headings = [anchor for target, anchor in links
                          if target.removesuffix(".md") == source_path and anchor]
        headings = _headings(reading.original_body(source.body))
        if not cited_headings or any(anchor not in headings for anchor in cited_headings):
            raise ValueError("the lesson must cite exact evidence headings in the primary archive")
        for target, _ in links:
            if target and not vi.resolve_link(target)[0]:
                raise ValueError(f"unresolved lesson link: {target}")
        key = hashlib.sha256((source.note_id or source_path).encode()).hexdigest()[:12]
        target = source.meta.get("reading_lesson") or f"50_skills/Deep Reading/{ids.slugify(heading[1], 48)}--{key}"
        if not isinstance(target, str) or not target.startswith("50_skills/Deep Reading/"):
            raise ValueError("the lesson target must be in 50_skills/Deep Reading")
        dest = cfg.vault / (target + ".md")
        lesson_folder = cfg.vault / "50_skills/Deep Reading"
        if dest.is_symlink() or not dest.resolve().is_relative_to(lesson_folder.resolve()):
            raise ValueError("invalid lesson target")
        prior = fm.read_note(dest)[0] if dest.is_file() else {}
        if prior and (prior.get("note_type") != "lesson" or
                      (prior.get("source_note") != source_path and source.note_id not in (prior.get("source_ids") or []))):
            raise ValueError("the lesson path already belongs to a different note")
        lesson_meta = {**prior, "id": prior.get("id") or "lesson_" + key, "note_type": "lesson",
                       "title": heading[1], "source_note": source_path, "read_minutes": minutes}
        navigation = f"[[HOME|Home]] · [[{source_path}|Archived source]]"
        for attachment in source.meta.get("attachments") or []:
            if attachment.endswith(".pdf") and vi.resolve_link(attachment)[0]:
                navigation += f" · [[{attachment}|Original PDF]]"
        lesson_body = f"{navigation}\n\n{body}"
        meta = {**source.meta, "reading_status": "Read deeply", "reading_lesson": target,
                "read_minutes": minutes, "reading_cue": str(draft_meta.get("reading_cue") or "Work through the guided explanation.")}
        source_body = _source_with_companion(source, meta, "reading_lesson", "Open the guided lesson")
        txn = library_writes.new_transaction(cfg, {"action": "library.lesson"})
        for path, content in ((target + ".md", fm.dump(lesson_meta, lesson_body, preserve_body=True)),
                              (source_path + ".md", fm.dump(meta, source_body, preserve_body=True))):
            file = cfg.vault / path
            if not file.is_file() or file.read_text(encoding="utf-8") != content:
                txn.stage_write(path, content)
        if txn.ops:
            txn.commit()
        _stage_maps(cfg, _registry(cfg))
    return {"lesson": target + ".md", "source": source_path + ".md"}


def review_resources(cfg, source_ref):
    """Read a bounded set of first-party pages cited by one saved source."""
    writer.verify_vault(cfg)
    vi = VaultIndex.build(cfg.vault)
    source = _archived_source(vi, source_ref)
    if source.meta.get("capture_status") in ("metadata_only", "failed"):
        raise ValueError("archive the primary article or usable passage before reviewing its references")
    source_path = rel(cfg.vault, source.path)
    links = _resource_links(source)
    if not links:
        return {"source": source_path + ".md", "note": None, "links": 0, "evidence": []}
    evidence = [references.inspect_resource(url) for url in links[:4]]
    key = hashlib.sha256((source.note_id or source_path).encode()).hexdigest()[:12]
    target = f"50_skills/Referenced Resources/{ids.slugify(source.path.stem, 42)}--{key}.md"
    marker, end = "<!-- liangwiki:resource-review -->", "<!-- /liangwiki:resource-review -->"
    generated = f"{marker}\n{references.render_review(links, evidence)}{end}"
    with library_writes.library_lock(cfg.state_home / "library.lock"):
        library_writes.recover(cfg, lambda: search.refresh_after_commit(cfg))
        current = VaultIndex.build(cfg.vault).by_rel.get(source_path)
        if (not current or current.note_id != source.note_id or _resource_links(current) != links
                or current.meta.get("capture_status") in ("metadata_only", "failed")):
            raise ValueError("the primary source changed during review; retry with the latest archive")
        destination = cfg.vault / target
        title = " ".join(str(current.meta.get("title") or current.path.stem).split())
        heading = "Referenced resources · " + writer.literal_source(title)
        if destination.is_symlink():
            raise ValueError("the referenced-resource note is a symbolic link")
        if destination.is_file():
            review_meta, body = fm.read_note(destination)
            before, start, remainder = body.partition(marker)
            _, close, after = remainder.partition(end)
            if review_meta.get("id") != "resource_" + key or not (start and close):
                raise ValueError("existing resource note lacks the expected review section")
            body = before + generated + after
        else:
            review_meta = {"id": "resource_" + key, "note_type": "resource_review", "title": heading,
                           "source_note": source_path}
            body = f"# {heading}\n\n[[HOME|Home]] · [[{source_path}|Original article and guide]]\n\n{generated}\n"
        source_meta = {**current.meta, "resource_review": target.removesuffix(".md")}
        source_body = _source_with_companion(current, source_meta, "resource_review", "Referenced resource briefing")
        txn = library_writes.new_transaction(cfg, {"action": "library.resources"})
        for path, content in ((target, fm.dump(review_meta, body, preserve_body=True)),
                              (source_path + ".md", fm.dump(source_meta, source_body, preserve_body=True))):
            file = cfg.vault / path
            if not file.is_file() or file.read_text(encoding="utf-8") != content:
                txn.stage_write(path, content)
        if txn.ops:
            txn.commit(after_commit=lambda: search.refresh_after_commit(cfg))
    return {"source": source_path + ".md", "note": target, "links": len(links),
            "evidence": [asdict(item) for item in evidence]}


def import_web_clips(cfg):
    """File browser-saved Markdown locally, keeping the clip as a receipt."""
    writer.verify_vault(cfg)
    folder = cfg.dir_inbox / "Web Clips"
    result = {"filed": [], "unchanged": 0, "errors": []}
    if not folder.resolve().is_relative_to(cfg.vault.resolve()):
        raise ValueError("Web Clips folder must be inside the vault")
    with library_writes.library_lock(cfg.state_home / "web-clips.lock"):
        for path in sorted(folder.glob("*.md")):
            try:
                if path.is_symlink():
                    raise ValueError("symbolic links are not accepted")
                with path.open("rb") as handle:
                    data = handle.read(4 * 1024 * 1024 + 1)
                if len(data) > 4 * 1024 * 1024:
                    raise ValueError("clip exceeds 4 MiB")
                meta, body = fm.parse(data.decode("utf-8-sig"))
                if meta.get("note_type") != "web_clip":
                    continue
                url = meta.get("source_url")
                parsed = urlsplit(url if isinstance(url, str) else "")
                if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None:
                    raise ValueError("clip needs an HTTP or HTTPS source_url")
                heading = re.match(r"^# ([^\r\n]+)\r?\n", body)
                title = heading[1].strip() if heading else path.stem
                text = (body[heading.end():] if heading else body).strip()
                scope = meta.get("clip_scope")
                confirmed = scope == "Full article"
                fingerprint = ids.content_sha256(repr((url, title, text, scope)))
                if meta.get("clip_import_hash") == fingerprint and meta.get("clip_archive"):
                    result["unchanged"] += 1
                    continue
                raw = {"title": title, "text": text, "content_kind": "article",
                       "capture_method": "browser", "via": "obsidian-web-clipper",
                       "body_verified": confirmed,
                       "partial_reason": None if confirmed else "Browser clip coverage was not verified"}
                run = ids.run_id()
                log = OpLog(cfg, run, command="library.import-clips")
                log.event("capturing", source_kind="local browser clip")
                archived = _archive_capture(cfg, run, log, raw, url, None, "offline")
                outcome = archived["result"]
                target = outcome.get("note") or outcome.get("existing_note")
                with library_writes.library_lock(cfg.state_home / "library.lock"):
                    if not target or not (cfg.vault / target).is_file():
                        raise ValueError("archive or capture receipt could not be verified")
                    if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
                        raise ValueError("clip changed during import; rerun to update the receipt")
                    meta.update(clip_archive=f"[[{target}]]", clip_import_hash=fingerprint)
                    txn = library_writes.new_transaction(cfg, {"action": "library.import-clips"})
                    txn.stage_write(path.relative_to(cfg.vault).as_posix(), fm.dump(meta, body, preserve_body=True))
                    txn.commit()
                result["filed"].append({"clip": path.name, "note": target,
                                        "capture_status": outcome["capture_status"]})
            except (ValueError, OSError) as error:
                result["errors"].append(f"{path.name}: {error}")
    return result


def run_capture(cfg: KFConfig, source: str, kind: str = "url",
                note: Optional[str] = None, profile: str = "low",
                image_paths: Optional[list] = None,
                _test_raw: Optional[dict] = None,
                source_url: Optional[str] = None, public_fallback: bool = False,
                transcribe_preview: bool = False, follow_references: bool = False) -> dict:
    """Capture, enrich and verify one archived item, preserving prior receipts."""
    run = ids.run_id()
    writer.verify_vault(cfg)
    log = OpLog(cfg, run, command=f"capture.{kind}")
    log.event("capturing", source_kind=kind)
    raw = (_test_raw if _test_raw is not None
           else _captured_input(cfg, source, kind, image_paths, public_fallback, transcribe_preview))
    archived = _archive_capture(cfg, run, log, raw, source if kind == "url" else source_url,
                                note, profile, infer_platform=bool(source_url))
    result = archived.get("result") or {}
    target = result.get("note") or result.get("existing_note")
    if follow_references and target and result.get("linked_resources") and archived["status"] in ("completed", "noop"):
        try:
            reviewed = review_resources(cfg, target)
            if reviewed["note"]:
                result["resource_review"] = reviewed["note"]
        except (ValueError, OSError, RuntimeError):
            archived["warnings"].append("The primary source was saved. Its linked resources still need review.")
    return archived


def _archive_capture(cfg, run, log, raw, source_url, note, profile, infer_platform=True):
    if source_url and infer_platform:
        raw["platform"] = adapters.platform_of(source_url)
    record = build_record(raw, source_url, run)
    if not record.get("source_url") and raw.get("artifacts"):
        digest = "|".join(hashlib.sha256(data).hexdigest() for _, data in raw["artifacts"])
        record["content_hash"] = ids.content_sha256(digest)
        record["canonical_url_or_local"] = record["content_hash"]
        record["id"] = ids.source_id(None, record["content_hash"])
    artifacts = _prepare_artifacts(record, raw)
    gate.transition(record, "capturing")
    gate.transition(record, "normalized")
    gate.assess(record)
    linked_count = len(references.discover(record["text"], record.get("source_url"))) if gate.is_usable(record) else 0
    log.event("quality_checked", status=record["capture_status"], adapter=raw.get("via"))
    registry = _registry(cfg)
    warnings, enrich, classification = [], {}, {}
    if gate.is_usable(record):
        provider = harness.OfflineProvider() if raw.get("via") == "obsidian-web-clipper" else _provider(cfg, profile)
        enrich = tasks.enrich(record, provider, user_context=note)
        classification = tasks.classify(record, enrich["tag_proposals"], registry, provider)
        if enrich.get("queued"):
            warnings.append("enrichment needs review")
        if classification.get("pending"):
            warnings.append("classification pending: " + classification.get("reason", ""))

    rules = record.get("quality_reasons") or []
    if record["capture_status"] != "complete":
        warnings.extend(rules)
    with library_writes.library_lock(cfg.state_home / "library.lock"):
        library_writes.recover(cfg, lambda: search.refresh_after_commit(cfg))
        vi = VaultIndex.build(cfg.vault)
        existing = _existing_capture(vi, record)
        if existing:
            record["id"] = existing.note_id or record["id"]
            # Match legacy URL spellings without moving the note or changing its ID.
            vi.by_id[record["id"]] = existing
        if (raw.get("via") == "obsidian-web-clipper" and existing and existing.note_type == "source"
                and existing.meta.get("capture_status") == "complete" and record["needs_retry"]):
            return _capture_result(run, "noop", {"existing_note": rel(cfg.vault, existing.path) + ".md",
                                                "capture_status": "complete"},
                                   "该链接已有完整存档。")
        keep_existing = (existing and existing.note_type == "source"
                and (not gate.is_usable(record) or
                     (existing.meta.get("capture_status") == "complete" and record["needs_retry"])))
        category_id = classification.get("primary_category_id")
        category = registry.get(category_id) if category_id else None
        plan = writer.plan_source_filing(cfg, vi, record, tldr=enrich.get("tldr", ""),
                                         description=enrich.get("description", ""),
                                         tags=enrich.get("tag_proposals"),
                                         primary_category_id=category_id,
                                         secondary_category_ids=classification.get("secondary_category_ids"),
                                         topic=category.get("title") if category else "Unclassified",
                                         classification=classification,
                                         analysis=enrich, user_context=note)
        if keep_existing:
            meta, body = writer.build_quarantine_note(record)
            meta.update(id="attempt_" + run[4:], source_id=record["id"])
            body += f"\n\nExisting archive: [[{rel(cfg.vault, existing.path)}]]."
            plan = {"action": "quarantine", "rel": f"80_queues/review_required/{run}.md",
                    "meta": meta, "body": body}
        if plan["action"] == "noop_duplicate":
            status = existing.meta.get("capture_status", "usable_partial")
            repair = None
            for path, data in artifacts.items():
                if not (cfg.vault / path).is_file():
                    if repair is None:
                        repair = library_writes.new_transaction(cfg, {"action": "library.repair", "run_id": run})
                    repair.stage_artifact(path, data)
            if repair is not None:
                repair.commit()
            _stage_maps(cfg, registry)
            if not search.contains_note(cfg, record["id"]):
                raise RuntimeError("saved capture could not be verified in the search index")
            if status == "complete":
                jobs.reconcile_archived(cfg, record.get("source_url"), record["id"], plan["rel"] + ".md")
            return _capture_result(run, "noop",
                                   {"action": plan["action"], "existing_note": plan["rel"] + ".md",
                                    "source_id": record["id"], "capture_status": status,
                                    "linked_resources": linked_count},
                                   "同一份内容已存在。", warnings, status != "complete")
        if existing and existing.note_type == "quarantined_capture" and plan["action"] == "quarantine":
            plan["rel"] = rel(cfg.vault, existing.path) + ".md"
            plan["body"] = existing.body.rstrip() + "\n\n## Latest attempt\n\n" + plan["body"]
        gate.transition(record, "staged")
        gate.transition(record, "validated")
        transaction = library_writes.new_transaction(cfg, {"action": "library.capture", "run_id": run})
        for path, data in artifacts.items():
            if not (cfg.vault / path).is_file():
                transaction.stage_artifact(path, data)
        transaction.stage_write(plan["rel"], writer.render(plan["meta"], plan["body"]))
        if plan.get("resolve_queue_note"):
            queued = vi.by_id.get(record["id"])
            if queued:
                closed = {**queued.meta, "id": "resolved_" + record["id"],
                          "note_type": "resolved_capture", "resolved_to": plan["rel"]}
                transaction.stage_write(plan["resolve_queue_note"], writer.render(
                    closed, queued.body + f"\n\nResolved by [[{plan['rel']}]]."))
        if not cfg.taxonomy_registry_path.exists():
            transaction.stage_write("90_meta/taxonomy/registry.yaml", registry.dumps())
        transaction.commit(after_commit=lambda: search.refresh_after_commit(cfg))
        _stage_maps(cfg, registry)
        if not (cfg.vault / plan["rel"]).is_file() or not search.contains_note(cfg, plan["meta"]["id"]):
            raise RuntimeError("saved capture could not be verified in the search index")
        if any(not (cfg.vault / path).is_file() for path in artifacts):
            raise RuntimeError("saved capture is missing an original attachment")

    labels = {"complete": "完整存档", "usable_partial": "部分存档",
              "metadata_only": "仅取得元数据", "failed": "需要补充材料或重试"}
    verdict = record["capture_status"]
    if verdict == "complete":
        jobs.reconcile_archived(cfg, record.get("source_url"), record["id"], plan["rel"])
    status = "queued" if plan["action"] == "quarantine" else "completed"
    pending = bool(classification.get("pending"))
    result = {"action": plan["action"], "source_id": record["id"], "note": plan["rel"],
              "capture_status": verdict, "content_kind": record["content_kind"],
              "content_completeness": record["content_completeness"],
              "capture_method": record["capture_method"], "missing_elements": record["missing_elements"],
              "attachments": list(artifacts), "primary_category_id": category_id,
              "linked_resources": linked_count}
    log.event("reported", rel=plan["rel"], action=plan["action"])
    message = f"{labels[verdict]} · 《{record['title']}》 · {plan['rel']}"
    return _capture_result(run, status, result, message, warnings, record["needs_retry"] or pending)


def retry_capture(cfg: KFConfig, source_id: str, profile: str = "low") -> dict:
    """Retry an existing receipt or partial archive using its original URL."""
    note = VaultIndex.build(cfg.vault).by_id.get(source_id)
    if note and note.meta.get("source_url"):
        return run_capture(cfg, note.meta["source_url"], kind="url", profile=profile)
    result = _capture_result(ids.run_id(), "failed", None,
                             "未找到可以重试的原始链接，请提供原文、截图或文件。", needs_input=True)
    result["errors"] = ["no original URL for this capture ID"]
    return result
