from pathlib import Path

import yaml

from knowledge_forge.discovery import Note, VaultIndex
from knowledge_forge.maps import library


def test_chinese_category_titles_have_distinct_schema_valid_ids():
    import re
    from knowledge_forge.ids import category_id

    titles = ['哲学 & 量子物理', '哲学', '量子物理']
    ids = {category_id(title) for title in titles}
    assert len(ids) == 3 and all(re.fullmatch(r'cat_[a-z0-9_]+', key) for key in ids)
    assert category_id('Research Methods') == 'cat_research_methods'


def test_briefing_bounded_and_skips_read_or_incomplete_notes():
    from knowledge_forge.maps import desk

    root = Path('/vault')
    guide = ('## Key takeaways\n\n- An evidence-backed takeaway. · [[#Source evidence 1|Evidence]]\n\n'
             '## What it means · Agent interpretation\n\nA practical next step.\n\n'
             '### Source evidence 1\n\n> A source quotation.')
    notes = []
    for i in range(9):
        meta = dict(note_type='source', capture_status='complete', analysis_status='ready',
                    title=f'Candidate {i}', reading_priority=i)
        notes.append(Note(root / f'10_sources/{i}.md', meta, guide))
    notes[5].body = guide.replace('### Source evidence 1', '### Different evidence')
    notes[6].meta['capture_status'] = 'usable_partial'
    notes[7].meta['reading_status'] = 'Reviewed'
    notes[8].meta.update(reading_status='Read deeply', read_minutes=15,
                         reading_start='Source evidence 1', reading_cue='Start with the quotation.',
                         reading_lesson='50_skills/Deep Reading/A lesson')
    lesson = Note(root / '50_skills/Deep Reading/A lesson.md', {'note_type': 'lesson'}, 'A lesson.')
    rendered = desk.build(root, VaultIndex(root, notes=[*notes, lesson],
                                           by_rel={'50_skills/Deep Reading/A lesson': lesson}))
    brief, deep = rendered.split('## Read deeply', 1)
    assert brief.count('**Takeaway**') == 3
    assert 'Candidate 4' in brief and 'Candidate 2' in brief and 'Candidate 1' not in brief
    assert all(f'Candidate {i}' not in brief for i in range(5, 9))
    assert '[[10_sources/4#Source evidence 1|Evidence]]' in brief
    assert '[[50_skills/Deep Reading/A lesson|Open lesson]]' in deep and '15 min' in deep
    assert '#Source evidence' not in deep


def test_reading_views_separate_progress_from_capture_status():
    views = {v['name']: v for v in yaml.safe_load(library.build())['views']}
    assert '!note.reading_status' in views['To Review']['filters']
    assert 'capture_status' not in views['To Review']['filters']
    assert 'reading_status == "Read deeply"' in views['Read Deeply']['filters']
    assert 'note.capture_status' in views['Capture Follow-Up']['filters']
    assert 'note.reading_status' not in views['Capture Follow-Up']['filters']


def test_live_briefing_uses_verified_takeaways_and_updates_from_properties():
    config = yaml.safe_load(library.build())
    views = {v['name']: v for v in config['views']}
    brief = views['Briefing']
    assert brief['limit'] == 3 and brief['type'] == 'list'
    assert 'note.reading_status == "To review"' in brief['filters']
    assert 'note.capture_status == "complete"' in brief['filters']
    assert 'note.analysis_status == "ready"' in brief['filters']
    assert 'note.reading_evidence' in brief['filters']
    assert 'note.reading_evidence' in config['formulas']['briefing_evidence']


def test_editorial_edition_has_its_own_view_and_deep_opens_lesson():
    config = yaml.safe_load(library.build())
    views = {v['name']: v for v in config['views']}
    assert views['Personal Brief']['limit'] == 1
    assert 'note.briefing_kind == "editorial"' in views['Personal Brief']['filters']
    assert 'note.reading_lesson' in config['formulas']['deep_start']
    assert 'note.reading_start' not in config['formulas']['deep_start']


def test_recapture_retains_reviewed_guide_and_reading_state():
    from types import SimpleNamespace
    from knowledge_forge.vault.writer import plan_source_filing

    root = Path('/vault')
    existing = Note(root / '10_sources/existing.md',
                    dict(id='src_existing', note_type='source', reading_status='Reviewed',
                         reading_guide_version=1, capture_status='complete', analysis_status='ready'),
                    'An evidence-checked guide.\n')
    vi = VaultIndex(root, notes=[existing], by_id={existing.note_id: existing})
    record = dict(id=existing.note_id, title='An article', platform='web', capture_status='complete',
                  capture_method='browser', content_hash='new-hash', captured_at='2026-11-25T18:00:00',
                  text='New article text.')
    plan = plan_source_filing(SimpleNamespace(vault=root), vi, record, analysis={'queued': True})
    assert plan['meta']['reading_status'] == 'Reviewed'
    assert plan['meta']['analysis_status'] == 'ready'
    assert plan['body'].startswith(existing.body)


def test_resource_review_is_based_on_archived_body_and_preserves_it(monkeypatch):
    import os
    from dataclasses import replace
    from tempfile import mkdtemp

    from knowledge_forge import frontmatter, pipeline
    from knowledge_forge.capture import references
    from knowledge_forge.config import load_config
    from knowledge_forge.maps import reading
    from knowledge_forge.vault.writer import stamp_vault

    root = Path(mkdtemp(prefix='resources-', dir=os.environ.get('KF_TEST_CASES')))
    cfg = replace(load_config(), vault=root / 'vault', state_home=root / 'state')
    stamp_vault(cfg)
    source = cfg.vault / '10_sources/example.md'
    source.parent.mkdir()
    repository = 'https://github.com/example/library'
    original = f'## Source text\n\nThe article cites {repository}.\n\n## My notes\n\nSee https://notes.example.org/a.\n'
    body = f'{reading.BEGIN}\n\n{reading.END}\n\n{reading.SAVED}\n\n{original}'
    source.write_text(frontmatter.dump(dict(id='src_resource', note_type='source', title='A source',
                                            capture_status='complete'), body, preserve_body=True))
    requests = []

    def inspect(url):
        requests.append(url)
        return references.ResourceEvidence(url, 'reviewed', 'Project', 'Saves reference notes.', url, 'README')

    monkeypatch.setattr(references, 'inspect_resource', inspect)
    result = pipeline.review_resources(cfg, 'src_resource')
    assert requests == [repository]
    review = cfg.vault / result['note']
    assert review.is_file() and repository in review.read_text()
    with review.open('a') as handle:
        handle.write('\n## My take\n\nCompare the export format.\n')
    assert pipeline.review_resources(cfg, '10_sources/example.md')['note'] == result['note']
    assert 'Compare the export format.' in review.read_text()
    metadata, after = frontmatter.read_note(source)
    assert '[[50_skills/Referenced Resources/' in after
    assert metadata['resource_review'] + '.md' == result['note']
    assert reading.original_body(after) == original


def test_attach_lesson_requires_a_real_evidence_heading():
    import os
    from dataclasses import replace
    from tempfile import mkdtemp

    import pytest
    from knowledge_forge import frontmatter, pipeline
    from knowledge_forge.config import load_config
    from knowledge_forge.vault.writer import stamp_vault

    root = Path(mkdtemp(prefix='lesson-', dir=os.environ.get('KF_TEST_CASES')))
    cfg = replace(load_config(), vault=root / 'vault', state_home=root / 'state')
    stamp_vault(cfg)
    source = cfg.vault / '10_sources/paper.md'
    source.parent.mkdir()
    original = '## Page 2\n\nThe paper reviews methods.\n'
    source.write_text(frontmatter.dump(dict(id='src_paper', note_type='source', title='A paper',
                                            capture_status='complete'), original, preserve_body=True))
    draft = root / 'lesson.md'
    lesson = '# How the method works\n\nStart from this concrete example. [[10_sources/paper#Page 3|Evidence]]\n'
    draft.write_text(lesson)
    with pytest.raises(ValueError, match='evidence'):
        pipeline.attach_lesson(cfg, 'src_paper', draft)
    draft.write_text('# How the method works\n\n[[10_sources/paper#Page 2')
    with pytest.raises(ValueError, match='evidence'):
        pipeline.attach_lesson(cfg, 'src_paper', draft)
    draft.write_text(lesson.replace('#Page 3', '#Page 2'))
    result = pipeline.attach_lesson(cfg, 'src_paper', draft)
    meta, body = frontmatter.read_note(source)
    assert meta['reading_lesson'] + '.md' == result['lesson']
    assert meta['reading_status'] == 'Read deeply' and body.endswith(original)
    assert 'Open lesson' in (cfg.vault / 'Reading Desk.md').read_text()


def test_follow_references_runs_only_after_primary_archive_is_committed(monkeypatch):
    import os
    from dataclasses import replace
    from tempfile import mkdtemp

    from knowledge_forge import pipeline
    from knowledge_forge.capture import references
    from knowledge_forge.config import load_config
    from knowledge_forge.vault.writer import stamp_vault

    root = Path(mkdtemp(prefix='follow-', dir=os.environ.get('KF_TEST_CASES')))
    cfg = replace(load_config(), vault=root / 'vault', state_home=root / 'state')
    stamp_vault(cfg)
    url = 'https://github.com/example/library'

    def inspect(target):
        assert target == url and list((cfg.vault / '10_sources').glob('*.md'))
        return references.ResourceEvidence(url, 'reviewed', 'Example', 'A text reference.', url, 'README')

    monkeypatch.setattr(references, 'inspect_resource', inspect)
    result = pipeline.run_capture(
        cfg, 'https://example.org/article', profile='offline', follow_references=True,
        _test_raw=dict(title='Article', text=f'The archive source discusses how to organize references. See {url}.',
                       body_verified=True, capture_method='direct'))
    assert result['status'] == 'completed'
    assert (cfg.vault / result['result']['resource_review']).is_file()


def test_browser_clip_import_is_local_conservative_and_idempotent():
    import os
    from dataclasses import replace
    from tempfile import mkdtemp

    from knowledge_forge import frontmatter, pipeline
    from knowledge_forge.config import load_config
    from knowledge_forge.vault.writer import stamp_vault

    root = Path(mkdtemp(prefix='clip-', dir=os.environ.get('KF_TEST_CASES')))
    cfg = replace(load_config(), vault=root / 'vault', state_home=root / 'state')
    stamp_vault(cfg)
    folder = cfg.dir_inbox / 'Web Clips'
    folder.mkdir(parents=True)
    clip = folder / 'An article.md'
    body = '# An article\n\n' + 'This passage describes a library with evidence-linked research. ' * 15 + '\n'
    clip.write_text('---\nnote_type: web_clip\nsource_url: https://example.org/a\nclip_scope: Unverified\n---\n\n' + body)
    result = pipeline.import_web_clips(cfg)
    assert not result['errors'] and len(result['filed']) == 1
    filed = result['filed'][0]
    assert filed['capture_status'] == 'usable_partial'
    note = cfg.vault / filed['note']
    assert frontmatter.read_note(note)[0]['reading_status'] == 'To review'
    assert frontmatter.read_note(clip)[1] == body
    assert pipeline.import_web_clips(cfg)['unchanged'] == 1
    clip.write_text(clip.read_text().replace('clip_scope: Unverified', 'clip_scope: Full article'))
    again = pipeline.import_web_clips(cfg)
    assert not again['errors'] and again['filed'][0]['capture_status'] == 'complete'
    assert again['filed'][0]['note'] == filed['note']
