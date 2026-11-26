"""Evidence-linked rolling coverage for newly saved or re-captured material."""
from collections import defaultdict
from datetime import date, timedelta
import re

from ..discovery import rel
from ..maps import brain, desk
from ..vault.writer import literal_source, render


PATH = "60_briefings/Current Brief.md"


def _captured(note):
    return str(note.meta.get("last_captured_at") or note.meta.get("captured_at") or "")


def _excerpt(note):
    match = re.search(r"(?m)^- (.+?) · \[\[#(Evidence \d+)\|[^\]]+\]\]$", note.body)
    if match:
        return match[1], match[2]
    for heading in ("Source text", "OCR (from images)", "Saved source and original notes"):
        section = desk._section(note.body, re.escape(heading))
        if section:
            return literal_source(" ".join(section.split())[:240]), heading
    return "", ""


def build(vault, index, today=None):
    today = today or date.today()
    since = today - timedelta(days=6)
    candidates = sorted((note for note in index.notes if note.note_type == "source"
                         and note.meta.get("capture_status") in ("complete", "usable_partial")
                         and rel(vault, note.path).startswith("10_sources/")
                         and since.isoformat() <= _captured(note)[:10] <= today.isoformat()),
                        key=lambda note: (_captured(note), note.path.name), reverse=True)
    topics, seen = defaultdict(list), set()
    for note in candidates:
        key = note.meta.get("canonical_url") or str(note.path)
        if key in seen:
            continue
        seen.add(key)
        topics[str(note.meta.get("topic") or "Recently saved")].append(note)
        if len(seen) == 10:
            break

    count = "1 source" if len(seen) == 1 else f"{len(seen)} sources"
    body = ["# LiangWiki · Live briefing", "", "[[HOME|Home]] · [[Library.base#Personal Brief|Personal editions]]",
            "", f"{since.isoformat()} – {today.isoformat()} · {count}", ""]
    if not seen:
        body.extend(["No usable sources were saved during this period.", ""])
    for topic, notes in topics.items():
        body.extend([f"## {literal_source(str(topic))}", ""])
        for note in notes:
            target = rel(vault, note.path)
            body += [f"### {brain.wiki(target, brain.title(note))}", ""]
            if note.meta["capture_status"] == "usable_partial":
                body += ["**Coverage** Partial archive.", ""]
            fields = desk.briefing_fields(note.meta, note.body)
            if fields:
                body += [f"**Takeaway** {fields['reading_takeaway']}", "",
                         f"**Why it matters** {fields['reading_value']}", "",
                         f"[[{target}#{fields['reading_evidence']}|Evidence]]", ""]
            else:
                excerpt, anchor = _excerpt(note)
                if excerpt:
                    body += [f"**Source excerpt** {excerpt} · [[{target}#{anchor}|Captured text]]", ""]
    return render({"id": "brf_live_current", "note_type": "briefing", "title": "LiangWiki · Live briefing",
                   "briefing_kind": "live", "updated": today.isoformat()}, "\n".join(body))
