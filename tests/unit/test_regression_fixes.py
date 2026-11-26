"""Regression tests for the fixes made during the 2026-07 capture-pipeline
hardening session. Each test locks in one previously-broken behaviour:

  A  arXiv adapter          — arxiv.org is a platform; cap_arxiv parses metadata
  B2 provider guard         — key set but base_url missing must degrade to offline
  B3 briefing degradation   — a raising provider must not crash the brief
  D  versioning path        — new_version must write a .md file, not an extensionless sibling
  E  youtube subtitle pick  — deterministic language-priority choice
  G  quality gate           — image/video-first post with no media is metadata_only, not complete
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import pytest

from knowledge_forge import urlnorm
from knowledge_forge.quality import gate
from knowledge_forge.capture import adapters
from knowledge_forge.enrichment import harness
from knowledge_forge.vault import writer
from knowledge_forge.discovery import VaultIndex
from knowledge_forge.briefing import builder


# ---- shared gate record helper (mirrors tests/unit/test_core.py) -----------------
def _rec(text, method="direct", **kw):
    r = {"text": text, "capture_method": method, "state": "received",
         "title": kw.pop("title", "T"), "images": []}
    r.update(kw)
    r["state"] = "received"
    gate.transition(r, "capturing")
    gate.transition(r, "normalized")
    return r


# ---- A: arXiv adapter ------------------------------------------------------------
def test_arxiv_is_a_platform():
    assert urlnorm.platform_of("https://arxiv.org/abs/2606.12683") == "arxiv"
    assert urlnorm.platform_of("https://arxiv.org/pdf/2606.12683v1") == "arxiv"
    assert urlnorm.platform_of("https://www.arxiv.org/html/2606.12683") == "arxiv"


def test_arxiv_cascade_prefers_full_pdf_before_abstract():
    assert adapters.CASCADES["arxiv"][:2] == [adapters.cap_paper_pdf, adapters.cap_arxiv]


_ARXIV_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <title>From AGI to ASI</title>
    <summary>  Over the last decade, building AGI moved from speculation to target.  </summary>
    <published>2026-06-10T12:00:00Z</published>
    <author><name>Alice Alpha</name></author>
    <author><name>Bob Beta</name></author>
    <category term="cs.LG"/>
    <category term="cs.AI"/>
    <arxiv:primary_category term="cs.AI"/>
  </entry>
</feed>"""


def test_cap_arxiv_parses_metadata(monkeypatch):
    monkeypatch.setattr(adapters, "http_get", lambda url, **kw: _ARXIV_XML)
    cap = adapters.cap_arxiv("https://arxiv.org/abs/2606.12683", "arxiv", {})
    assert cap["title"] == "From AGI to ASI"          # bracket-free, whitespace-collapsed
    assert cap["authors"] == ["Alice Alpha", "Bob Beta"]
    assert cap["published_at"] == "2026-06-10"
    assert cap["platform"] == "arxiv" and cap["capture_method"] == "api"
    assert "cs.AI" in cap["text"]                     # primary category first
    assert cap["text"].index("cs.AI") < cap["text"].index("cs.LG")


def test_cap_arxiv_raises_on_bad_id():
    with pytest.raises(ValueError):
        adapters.cap_arxiv("https://arxiv.org/abs/not-an-id", "arxiv", {})


# ---- B2: provider requires both key AND base_url ---------------------------------
_PROFILES = {"profiles": {"low": {"provider": "openai_compat",
                                  "api_key_env": "K", "base_url_env": "B"}}}


def test_provider_half_config_degrades_to_offline():
    key_only = lambda n: "sk-key" if n == "K" else ""      # key set, base_url missing
    assert type(harness.provider_for_profile("low", _PROFILES, key_only)).__name__ \
        == "OfflineProvider"


def test_provider_full_config_builds_openai():
    both = lambda n: "sk-key" if n == "K" else "https://ep.example/v1"
    prov = harness.provider_for_profile("low", _PROFILES, both)
    assert type(prov).__name__ == "OpenAICompatProvider"
    assert prov.base_url == "https://ep.example/v1"


# ---- B3: a raising provider must not crash the brief -----------------------------
def _write_source_note(vault: Path, day: str):
    (vault / "10_sources").mkdir(parents=True, exist_ok=True)
    (vault / "10_sources" / "n.md").write_text(
        "---\nid: src_x\nnote_type: source\ntitle: Sample Note\n"
        f"capture_status: complete\ncaptured_at: '{day}T10:00:00+08:00'\n"
        "schema_version: 1.5.1\n---\n\n## TL;DR\n\nDETERMINISTIC_TLDR_MARKER\n",
        encoding="utf-8")


def test_briefing_survives_provider_error(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    today = dt.date.today()
    _write_source_note(vault, today.isoformat())
    cfg = SimpleNamespace(vault=vault)

    monkeypatch.setattr(harness, "load_prompt", lambda *a, **k: "{sources}")

    def boom(*a, **k):
        raise ValueError("unknown url type: '/chat/completions'")
    monkeypatch.setattr(harness, "call_with_contract", boom)

    # provider is non-None so the narration path is taken; it raises -> must degrade
    built = builder.build_briefing(cfg, "daily", today, provider=object())
    assert built["model_used"] is False
    assert "DETERMINISTIC_TLDR_MARKER" in built["content"]   # extractive fallback used


# ---- D: new_version writes a .md path --------------------------------------------
def test_new_version_writes_md_path(tmp_path):
    existing = SimpleNamespace(
        meta={"id": "src_x", "content_hash": "sha256:OLD", "canonical_url": "https://x/y"},
        path=tmp_path / "10_sources" / "2026-07-15-web-t--dead.md",
        note_type="source", body="original body")
    vi = SimpleNamespace(by_id={"src_x": existing}, notes=[existing])
    record = {"id": "src_x", "capture_status": "complete", "canonical_url": "https://x/y",
              "content_hash": "sha256:NEW", "captured_at": "2026-07-15T00:00:00+08:00",
              "text": "updated text", "platform": "web", "title": "t", "capture_method": "direct"}
    plan = writer.plan_source_filing(SimpleNamespace(vault=tmp_path), vi, record)
    assert plan["action"] == "new_version"
    assert plan["rel"].endswith(".md")               # regression: was extensionless


def test_duplicate_hash_is_noop(tmp_path):
    existing = SimpleNamespace(
        meta={"id": "src_x", "content_hash": "sha256:SAME", "canonical_url": "https://x/y",
              "capture_status": "complete"},
        path=tmp_path / "10_sources" / "n.md", note_type="source", body="b")
    vi = SimpleNamespace(by_id={"src_x": existing}, notes=[existing])
    record = {"id": "src_x", "capture_status": "complete", "canonical_url": "https://x/y",
              "content_hash": "sha256:SAME", "captured_at": "2026-07-15T00:00:00+08:00",
              "text": "x", "platform": "web", "title": "t", "capture_method": "direct"}
    plan = writer.plan_source_filing(SimpleNamespace(vault=tmp_path), vi, record)
    assert plan["action"] == "noop_duplicate"


# ---- E: deterministic youtube subtitle selection ---------------------------------
def test_pick_vtt_prefers_english_deterministically():
    vtts = [Path("cap.zh.vtt"), Path("cap.en.vtt"), Path("cap.zh-Hans.vtt")]
    assert adapters._pick_vtt(vtts).name == "cap.en.vtt"
    # order of the glob result must not change the choice
    assert adapters._pick_vtt(list(reversed(vtts))).name == "cap.en.vtt"


def test_pick_vtt_falls_to_priority_order():
    vtts = [Path("cap.fr.vtt"), Path("cap.zh.vtt"), Path("cap.zh-Hans.vtt")]
    assert adapters._pick_vtt(vtts).name == "cap.zh-Hans.vtt"   # zh-Hans before zh


# ---- G: image/video-first captures without media are metadata_only ---------------
def test_image_first_platform_without_media_is_metadata_only():
    r = gate.assess(_rec("页面导航 " * 200, platform="xiaohongshu"))   # images=[] by default
    assert r["capture_status"] == "metadata_only"
    assert r["needs_retry"]
    assert any("image" in x.lower() for x in r["quality_reasons"])


def test_blob_placeholder_without_media_is_metadata_only():
    text = "[![img](blob:http://localhost/x)](http://y) " * 20 + "word " * 30
    r = gate.assess(_rec(text, platform="web"))
    assert r["capture_status"] == "metadata_only"


def test_text_article_still_complete_no_regression():
    r = gate.assess(_rec("real substantive content. " * 100, platform="web"))
    assert r["capture_status"] == "complete"


def test_image_platform_with_ocr_text_is_not_downgraded():
    r = _rec("some caption", platform="xiaohongshu")
    r["ocr_text"] = "OCR extracted body. " * 100      # real content obtained via OCR
    r = gate.assess(r)
    assert r["capture_status"] == "complete"
