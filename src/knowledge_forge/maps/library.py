"""Obsidian Bases views backed by plain source-note properties."""
from __future__ import annotations

import yaml


SOURCE = 'file.inFolder("10_sources") && note.note_type == "source"'
TO_REVIEW = f'{SOURCE} && (!note.reading_status || note.reading_status == "To review")'
DEEP = f'{SOURCE} && note.reading_status == "Read deeply"'
EDITORIAL = 'file.inFolder("60_briefings/editions") && note.note_type == "briefing" && note.briefing_kind == "editorial"'
CAPTURE_FOLLOW_UP = (
    '(file.inFolder("80_queues") && note.note_type == "quarantined_capture") || '
    '(' + SOURCE + ' && ((note.capture_status == "metadata_only" && !note.full_archive_path) || '
    'note.capture_status == "usable_partial" || note.capture_status == "failed"))'
)
REVIEW = (
    '(file.inFolder("80_queues") && note.note_type != "resolved_capture") || '
    '(file.inFolder("10_sources") && ((note.capture_status == "metadata_only" && !note.full_archive_path) || '
    'note.capture_status == "usable_partial" || '
    'note.analysis_status == "needs_review" || note.classification_status == "review"))'
)


def build():
    columns = ["formula.open_note", "note.topic", "note.content_kind", "note.source_platform",
               "note.reading_status", "note.capture_status", "note.captured_at"]
    common = {"type": "table", "order": columns, "filters": SOURCE,
              "sort": [{"property": "note.captured_at", "direction": "DESC"}]}
    by_priority = [{"property": "formula.priority", "direction": "DESC"}, *common["sort"]]
    list_view = {"type": "list", "markers": "none", "indentProperties": True, "limit": 3, "sort": by_priority}
    views = [
        {**common, "name": "To Review", "filters": TO_REVIEW,
         "order": ["formula.open_note", "note.reading_status", "note.reading_abstract", "note.capture_status"],
         "sort": by_priority},
        {**common, "name": "Read Deeply", "filters": DEEP,
         "order": ["formula.open_note", "formula.deep_start", "note.reading_status", "note.read_minutes", "note.reading_cue"]},
        {**list_view, "name": "Personal Brief", "limit": 1, "filters": EDITORIAL,
         "sort": [{"property": "note.edition_date", "direction": "DESC"},
                  {"property": "formula.edition_update", "direction": "DESC"}],
         "order": ["formula.open_note", "note.edition_deck"]},
        {**list_view, "name": "Briefing",
         "filters": f'{SOURCE} && note.reading_status == "To review" && '
                    'note.capture_status == "complete" && note.analysis_status == "ready" '
                    '&& note.reading_takeaway && note.reading_value && note.reading_evidence',
         "order": ["formula.open_note", "formula.takeaway", "formula.reading_value", "formula.briefing_evidence"]},
        {**list_view, "name": "Deep Reading Picks", "filters": DEEP,
         "order": ["formula.open_note", "formula.minutes", "note.reading_cue", "formula.deep_start"]},
        {**common, "name": "Reviewed", "filters": f'{SOURCE} && note.reading_status == "Reviewed"'},
        {**common, "name": "Reference", "filters": f'{SOURCE} && note.reading_status == "Reference"'},
        {**common, "name": "Recent", "limit": 12},
        {**common, "name": "All Sources"},
        {**common, "name": "By Topic", "groupBy": {"property": "note.topic", "direction": "ASC"}},
        *[{**common, "name": name, "filters": condition,
           "order": ["formula.open_note", "note.capture_status", "note.missing_elements",
                     "note.next_action", "note.classification_reason", "note.reason"]}
          for name, condition in [("Capture Follow-Up", CAPTURE_FOLLOW_UP), ("Needs Attention", REVIEW)]],
        {**common, "name": "Web Clips", "filters": 'file.inFolder("00_inbox/Web Clips") && note.note_type == "web_clip"',
         "order": ["formula.open_note", "note.clip_archive", "note.source_url"]},
    ]
    labels = {"formula.open_note": "Open note", "note.topic": "Topic", "note.content_kind": "Format",
              "note.source_platform": "Platform", "note.capture_status": "Capture",
              "note.captured_at": "Saved", "note.missing_elements": "Still needed",
              "note.next_action": "Next action", "note.reading_status": "Reading",
              "note.reading_abstract": "Abstract", "note.read_minutes": "Minutes",
              "note.reading_cue": "Starting point", "note.clip_archive": "Filed copy"}
    formulas = {"open_note": 'link(file.path, if(note.title, note.title, file.name))',
                "edition_update": 'if(note.edition_updated, note.edition_updated.toString(), "")',
                "priority": 'if(note.reading_priority, note.reading_priority, 0)',
                "takeaway": '"Takeaway · " + note.reading_takeaway',
                "reading_value": '"Why it matters · " + note.reading_value',
                "briefing_evidence": 'link(file.path + "#" + note.reading_evidence, "Evidence")',
                "minutes": 'if(note.read_minutes, note.read_minutes.toString() + " min")',
                "deep_start": 'if(note.reading_lesson, link(note.reading_lesson, "Open lesson"), "Lesson pending")'}
    return yaml.safe_dump({"formulas": formulas, "properties": {key: {"displayName": value}
                                         for key, value in labels.items()}, "views": views},
                          sort_keys=False, allow_unicode=True, width=120)
