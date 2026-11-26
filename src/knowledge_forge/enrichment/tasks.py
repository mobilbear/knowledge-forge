"""Bounded model tasks with deterministic fallbacks.

Each task: build prompt → call_with_contract → apply result OR fall back.
Fallbacks never invent content; they degrade gracefully (extractive summary,
pending_classification queue).
"""
from __future__ import annotations

import re
from typing import Optional

from .. import ids
from ..quality.gate import transition
from ..taxonomy.registry import TaxonomyRegistry
from . import harness

PROMPT_VERSIONS = {"summarize": "v1", "classify": "v1", "quality_check": "v1",
                   "synthesize": "v1"}


# ---- deterministic fallbacks -------------------------------------------------------
def extractive_tldr(text: str, max_sentences: int = 4) -> str:
    """First substantive sentences — honest, never inventive."""
    sents = re.split(r"(?<=[.!?。！？])\s+", text.strip())
    picked = [s.strip() for s in sents if len(s.strip()) > 30][:max_sentences]
    return "\n".join(f"- {s[:240]}" for s in picked) or "- (no summarizable body text)"


def fallback_description(record: dict) -> str:
    t = (record.get("text") or "").strip().replace("\n", " ")
    return (t[:140] + "…") if len(t) > 140 else (t or record["title"])


# ---- enrichment (summary + tags) ------------------------------------------------------
def _source_text(record):
    return "\n\n".join(text for text in (record.get("text"), record.get("ocr_text")) if text)


def evidence_groups(text):
    """Number stable, bounded excerpts covering the start through the end."""
    parts = []
    location = ""
    for block in re.split(r"\n[ \t]*\n", text.strip()):
        page = re.match(r"(?m)^### Page \d+\s*", block)
        if page:
            location = page[0].strip()
            block = block[page.end():]
        elif block.startswith("## OCR"):
            location = block.splitlines()[0]
            block = block[len(location):]
        paragraphs = re.split(r"(?<=[.!?。！？])\s+", block)
        for paragraph in paragraphs:
            paragraph = paragraph.strip()
            for offset in range(0, len(paragraph), 640):
                value = paragraph[offset:offset + 640].strip()
                if len(value) >= 24:
                    parts.append(f"{location}\n{value}" if location else value)
    if len(parts) <= 10:
        return parts
    return [parts[i * (len(parts) - 1) // 9] for i in range(10)]


def excerpt_points(groups):
    if not groups:
        return []
    count = min(len(groups), 4)
    points, seen = [], set()
    for i in range(count):
        group = groups[i * (len(groups) - 1) // max(1, count - 1)]
        quote = " ".join(group.split())[:230]
        if quote not in seen:
            points.append({"point": quote, "quote": quote, "location": _location(group)})
            seen.add(quote)
    return points


def _location(group):
    page = re.search(r"(?m)^### Page (\d+)", group)
    if page:
        return f"[[#Page {page[1]}|Page {page[1]}]]"
    if group.startswith("## OCR"):
        return "[[#OCR (from images)|Read OCR text]]"
    time = re.search(r"\[(\d{2}:\d{2}:\d{2})\]", group)
    if time:
        return f"{time[1]} · [[#Source text|Read transcript]]"
    return "[[#Source text|Read archived text]]"


def enrich(record: dict, provider: harness.Provider, user_context=None) -> dict:
    """Produce evidence-checked highlights and a labeled interpretation."""
    version = PROMPT_VERSIONS["summarize"]
    text = _source_text(record)
    groups = evidence_groups(text if record.get("text") else "## OCR (from images)\n\n" + text)
    context = "\n\n".join(f"[{n}] {value}" for n, value in enumerate(groups, 1))
    prompt = harness.load_prompt("summarize", version).format(
        title=record["title"], lang=record.get("lang", "en"),
        untrusted=harness.wrap_untrusted(context, limit=7000),
        user_context=harness.wrap_untrusted(user_context or "", limit=700),
    )
    res = None
    try:
        res = harness.call_with_contract(provider, prompt, "enrichment", max_tokens=1600)
    except harness.ModelUnavailable:
        pass
    transition(record, "enriched")
    if res and res.ok and not res.data.get("abstained"):
        points = []
        for item in res.data.get("key_points", []):
            index, quote = item["evidence"], " ".join(item["quote"].split())
            if 1 <= index <= len(groups) and quote in " ".join(groups[index - 1].split()):
                points.append({"point": item["point"], "quote": quote,
                               "location": _location(groups[index - 1])})
        why = res.data.get("why_it_matters") if points else None
        return {"tldr": res.data["tldr"], "description": res.data["description"],
                "tag_proposals": [ids.normalize_tag(t) for t in res.data["tag_proposals"]],
                "points": points or excerpt_points(groups), "why": why,
                "via_model": bool(points), "queued": not (points and why),
                "prompt_version": version}
    return {"tldr": extractive_tldr(text), "description": fallback_description(record),
            "tag_proposals": [], "points": excerpt_points(groups), "why": None,
            "via_model": False, "queued": True, "prompt_version": version}


# ---- candidate-first classification ---------------------------------------------------
def classify(record: dict, tags: list[str], registry: TaxonomyRegistry,
             provider: harness.Provider) -> dict:
    """Returns {primary_category_id|None, secondary_category_ids, pending, reason}.
    Software retrieves candidates; the model may only select among them or say
    none_of_the_above. Anything uncertain lands in pending_classification."""
    text = _source_text(record)
    cands = registry.candidates_for(record["title"] + "\n" + "\n".join(evidence_groups(text)), tags)
    transition_target = "classified"
    if not cands:
        transition(record, transition_target)
        return {"primary_category_id": None, "secondary_category_ids": [],
                "pending": True, "reason": "no plausible candidates in registry"}

    cand_block = "\n".join(
        f"- id: {c['category_id']} | title: {c['title']} | {c.get('description','')[:120]}"
        for c in cands)
    version = PROMPT_VERSIONS["classify"]
    prompt = harness.load_prompt("classify", version).format(
        title=record["title"], candidates=cand_block,
        untrusted=harness.wrap_untrusted("\n".join(value[:280] for value in evidence_groups(text)), limit=3000),
    )
    try:
        res = harness.call_with_contract(provider, prompt, "classification", max_tokens=500)
    except harness.ModelUnavailable:
        transition(record, transition_target)
        return {"primary_category_id": None, "secondary_category_ids": [],
                "pending": True, "reason": "no model available; queued for classification"}

    allowed = {c["category_id"] for c in cands}
    if res.ok:
        d = res.data
        cid = d.get("primary_category_id")
        conf = d.get("confidence", 0)
        if (d["candidate_decision"] == "selected" and cid in allowed
                and conf >= registry.policy["min_classification_confidence"]):
            secondaries = [s for s in (d.get("secondary_category_ids") or []) if s in allowed][:3]
            transition(record, transition_target)
            return {"primary_category_id": cid, "secondary_category_ids": secondaries,
                    "pending": False, "confidence": conf,
                    "reason": d.get("reason_short", ""), "repaired": res.repaired}
        # selection outside the candidate set is a structural violation → pending, logged
        reason = ("model selected a non-candidate id" if cid and cid not in allowed
                  else d.get("reason_short") or d["candidate_decision"])
    else:
        reason = f"invalid model output after repair: {res.errors[:3]}"
    transition(record, transition_target)
    return {"primary_category_id": None, "secondary_category_ids": [],
            "pending": True, "reason": reason}
