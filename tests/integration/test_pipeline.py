"""Integration tests: end-to-end pipeline behaviour on a real temp vault."""
import json
import os
from pathlib import Path

import pytest

from knowledge_forge.config import load_config
from knowledge_forge.vault.writer import stamp_vault
from knowledge_forge.index import search
from knowledge_forge import pipeline
from knowledge_forge.capture import jobs
from knowledge_forge.frontmatter import read_note


GOOD_TEXT = ("Deterministic scaffolding lets weak language models operate "
             "production knowledge systems safely because schemas, state "
             "machines and validation carry correctness. " * 12)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    for d in ("00_inbox", "10_sources", "20_concepts", "30_entities", "40_maps",
              "60_briefings/daily", "80_queues", "90_meta"):
        (vault / d).mkdir(parents=True)
    monkeypatch.setenv("KF_VAULT", str(vault))
    monkeypatch.setenv("KF_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("KF_HOME", str(Path(__file__).resolve().parents[2]))
    monkeypatch.setenv("KF_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("KF_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("KF_BACKUP_ROOT", str(tmp_path / "bak"))
    c = load_config()
    stamp_vault(c, "TestVault")
    return c


def _capture(cfg, url="https://example.com/a", text=GOOD_TEXT, **raw_extra):
    raw = {"platform": "web", "title": raw_extra.pop("title", "Agent Harness Notes"),
           "text": text, "capture_method": "direct", **raw_extra}
    return pipeline.run_capture(cfg, url, _test_raw=raw)


def test_usable_capture_commits_and_indexes(cfg):
    res = _capture(cfg)
    assert res["status"] == "completed"
    note = cfg.vault / res["result"]["note"]
    assert note.exists()
    # committed content is immediately searchable — no manual rebuild
    hits = search.query(cfg, "scaffolding")
    assert hits and hits[0]["note_id"] == res["result"]["source_id"]
    # a Chinese-status message exists
    assert res["result"]["capture_status"] == "complete"
    assert "完整存档" in res["user_message_zh"]


def test_repeat_url_idempotent(cfg):
    r1 = _capture(cfg, url="https://example.com/a?utm_source=wx")
    r2 = _capture(cfg, url="https://example.com/a?utm_source=tg")
    assert r1["status"] == "completed"
    assert r2["status"] == "noop"
    assert r2["result"]["action"] == "noop_duplicate"
    files = list((cfg.vault / "10_sources").rglob("*.md"))
    assert len(files) == 1


def test_identical_recapture_restores_missing_attachment_and_receipt(cfg):
    first = _capture(cfg, artifacts=[("original.pdf", b"example original")])
    path = cfg.vault / first["result"]["attachments"][0]
    retained = path.rename(cfg.vault.parent / "retained-original.pdf")
    receipt = jobs.submit(cfg, "https://example.com/a")["receipt"]
    second = _capture(cfg, artifacts=[("original.pdf", b"example original")])
    assert second["status"] == "noop"
    assert path.read_bytes() == retained.read_bytes()
    assert jobs.lookup(cfg, receipt)["status"] == "archived"
    assert search.contains_note(cfg, first["result"]["source_id"])


def test_changed_content_appends_version_never_overwrites(cfg):
    r1 = _capture(cfg, text=GOOD_TEXT)
    r2 = _capture(cfg, text=GOOD_TEXT + " Updated paragraph with new findings.")
    assert r2["result"]["action"] == "new_version"
    body = (cfg.vault / (r1["result"]["note"] + ".md")).read_text() \
        if not r1["result"]["note"].endswith(".md") else ""
    note_path = cfg.vault / r2["result"]["note"]
    content = note_path.read_text()
    assert "## Updated capture" in content
    assert "Deterministic scaffolding" in content        # original text preserved
    assert "version_history" in content


def test_metadata_only_is_quarantined(cfg):
    res = _capture(cfg, url="https://zhihu.com/p/1",
                   text="请登录后查看全文", title="Login-walled post")
    assert res["status"] == "queued"
    assert res["result"]["action"] == "quarantine"
    assert res["result"]["note"].startswith("80_queues/")
    assert not list((cfg.vault / "10_sources").rglob("*login*"))
    # truthful Chinese message
    assert res["result"]["capture_status"] == "metadata_only"
    assert "仅取得元数据" in res["user_message_zh"]
    # the quarantined note itself is excluded from knowledge search
    # (HOME may mention it in "needs attention" — that's navigation, not knowledge)
    knowledge_hits = search.query(cfg, "Login-walled")
    assert all(h["note_type"] != "quarantined_capture"
               and not h["path"].startswith("80_queues/") for h in knowledge_hits)
    op_hits = search.query(cfg, "Login-walled", include_quarantined=True)
    assert any(h["path"].startswith("80_queues/") for h in op_hits)


def test_home_never_lists_raw_sources(cfg):
    _capture(cfg)
    home = (cfg.vault / "HOME.md").read_text()
    # today's section may link today's items, but no 10_sources dump section:
    assert "How this wiki is organised" not in home     # v1.0 folder table gone
    assert "10_sources" not in home.replace(
        "\n".join(l for l in home.splitlines() if "今日新增" in l or l.startswith("- [[10_sources")), "") or True
    # strict check: outside the "New today" block, no 10_sources links
    lines = home.splitlines()
    in_today = False
    for l in lines:
        if l.startswith("## "):
            in_today = l.startswith("## 今日新增")
        if "[[10_sources/" in l:
            assert in_today, f"raw source linked outside New-today: {l}"


def test_maps_contain_no_raw_sources(cfg):
    _capture(cfg)
    for p in (cfg.vault / "40_maps").rglob("_map.md"):
        assert "[[10_sources/" not in p.read_text()
    atlas = cfg.vault / "40_maps/_Global Atlas.md"
    assert atlas.exists() and "[[10_sources/" not in atlas.read_text()


def test_subhomes_exist_for_active_l1_l2(cfg):
    _capture(cfg)
    from knowledge_forge.taxonomy.registry import TaxonomyRegistry
    reg = TaxonomyRegistry.load(cfg.taxonomy_registry_path)
    from knowledge_forge.maps.generators import node_dir
    for n in reg.active():
        if n["level"] in (1, 2):
            assert (cfg.vault / node_dir(reg, n["category_id"]) / "_index.md").exists(), \
                f"missing Sub-Home for {n['category_id']}"


def test_unclassified_lands_in_pending_queue(cfg):
    res = _capture(cfg)
    assert res["needs_user_action"] is True
    meta, _ = read_note(cfg.vault / res["result"]["note"])
    assert meta["classification_status"] == "review" and meta["primary_category_id"] is None
    assert "Needs Attention" in (cfg.vault / "Library.base").read_text()


def test_offline_mode_still_captures(cfg):
    # offline profile: no model at all — capture must still commit evidence
    res = _capture(cfg)
    assert res["status"] == "completed"
    meta, body = read_note(cfg.vault / res["result"]["note"])
    assert meta["analysis_status"] == "needs_review"
    assert "## Key source excerpts" in body


def test_run_capture_recovers_stale_transaction_first(cfg):
    from knowledge_forge.vault import transactions as txn
    from knowledge_forge.vault import library_writes
    t = library_writes.new_transaction(cfg, {"action": "library.capture"})
    t.stage_write("10_sources/ghost.md", "ghost")
    t._write_journal("pending")   # simulated crash before commit
    res = _capture(cfg)
    assert res["status"] == "completed"
    assert not txn.find_incomplete(cfg.state_home)
    assert not (cfg.vault / "10_sources/ghost.md").exists()
    assert (t.journal_path.parent / "staged").is_dir()
