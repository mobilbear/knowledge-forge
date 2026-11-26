import json
import os
from dataclasses import replace
from datetime import date
from pathlib import Path
from tempfile import mkdtemp

import pytest

from knowledge_forge import pipeline
from knowledge_forge.briefing import live
from knowledge_forge.config import load_config
from knowledge_forge.discovery import Note, VaultIndex
from knowledge_forge.frontmatter import read_note
from knowledge_forge.vault.writer import stamp_vault


def test_capture_and_checked_guide_refresh_the_live_brief_and_accept_an_edition():
    root = Path(mkdtemp(prefix='live-brief-', dir=os.environ.get('KF_TEST_CASES')))
    cfg = replace(load_config(), vault=root / 'vault', state_home=root / 'state')
    stamp_vault(cfg)
    evidence = 'Check that the cited resources can still be retrieved before filing them.'
    capture = pipeline.run_capture(cfg, 'https://example.org/how-to-file', profile='offline', _test_raw={
        'title': 'Checking references', 'text': evidence, 'body_verified': True, 'capture_method': 'direct',
    })
    assert capture['status'] == 'completed'
    relative = capture['result']['note']
    target = relative.removesuffix('.md')
    brief = cfg.vault / '60_briefings/Current Brief.md'
    assert 'Checking references' in brief.read_text()
    assert 'Source excerpt' in brief.read_text()
    draft = root / 'guide.json'
    draft.write_text(json.dumps({Path(relative).name: {
        'abstract': 'This workflow checks whether references can still be retrieved.',
        'meaning': 'Try this when returning to an older note.',
        'limitation': 'The note does not measure accuracy.',
        'points': [{'point': 'Verify a reference before filing.', 'quote': evidence}],
    }}))
    assert pipeline.apply_reading_guides(cfg, [draft]) == 1
    text = brief.read_text()
    assert 'Verify a reference before filing.' in text and 'Try this when returning to an older note.' in text
    assert f'[[{target}#Source evidence 1|Evidence]]' in text
    draft_issue = root / 'edition.md'
    draft_issue.write_text(f'# Finding evidence again\n\nA new story. [[{target}#Made up|Evidence]]\n')
    with pytest.raises(ValueError, match='evidence'):
        pipeline.publish_edition(cfg, relative, draft_issue)
    draft_issue.write_text(f'# Finding evidence again\n\n```text\n[[{target}#Source evidence 1]]\n```\n')
    with pytest.raises(ValueError, match='evidence'):
        pipeline.publish_edition(cfg, relative, draft_issue)
    draft_issue.write_text(f'# Finding evidence again\n\nA new story. [[{target}#Source evidence 1|Evidence]]\n')
    result = pipeline.publish_edition(cfg, relative, draft_issue)
    edition = cfg.vault / result['edition']
    assert read_note(edition)[0]['briefing_kind'] == 'editorial'
    assert pipeline.publish_edition(cfg, relative, draft_issue)['edition'] == result['edition']


def test_live_brief_limits_coverage_and_keeps_newest_version():
    vault = Path('/vault')
    notes = []
    for i in range(12):
        meta = dict(note_type='source', title=f'Article {i:02d}', capture_status='complete',
                    captured_at=f'2026-11-26T12:00:{i:02d}', canonical_url=f'https://example.org/{i}')
        notes.append(Note(vault / f'10_sources/{i}.md', meta, '## Source text\n\nAn archived paragraph.'))
    notes[0].meta.update(title='Re-captured article', captured_at='2026-08-01', last_captured_at='2026-11-26T14:00:00')
    notes[0].body = ('## Source text\n\nOld finding.\n\n## Updated capture (2026-11-26) · Version 2'
                     '\n\n## Source text\n\nNew finding.')
    notes[1].meta.update(captured_at='2026-11-10')
    notes[2].meta.update(canonical_url='https://example.org/0')
    notes[11].meta.update(capture_status='usable_partial')
    notes.append(Note(vault / '10_sources/failure.md', dict(note_type='source', title='Blocked article',
                       captured_at='2026-11-26T14:00:00', capture_status='metadata_only'), 'No body.'))
    rendered = live.build(vault, VaultIndex(vault, notes=notes), date(2026, 11, 26))
    assert rendered.count('### [[') == 10
    assert 'Re-captured article' in rendered and 'Article 02' not in rendered
    assert 'Article 01' not in rendered and 'Blocked article' not in rendered
    assert 'Partial archive' in rendered and 'Source excerpt' in rendered
    assert 'New finding.' in rendered and 'Old finding.' not in rendered
    assert '[[10_sources/0#Updated capture (2026-11-26) · Version 2|Captured text]]' in rendered
