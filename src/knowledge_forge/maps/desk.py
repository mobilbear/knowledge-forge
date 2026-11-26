"""A short briefing from reviewed guides and a queue picked by the reader."""
import re

from ..discovery import rel


def _section(body, heading):
    match = re.search(rf"(?m)^## (?:{heading})[ \t]*$", body)
    if not match:
        return ""
    return re.split(r"(?m)^#{1,6} ", body[match.end():], maxsplit=1)[0].strip()


def _has_heading(body, heading):
    return any(line.lstrip("# ") == heading for line in body.splitlines() if line.startswith("#"))


def briefing_fields(meta, body):
    if meta.get("capture_status") != "complete" or meta.get("analysis_status") != "ready":
        return {}
    takeaways = _section(body, "Key takeaways")
    relevance = _section(body, r"(?:What it means|Why this matters) · Agent interpretation")
    takeaway = re.search(r"(?m)^- (.+?) · \[\[#([^\]|]+)\|[^\]]+\]\]$", takeaways)
    if not (relevance and takeaway and _has_heading(body, takeaway[2])):
        return {}
    return {"reading_takeaway": takeaway[1], "reading_value": " ".join(relevance.split()),
            "reading_evidence": takeaway[2]}


def _briefing_card(vault, note):
    fields = briefing_fields(note.meta, note.body)
    if not fields:
        return []
    target = rel(vault, note.path)
    title = _title(note)
    return [f"### [[{target}|{title}]]", "", f"**Takeaway** {fields['reading_takeaway']}", "",
            f"**Why it matters** {fields['reading_value']}", "",
            f"[[{target}#{fields['reading_evidence']}|Evidence]] · [[{target}#Key takeaways|3-minute guide]]", ""]


def _title(note):
    return " ".join(str(note.meta.get("title") or note.path.stem).split()).replace("|", "/").replace("]", "")


def _priority(note):
    value = note.meta.get("reading_priority")
    return value if isinstance(value, int) else 0


def home_sections():
    return ["## 10-minute briefing", "", "![[Library.base#Personal Brief]]", "",
            "[[60_briefings/Current Brief|Past 7 days · Live briefing]]", "",
            "## Reading picks", "", "![[Library.base#Briefing]]", "",
            "[[Library.base#To Review|Open review queue]] · [[Reading Desk|Markdown reading desk]]", "",
            "## Read deeply", "", "![[Library.base#Deep Reading Picks]]", "",
            "[[Library.base#Read Deeply|Open deep-reading queue]]", "",
            "![[Reading Desk#Explore by question]]", "", "![[Reading Desk#Needs capture follow-up]]", ""]


def build(vault, vi):
    sources = sorted((note for note in vi.notes if note.note_type == "source"
                      and rel(vault, note.path).startswith("10_sources/")),
                     key=lambda n: (_priority(n), str(n.meta.get("captured_at") or ""), n.path.name),
                     reverse=True)
    lines = ["# Reading Desk", "", "[[HOME|Home]] · [[Knowledge Map|Knowledge map]]", ""]
    editions = [note for note in vi.notes if note.note_type == "briefing" and note.meta.get("briefing_kind") == "editorial"
                and rel(vault, note.path).startswith("60_briefings/editions/")]
    if editions:
        latest = max(editions, key=lambda n: (str(n.meta.get("edition_date") or ""),
                                              str(n.meta.get("edition_updated") or ""), n.path.name))
        lines += ["## 10-minute briefing", "", f"[[{rel(vault, latest.path)}|{_title(latest)}]]", ""]
    lines += ["[[60_briefings/Current Brief|Past 7 days · Live briefing]]", ""]
    lines += ["## Reading picks", ""]
    cards = []
    for note in sources:
        if (note.meta.get("reading_status") or "To review") == "To review":
            card = _briefing_card(vault, note)
            if card:
                cards.append(card)
                if len(cards) == 3:
                    break
    for card in cards:
        lines.extend(card)
    if not cards:
        lines += ["No evidence-linked guides ready for a briefing. Open the review queue to browse the library.", ""]
    progress = "[[Library.base#To Review|Open review queue]]"
    if (vault / "90_meta/Library Workflow.md").is_file():
        progress += " · [[90_meta/Library Workflow#Reading progress|Set reading progress]]"
    lines += [progress, "", "## Read deeply", ""]
    deep = [note for note in sources if note.meta.get("reading_status") == "Read deeply"]
    for note in deep[:3]:
        target = rel(vault, note.path)
        minutes = note.meta.get("read_minutes")
        lesson = note.meta.get("reading_lesson")
        cue = note.meta.get("reading_cue")
        parts = [f"[[{target}|{_title(note)}]]"]
        if isinstance(minutes, int) and minutes > 0:
            parts.append(f"{minutes} min")
        if isinstance(lesson, str) and lesson in vi.by_rel and vi.by_rel[lesson].note_type == "lesson":
            parts.append(f"[[{lesson}|Open lesson]]")
        else:
            parts.append("Lesson pending")
        if cue:
            parts.append(" ".join(str(cue).split()))
        lines.append("- " + " · ".join(parts))
    if not deep:
        lines.append("Set a source’s `reading_status` to `Read deeply` to save a spot here.")
    lines += ["", "[[Library.base#Read Deeply|Open deep-reading queue]]", "", "## Explore by question", ""]
    questions = [note for note in vi.notes if note.note_type == "concept" and note.meta.get("explore_question") is True]
    for note in sorted(questions, key=_title):
        lines.append(f"- [[{rel(vault, note.path)}|{_title(note)}]]")
    if not questions:
        lines.append("[[Knowledge Map|Browse the knowledge map]]")
    lines += ["", "## Needs capture follow-up", "",
              "[[Library.base#Capture Follow-Up|Missing text, transcripts or files]] · "
              "[[Library.base#Web Clips|Browser clips]]", ""]
    return "\n".join(lines)
