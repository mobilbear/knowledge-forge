from knowledge_forge.capture import adapters
from knowledge_forge.normalize.record import build_record
from knowledge_forge.pipeline import _quality_precheck
from knowledge_forge.quality import gate
from knowledge_forge.urlnorm import canonicalize, platform_of
from knowledge_forge.vault.writer import build_source_note


def grade(raw, url):
    record = build_record(raw, url, "run_pilot")
    gate.transition(record, "capturing")
    gate.transition(record, "normalized")
    return gate.assess(record)


def test_long_arxiv_abstract_does_not_pass_as_full_paper():
    raw = dict(platform="arxiv", title="Paper", capture_method="api",
               text="The authors investigated research methods. " * 20)
    assert not _quality_precheck(raw)
    assert grade(raw, "https://arxiv.org/abs/2501.00002")["capture_status"] == "usable_partial"


def test_short_verified_article_can_be_complete():
    raw = dict(platform="substack", title="Announcement", capture_method="direct",
               text="The meeting is now scheduled for Thursday.", body_verified=True)
    assert _quality_precheck(raw)
    assert grade(raw, "https://example.substack.com/p/test")["capture_status"] == "complete"


def test_article_teaser_does_not_stop_the_cascade():
    raw = dict(platform="substack", title="Newsletter", capture_method="direct",
               text="A teaser with relevant information. " * 22 + " Continue reading")
    assert not _quality_precheck(raw)
    assert grade(raw, "https://example.substack.com/p/test")["capture_status"] == "usable_partial"


def test_bilibili_spoken_transcript_is_valid_without_images():
    raw = dict(platform="bilibili", title="讲座", capture_method="transcript",
               text="字幕保留了讲座的具体研究内容和结论。" * 40)
    assert grade(raw, "https://www.bilibili.com/video/BVx")["capture_status"] == "complete"


def test_mobile_video_identity():
    assert platform_of("https://b23.tv/demo") == "bilibili"
    assert canonicalize("https://youtu.be/a1?si=abc") == canonicalize("https://m.youtube.com/watch?v=a1")
    assert canonicalize("https://m.bilibili.com/video/BV123?p=2&spm_id_from=app") == (
        "https://bilibili.com/video/BV123?p=2")


def test_caption_cues_preserve_time_and_later_repetition():
    vtt = ("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHello\n\n"
           "00:01:01.000 --> 00:01:02.000\nHello\n")
    result = adapters._vtt_to_text(vtt)
    assert "00:00:01" in result and "00:01:01" in result
    assert result.count("Hello") == 2


def test_note_exposes_original_source_and_evidence():
    raw = dict(platform="manual", title="Paper", capture_method="manual",
               content_kind="document", text="The measured results are useful. " * 24)
    record = grade(raw, None)
    record["attachments"] = ["_attachments/library/src_42/paper.pdf"]
    _, body = build_source_note(record, tldr="Short summary")
    assert "[[#" in body and "[[_attachments/library/src_42/paper.pdf|" in body
    assert "## Source text" in body


def test_document_can_discuss_a_login_screen_without_being_rejected():
    raw = dict(platform="arxiv", title="Paper on authentication", capture_method="direct",
               content_kind="document", body_verified=True,
               text="### Page 1\n\nThe study examined CAPTCHA prompts and user experience.")
    assert grade(raw, "https://arxiv.org/pdf/2501.00002")["capture_status"] == "complete"


def test_interpretation_checks_quote_and_links_pdf_page():
    import json
    from knowledge_forge.enrichment import harness, tasks

    text = "A reader completed the experiment faster after reducing interruptions."
    response = dict(tldr="Fewer interruptions helped.", description="A study.",
                    tag_proposals=["research"], abstained=False,
                    key_points=[{"point": "Removing interruptions helped.",
                                 "quote": text, "evidence": 1}],
                    why_it_matters="Use this when designing a focused reading session.")
    data = {"state": "usable", "title": "Study", "lang": "en", "text": f"### Page 4\n\n{text}"}
    accepted = tasks.enrich(data.copy(), harness.MockProvider([json.dumps(response)]))
    assert accepted["why"] and accepted["points"][0]["location"] == "[[#Page 4|Page 4]]"
    response["key_points"][0]["quote"] = "An unsupported exact quote."
    rejected = tasks.enrich(data.copy(), harness.MockProvider([json.dumps(response)]))
    assert rejected["queued"] and rejected["why"] is None


def test_archive_receipts_and_search_remain_consistent():
    import os
    from pathlib import Path
    from tempfile import mkdtemp
    from dataclasses import replace

    from knowledge_forge import pipeline
    from knowledge_forge.capture import jobs
    from knowledge_forge.config import load_config
    from knowledge_forge.discovery import VaultIndex
    from knowledge_forge.index import search
    from knowledge_forge.vault.writer import stamp_vault

    parent = os.environ.get("KF_TEST_CASES")
    if not parent:
        raise AssertionError("set KF_TEST_CASES to a directory for retained cases")
    root = Path(mkdtemp(prefix="archive-", dir=parent))
    cfg = replace(load_config(), vault=root / "vault", state_home=root / "state", raw={})
    stamp_vault(cfg)
    url = "https://www.youtube.com/watch?v=DemoCase"
    receipt = jobs.submit(cfg, "Forwarded " + url)
    assert jobs.submit(cfg, "https://youtu.be/DemoCase?si=tracking")["receipt"] == receipt["receipt"]

    def fail(cfg, url, **kwargs):
        return pipeline.run_capture(cfg, url, profile="offline",
                                    _test_raw={"platform": "youtube", "title": "Video",
                                               "capture_method": "none", "adapter_error": "restricted"})
    first = jobs.process(cfg, fail, "offline", receipt["receipt"])
    assert first["status"] == "needs_input"
    prior_path = cfg.vault / first["result_path"]
    assert prior_path.is_file()

    text = "- [00:01:02] A specific marker, 深度知识检索, appears in the original."
    payload = dict(platform="youtube", title="Video", capture_method="transcript",
                   text=text, body_verified=True, artifacts=[("captions-en.vtt", b"WEBVTT\n")])

    def succeed(cfg, url, **kwargs):
        return pipeline.run_capture(cfg, url, profile="offline", _test_raw=payload)
    assert jobs.submit(cfg, "Forwarded https://youtu.be/DemoCase")["status"] == "ready"
    done = jobs.process(cfg, succeed, "offline", receipt["receipt"])
    assert done["status"] == "archived" and done["source_id"] == first["source_id"]
    path = cfg.vault / done["result_path"]
    assert path.is_file() and prior_path.is_file()
    assert "resolved_capture" in prior_path.read_text()
    assert len(list(cfg.vault.rglob("*.vtt"))) == 1
    assert succeed(cfg, url)["status"] == "noop"
    assert search.query(cfg, "深度知识检索", n=2)[0]["section"] == "Source text"
    partial = pipeline.run_capture(
        cfg, url, profile="offline", _test_raw={
            **payload, "partial_reason": "missing final segment",
            "artifacts": [("captions-partial.vtt", b"WEBVTT\noriginal partial track\n")],
        })
    assert partial["status"] == "queued"
    assert (cfg.vault / partial["result"]["attachments"][0]).is_file()
    assert "capture_status: complete" in path.read_text()
    assert (cfg.vault / "Library.base").is_file()
    assert not VaultIndex.build(cfg.vault).duplicate_ids


def test_rednote_uses_only_the_requested_item_and_keeps_image_order():
    import json

    images = [{"urlDefault": "https://example.xhscdn.com/first.jpg"},
              {"urlDefault": "https://example.xhscdn.com/second.jpg"}]
    data = {"note": {"noteDetailMap": {
        "target": {"note": {"type": "normal", "imageList": images}},
        "unrelated": {"note": {"type": "normal", "imageList": list(reversed(images))}},
    }}}
    html = "<script>window.__INITIAL_STATE__=" + json.dumps(data) + "</script>"
    found = adapters._rednote_image_set(html, "https://www.xiaohongshu.com/explore/target")
    assert found[1] == images
    assert adapters._rednote_image_set(html, "https://www.xiaohongshu.com/login") is None


def test_mobile_rednote_state_preserves_literal_undefined_and_checks_note_id():
    import json
    assert platform_of("https://xhslink.cn/o/demo") == "xiaohongshu"
    note = {"noteId": "Abcd", "title": "The word undefined", "imageList": [{"url": "https://img.xhscdn.com/x.jpg"}]}
    state = json.dumps({"noteData": {"data": {"noteData": note}}, "placeholder": None})
    html = "<script>window.__INITIAL_STATE__=" + state.replace(": null", ": undefined") + "</script>"
    result = adapters._rednote_image_set(html, "https://www.xiaohongshu.com/discovery/item/Abcd")
    assert result[0]["title"] == "The word undefined"
    assert adapters._rednote_image_set(html, "https://www.xiaohongshu.com/discovery/item/Wrong") is None


def test_wechat_parser_selects_nested_article_not_a_challenge():
    sample = """<html><head><meta property="og:title" content="Reading note"></head>
    <body><div id="js_content"><section><h2>Paper title</h2><p>Study finds an effect.</p>
    <img data-src="https://mmbiz.qpic.cn/demo/image"></section></div><footer>Publisher footer</footer></body></html>"""
    article = adapters.parse_wechat_html(sample)
    assert "Study finds an effect." in article["text"] and "Publisher footer" not in article["text"]
    assert article["body_verified"] and article["image_urls"] == ["https://mmbiz.qpic.cn/demo/image"]
    blocked = adapters.parse_wechat_html("<title>环境异常</title><p>请完成验证</p>")
    assert "verification" in blocked["adapter_error"] and not blocked["text"]


def test_video_description_does_not_count_as_a_transcript():
    raw = dict(platform="bilibili", title="Lecture", text="Description " * 200,
               capture_method="api", content_kind="video metadata")
    record = grade(raw, "https://www.bilibili.com/video/BV123")
    assert record["capture_status"] == "metadata_only"
    assert record["missing_elements"] == ["spoken transcript"]


def test_reading_guide_preserves_archive_and_rejects_invented_quote():
    from pathlib import Path
    from knowledge_forge.discovery import Note, VaultIndex
    from knowledge_forge.maps import reading
    from knowledge_forge.taxonomy.registry import TaxonomyRegistry
    import pytest

    vault = Path("/synthetic-vault")
    body = "## Source text\n\nThe draft describes a useful reading method.\n\n## My notes\n\nKeep this note.\n"
    n = Note(vault / "10_sources/example.md", {"id": "sample", "note_type": "source",
              "title": "Sample", "capture_status": "complete"}, body)
    vi = VaultIndex(vault)
    registry = TaxonomyRegistry.load(vault / "registry.yaml")
    card = {"abstract": "A reading method", "meaning": "Useful when reviewing notes",
            "limitation": "The archive is a draft.",
            "points": [{"point": "The method is a draft",
                        "quote": "The draft describes a useful reading method."}]}
    meta, result = reading.render_card(vault, n, card, registry, vi)
    assert reading.original_body(result) == body
    assert "[[#Source text|Read full saved source]]" in result
    assert meta["reading_guide_version"] == 1 and "[[#Source evidence 1|Evidence]]" in result
    card["points"][0]["quote"] = "The draft describes a nonexistent technique."
    with pytest.raises(ValueError, match="not found"):
        reading.render_card(vault, n, card, registry, vi)


def test_brain_refresh_preserves_layout_and_manual_edges():
    import json
    import os
    from pathlib import Path
    from tempfile import mkdtemp
    from knowledge_forge.maps import brain

    root = Path(mkdtemp(prefix="canvas-", dir=os.environ["KF_TEST_CASES"]))
    first = brain._card("root", "Domain", [], 0, 0)
    second = brain._card("child", "Topic", [], 200, 200)
    generated_edge = brain._edge(first, second, "5")
    old = {"nodes": [{**first, "x": 417}, second,
                      {"id": "reader-note", "x": 80, "y": 10, "width": 200, "height": 100,
                       "type": "text", "text": "My graph annotation"}],
           "edges": [generated_edge, {"id": "reader-link", "fromNode": first["id"],
                                     "toNode": "reader-note"}]}
    (root / "test.canvas").write_text(json.dumps(old))
    output = json.loads(brain._layout(root, "test.canvas", [first, second], [generated_edge]))
    assert output["nodes"][0]["x"] == 417
    assert output["nodes"][2]["text"] == "My graph annotation"
    assert output["edges"][1]["id"] == "reader-link"


def test_retried_short_link_matches_legacy_saved_share_url():
    from pathlib import Path
    from knowledge_forge.discovery import Note, VaultIndex
    from knowledge_forge.pipeline import _existing_capture

    vault = Path("/synthetic-vault")
    original = Note(vault / "10_sources/old.md", {"id": "old-source", "note_type": "source",
                    "url": "https://xhslink.com/o/old"}, "")
    vi = VaultIndex(vault, notes=[original], by_id={"old-source": original})
    record = {"id": "new-id", "canonical_url": "https://xiaohongshu.com/explore/123",
              "source_url": "https://xhslink.com/o/old"}
    assert _existing_capture(vi, record) is original
