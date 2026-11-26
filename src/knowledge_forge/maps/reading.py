"""Evidence-checked reading guides and the portable Markdown catalog."""
from collections import Counter, defaultdict
import re

from ..discovery import rel
from ..urlnorm import canonicalize
from ..vault.writer import literal_source
from . import brain, desk

BEGIN = "<!-- liangwiki:reading-guide -->"
END = "<!-- /liangwiki:reading-guide -->"
CONNECTION_BEGIN = "<!-- liangwiki:reading-connection -->"
CONNECTION_END = "<!-- /liangwiki:reading-connection -->"
SAVED = "## Saved source and original notes"
STATUS = {"complete": "Full archive", "usable_partial": "Partial archive",
          "metadata_only": "Metadata only", "failed": "Needs capture"}


def _compact(value):
    return " ".join(value.split())


def _field(card, name):
    value = card.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Reading guide requires {name}")
    return literal_source(_compact(value))


def layer_links(has_takeaways):
    links = ["[[#Abstract|30-second read]]"]
    if has_takeaways:
        links.append("[[#Key takeaways|3-minute guide]]")
    links.append("[[#What it means · Agent interpretation|What it means]]")
    return " · ".join(links)


def original_body(body):
    if not body.startswith(BEGIN):
        return body
    _, separator, original = body.partition(END + "\n\n" + SAVED + "\n\n")
    if not separator:
        raise ValueError("existing reading guide is missing its end marker")
    return original


def _link(target, name):
    label = _compact(name).replace("]", "").replace("|", "/")
    return f"[[{target}|{label}]]"


def companion_link(meta, field, label):
    path = meta.get(field)
    return _link(path, label) if isinstance(path, str) and path.startswith("50_skills/") else ""


def _topic_link(registry, cid):
    route = registry.path_of(cid) if cid else []
    if not route:
        return "Unclassified · [[Library Index#To classify|Review topic]]"
    parts = []
    for index, node in enumerate(route, 1):
        folder = "/".join(item["category_id"] for item in route[:index])
        parts.append(_link(f"40_maps/{folder}/_index", node["title"]))
    return " / ".join(parts)


def _stable_url(note):
    url = note.meta.get("canonical_url") or note.meta.get("source_url") or note.meta.get("url")
    return canonicalize(url) if url else None


def _archive_evidence(body, title, metadata_only):
    body = re.split(r"(?im)^#{1,3} (?:my notes|personal notes|agent interpretation)\s*$",
                    body, maxsplit=1)[0]
    start = re.search(r"(?im)^#{2,3} (?:source text|original article|raw source|full text|transcript|OCR \(from images\))\s*$", body)
    if start and not metadata_only:
        return _compact(body[start.end():])
    return _compact(body + "\n" + title)


def render_card(vault, note, card, registry, vi):
    """Checks every quoted excerpt and keeps the original note body as a suffix."""
    body = original_body(note.body)
    source_evidence = _archive_evidence(body, str(note.meta.get("title") or ""),
                                        card.get("coverage") == "metadata_only")
    metadata = dict(note.meta)
    citation = card.get("full_sibling")
    sibling = None
    if citation:
        if "/" in citation or not citation.endswith(".md"):
            raise ValueError("invalid full archive reference")
        sibling = vi.by_rel.get("10_sources/" + citation[:-3])
        if (not sibling or sibling.path == note.path or not _stable_url(note)
                or _stable_url(note) != _stable_url(sibling)):
            raise ValueError("full archive reference is not a matching source")
        metadata["full_archive_path"] = rel(vault, sibling.path)
    coverage = card.get("coverage")
    if coverage == "partial":
        if not (metadata.get("content_kind") == "document" and metadata.get("attachments")):
            metadata["capture_status"] = "usable_partial"
    elif coverage == "metadata_only":
        metadata["capture_status"] = "metadata_only"
    elif coverage is not None:
        raise ValueError(f"unsupported coverage {coverage}")
    candidate = card.get("topic")
    if candidate and not registry.get(candidate):
        raise ValueError(f"unknown topic {candidate}")
    cid = metadata.get("primary_category_id") or candidate
    category = registry.get(cid) if cid else None
    metadata.update(
        note_type="source", primary_category_id=cid, topic=category["title"] if category else "Unclassified",
        classification_status="classified" if category else "review",
        classification_basis=(note.meta.get("classification_basis") or "existing category"
                              if note.meta.get("primary_category_id")
                              else "agent-reviewed reading guide" if category else "needs a topic review"),
        reading_abstract=_field(card, "abstract"), analysis_status="ready", reading_guide_version=1,
    )
    metadata["topic_path"] = [item["title"] for item in registry.path_of(cid)] if category else []
    platform = metadata.get("source_platform") or metadata.get("platform") or "web"
    metadata["source_platform"] = platform
    if "content_kind" not in metadata:
        metadata["content_kind"] = (
            "transcript" if metadata.get("capture_method") == "transcript" else
            "image post" if platform == "xiaohongshu" else
            "abstract" if platform == "arxiv" and coverage == "partial" else
            "document" if platform == "arxiv" else "article")
    status = STATUS.get(metadata.get("capture_status"), "Coverage needs review")
    pdf = next((path for path in metadata.get("attachments") or [] if path.endswith(".pdf")), None)
    if sibling:
        sibling_label = ("Open matching full archive" if sibling.meta.get("capture_status") == "complete"
                         else "Open matching saved text")
        jump = _link(metadata["full_archive_path"], sibling_label)
    elif pdf:
        jump = _link(pdf, "Open original PDF")
    else:
        heading = re.search(r"(?im)^#{2,3} ((?:source text|original article|raw source|full text|transcript|page 1)[^\n]*)$", body)
        display = ("Read full saved source" if metadata.get("capture_status") == "complete"
                   else "Read what was saved")
        jump = _link("#" + (heading[1].strip() if heading else SAVED[3:]), display)
    points = card.get("points")
    if not isinstance(points, list) or len(points) > 5:
        raise ValueError("Reading guide requires zero to five points")
    companions = [companion_link(metadata, "reading_lesson", "Open the guided lesson"),
                  companion_link(metadata, "resource_review", "Referenced resource briefing")]
    lines = [BEGIN, f"[[HOME|Home]] / [[Library Index|Library]] / {_topic_link(registry, cid)}",
             "", f"**{status}** · {jump}", "", layer_links(bool(points)), ""]
    if any(companions):
        lines += [" · ".join(link for link in companions if link), ""]
    lines += ["## Abstract", "", metadata["reading_abstract"]]
    if points:
        lines += ["", "## Key takeaways", ""]
        evidence = []
        for i, point in enumerate(points, 1):
            quote = point.get("quote")
            if not isinstance(quote, str) or not 5 <= len(_compact(quote)) <= 300:
                raise ValueError("unusable supporting quotation")
            if _compact(quote) not in source_evidence:
                raise ValueError("supporting quotation was not found in the archived note")
            lines.append(f"- {_field(point, 'point')} · [[#Source evidence {i}|Evidence]]")
            evidence += [f"### Source evidence {i}", "", f"> {literal_source(_compact(quote))}", ""]
    else:
        evidence = []
    lines += ["", "## What it means · Agent interpretation", "", _field(card, "meaning"),
              "", "## Limits and open questions", "", _field(card, "limitation")]
    guide = note.body.partition(END)[0] if note.body.startswith(BEGIN) else ""
    _, marker, tail = guide.partition(CONNECTION_BEGIN)
    connection, end, _ = tail.partition(CONNECTION_END)
    if marker and end:
        lines += ["", marker + connection + end]
    related = []
    current_path = rel(vault, note.path)
    for other in vi.notes:
        if other.note_type != "concept":
            continue
        if any(vi.resolve_link(target)[0] == current_path for target in vi.outbound_links(other)):
            related.append(_link(rel(vault, other.path), other.meta.get("title") or other.path.stem))
    if related:
        lines += ["", "## Related synthesis", "", *[f"- {link}" for link in related[:5]]]
    attachments = metadata.get("attachments") or []
    if attachments:
        lines += ["", "## Saved files", "", *[f"- {_link(path, path.rsplit('/', 1)[1])}" for path in attachments]]
    if evidence:
        lines += ["", "## Evidence from the saved source", "", *evidence]
    lines += [jump, "", END, "", SAVED, "", body]
    body = "\n".join(lines)
    for field in ("reading_takeaway", "reading_value", "reading_evidence"):
        metadata.pop(field, None)
    metadata.update(desk.briefing_fields(metadata, body))
    return metadata, body


def _catalog_entry(vault, note):
    title = note.meta.get("title") or note.path.stem
    summary = note.meta.get("reading_abstract") or note.meta.get("description") or ""
    status = STATUS.get(note.meta.get("capture_status"), "Coverage needs review")
    return f"- {_link(rel(vault, note.path), title)} · {status}" + (
        f" · {_compact(str(summary))[:180]}" if summary else "")


def catalog(vault, vi, registry):
    groups = defaultdict(list)
    concepts = []
    for note in vi.notes:
        if note.note_type == "source" and rel(vault, note.path).startswith("10_sources/"):
            groups[note.meta.get("primary_category_id") or ""].append(note)
        if note.note_type in ("concept", "entity") and rel(vault, note.path).startswith(("20_concepts/", "30_entities/")):
            concepts.append(note)
    index = ["# Library Index", "", "[[HOME|Home]] · [[Reading Desk|Reading Desk]] · [[Knowledge Map|Knowledge Map]]", ""]
    for node in registry.active():
        assigned = groups.get(node["category_id"], [])
        if assigned:
            index += [f"## {node['title']}", "", _topic_link(registry, node["category_id"]), ""]
            index.extend(_catalog_entry(vault, item) for item in sorted(assigned, key=lambda n: n.path.name))
            index.append("")
    active_ids = {node["category_id"] for node in registry.active()}
    unassigned = [note for key, notes in groups.items() if key not in active_ids for note in notes]
    index += ["## To classify", ""]
    index += [_catalog_entry(vault, note) for note in unassigned] or ["No unclassified sources."]
    index += ["", "## Concepts and synthesis", ""]
    index.extend(f"- {_link(rel(vault, note.path), note.meta.get('title') or note.path.stem)}"
                 for note in concepts)
    return "\n".join(index).rstrip() + "\n"


def overview(vault, vi, registry):
    counts = Counter()
    for note in vi.notes:
        if note.note_type == "source" and rel(vault, note.path).startswith("10_sources/"):
            category = note.meta.get("primary_category_id")
            for parent in registry.path_of(category) if category else []:
                counts[parent["category_id"]] += 1
    lines = ["# Knowledge Map", "", "[[HOME|Home]] · [[LiangWiki Brain.canvas|Open Brain graph]] · [[Library Index|Full catalog]]"]
    if any(note.meta.get("explore_question") is True for note in vi.notes):
        lines += ["", "[[Reading Desk#Explore by question|Start with a question]]"]
    if (vault / "90_meta/Library Workflow.md").is_file():
        lines += ["", "[[90_meta/Library Workflow|How this library is maintained]]"]
    lines += ["", "## Domains and topics", ""]
    for node in registry.active():
        cid = node["category_id"]
        path = "/".join(n["category_id"] for n in registry.path_of(cid))
        lines.append("  " * (node["level"] - 1) +
                     f"- {_link(f'40_maps/{path}/_index', node['title'])} · {counts[cid]} sources")
    lines += ["", "## Expandable branches", "", *brain.outline(vault, vi, registry),
              "", "## Concepts that connect sources", ""]
    for note in vi.notes:
        if note.note_type == "concept" and rel(vault, note.path).startswith("20_concepts/"):
            lines.append(f"- {_link(rel(vault, note.path), note.meta.get('title') or note.path.stem)}")
    return "\n".join(lines) + "\n"
