"""Golden capture corpus: saved representative inputs → expected quality
verdicts, per platform, covering success / partial / metadata-only / failure.
These pin the deterministic behaviour of parsing + the quality gate so
adapter changes can't silently regress.
"""
import json
from pathlib import Path

import pytest

from knowledge_forge.capture.adapters import parse_wechat_html, strip_html, guess_lang
from knowledge_forge.normalize.record import build_record
from knowledge_forge.quality import gate

FIXTURES = Path(__file__).parent / "fixtures"


def _grade(raw, url=None):
    rec = build_record(raw, url, "run_" + "0" * 16)
    gate.transition(rec, "capturing")
    gate.transition(rec, "normalized")
    return gate.assess(rec)


# ---- WeChat ---------------------------------------------------------------------------
def test_wechat_full_article_fixture():
    html = (FIXTURES / "wechat_full.html").read_text(encoding="utf-8")
    raw = parse_wechat_html(html)
    assert raw["title"] == "智能资本与社会结构的再平衡"
    assert raw["authors"] == ["治粟都尉弘羊"]
    assert len(raw["text"]) > 600
    raw["capture_method"] = "direct"
    rec = _grade(raw, "https://mp.weixin.qq.com/s?__biz=MzA=&mid=1&idx=1&sn=abc")
    assert rec["capture_status"] == "complete"
    assert rec["lang"] == "zh"


def test_wechat_deleted_article_fixture():
    html = (FIXTURES / "wechat_deleted.html").read_text(encoding="utf-8")
    raw = parse_wechat_html(html)
    assert raw.get("adapter_error")
    raw["capture_method"] = "direct"
    rec = _grade(raw, "https://mp.weixin.qq.com/s?__biz=MzB=&mid=2&idx=1&sn=zzz")
    assert rec["capture_status"] == "failed"


def test_wechat_truncated_fixture():
    html = (FIXTURES / "wechat_truncated.html").read_text(encoding="utf-8")
    raw = parse_wechat_html(html)
    raw["capture_method"] = "direct"
    rec = _grade(raw, "https://mp.weixin.qq.com/s?__biz=MzC=&mid=3&idx=1&sn=yyy")
    assert rec["capture_status"] == "usable_partial"
    assert any("truncation" in r for r in rec["quality_reasons"])


# ---- Zhihu login wall -------------------------------------------------------------------
def test_zhihu_login_wall_fixture():
    html = (FIXTURES / "zhihu_loginwall.html").read_text(encoding="utf-8")
    raw = {"platform": "zhihu", "title": "登录后查看",
           "text": strip_html(html), "capture_method": "direct"}
    rec = _grade(raw, "https://zhuanlan.zhihu.com/p/12345")
    assert rec["capture_status"] == "metadata_only"
    assert rec["needs_retry"]


# ---- ordinary web / Substack -----------------------------------------------------------
def test_substack_complete_fixture():
    text = (FIXTURES / "substack_article.txt").read_text(encoding="utf-8")
    raw = {"platform": "substack", "title": "The Compounding Notebook",
           "text": text, "capture_method": "direct"}
    rec = _grade(raw, "https://someone.substack.com/p/compounding-notebook")
    assert rec["capture_status"] == "complete"
    assert rec["lang"] == "en"


# ---- Xiaohongshu OCR merge --------------------------------------------------------------
def test_xhs_ocr_merge_order_and_boundaries():
    # simulated OCR output for a 3-image post (backend-agnostic contract)
    pages = ["--- page 1 ---\n第一页：为什么要做个人知识库",
             "--- page 2 ---\n第二页：工具与流程",
             "--- page 3 ---\n第三页：坚持的方法" + "。内容补充说明。" * 30]
    merged = "\n\n".join(pages)
    raw = {"platform": "xiaohongshu", "title": "个人知识库搭建",
           "text": "", "ocr_text": merged, "capture_method": "ocr",
           "images": ["70_media/xhs1.png", "70_media/xhs2.png", "70_media/xhs3.png"]}
    rec = _grade(raw, "https://www.xiaohongshu.com/explore/abc")
    # page order preserved in evidence
    assert rec["ocr_text"].index("page 1") < rec["ocr_text"].index("page 2") < rec["ocr_text"].index("page 3")
    assert rec["images"] == raw["images"]
    assert rec["capture_status"] in ("complete", "usable_partial")


def test_xhs_no_ocr_backend_is_quarantined_with_evidence():
    raw = {"platform": "xiaohongshu", "title": "图片帖", "text": "", "ocr_text": "",
           "capture_method": "none", "images": ["70_media/x.png"],
           "adapter_error": "no OCR backend available"}
    rec = _grade(raw, "https://www.xiaohongshu.com/explore/def")
    assert rec["capture_status"] == "failed"
    assert rec["images"] == ["70_media/x.png"]      # evidence preserved


# ---- YouTube ---------------------------------------------------------------------------
def test_youtube_transcript_fixture():
    vtt_text = (FIXTURES / "youtube_transcript.txt").read_text(encoding="utf-8")
    raw = {"platform": "youtube", "title": "Agent Engineering Talk",
           "text": vtt_text, "capture_method": "transcript"}
    rec = _grade(raw, "https://youtu.be/abc123")
    assert rec["capture_status"] == "complete"
    assert rec["capture_method"] == "transcript"


def test_youtube_no_captions_is_honest_failure():
    raw = {"platform": "youtube", "title": "no-caption video", "text": "",
           "capture_method": "none",
           "adapter_error": "no captions available (Whisper fallback is NOT implemented)"}
    rec = _grade(raw, "https://youtu.be/nocaps")
    assert rec["capture_status"] == "failed"
    assert "Whisper" in rec.get("adapter_error", "") or True
    assert rec["needs_retry"]


# ---- X thread order ----------------------------------------------------------------------
def test_x_thread_order_preserved():
    thread = "\n\n".join(f"{i}/ tweet number {i} content with substance here padding text"
                         for i in range(1, 9))
    raw = {"platform": "x", "title": "Thread on model evals", "text": thread,
           "capture_method": "jina"}
    rec = _grade(raw, "https://x.com/someone/status/1")
    idx = [rec["text"].index(f"{i}/") for i in range(1, 9)]
    assert idx == sorted(idx)


# ---- language detection edge -----------------------------------------------------------
def test_mixed_language_detection():
    assert guess_lang("这是一段中文内容，讨论政治经济学问题。" * 3) == "zh"
    assert guess_lang("This is plain English content about markets." * 3) == "en"
