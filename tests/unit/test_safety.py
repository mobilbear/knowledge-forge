"""Unit tests: transaction journal, lock, crash recovery, recursive discovery,
same-basename resolution, and taxonomy governance."""
import json
import os
import threading
import time
from pathlib import Path

import pytest

from knowledge_forge.vault import transactions as txn
from knowledge_forge.discovery import VaultIndex
from knowledge_forge.taxonomy.registry import TaxonomyRegistry, GovernanceError, default_registry_nodes


@pytest.fixture
def env(tmp_path):
    vault = tmp_path / "vault"
    state = tmp_path / "state"
    for d in ("10_sources/nested/deep", "20_concepts", "40_maps", "90_meta"):
        (vault / d).mkdir(parents=True)
    return vault, state


# ---- transactions -------------------------------------------------------------------
def test_commit_and_journal_done(env):
    vault, state = env
    t = txn.new_transaction(state, vault)
    t.stage_write("10_sources/a.md", "hello")
    diff = t.diff()
    assert diff[0]["op"] == "create"
    t.commit()
    assert (vault / "10_sources/a.md").read_text() == "hello"
    assert not txn.find_incomplete(state)


def test_no_silent_overwrite_diff_visible(env):
    vault, state = env
    (vault / "10_sources/a.md").write_text("original")
    t = txn.new_transaction(state, vault)
    t.stage_write("10_sources/a.md", "changed")
    assert t.diff()[0]["op"] == "modify"     # change is explicit, never silent
    t.commit()
    # original is preserved in the journal backup
    backups = list((state / "journal").rglob("backup/*"))
    assert any(b.read_text() == "original" for b in backups)


def test_path_escape_rejected(env):
    vault, state = env
    t = txn.new_transaction(state, vault)
    with pytest.raises(ValueError):
        t.stage_write("../outside.md", "x")
    with pytest.raises(ValueError):
        t.stage_write("10_sources/../../etc/passwd", "x")


def test_delete_only_generated(env):
    vault, state = env
    t = txn.new_transaction(state, vault)
    with pytest.raises(ValueError):
        t.stage_delete_generated("10_sources/a.md")
    t.stage_delete_generated("40_maps/_old.md")  # allowed


def test_interrupted_committing_rolls_back(env):
    vault, state = env
    (vault / "10_sources/a.md").write_text("original")
    t = txn.new_transaction(state, vault)
    t.stage_write("10_sources/a.md", "new-a")
    t.stage_write("10_sources/b.md", "new-b")
    # simulate crash mid-commit: backup + journal(committing) + apply first op only
    t._write_journal("pending")
    t.backup_dir.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copy2(vault / "10_sources/a.md", t.backup_dir / "10_sources__a.md")
    t._write_journal("committing")
    (vault / "10_sources/a.md").write_text("new-a")  # first op applied, then crash

    actions = txn.recover(state, vault)
    assert actions and actions[0]["action"] == "rolled_back_partial_commit"
    assert (vault / "10_sources/a.md").read_text() == "original"
    assert not (vault / "10_sources/b.md").exists()
    assert not txn.find_incomplete(state)


def test_interrupted_committed_finishes_index(env):
    vault, state = env
    t = txn.new_transaction(state, vault)
    t.stage_write("10_sources/a.md", "x")
    t._write_journal("pending")
    t.backup_dir.mkdir(parents=True, exist_ok=True)
    t._write_journal("committing")
    (vault / "10_sources/a.md").write_text("x")
    t._write_journal("committed")   # crash before index refresh
    refreshed = []
    txn.recover(state, vault, index_refresh=lambda: refreshed.append(1))
    assert refreshed == [1]
    assert not txn.find_incomplete(state)


def test_lock_exclusive_and_stale(env, tmp_path):
    vault, state = env
    lock = tmp_path / "v.lock"
    l1 = txn.VaultLock(lock)
    l1.acquire()
    l2 = txn.VaultLock(lock)
    with pytest.raises(txn.LockError):
        l2.acquire(timeout_s=0.4)
    l1.release()
    l2.acquire(timeout_s=0.4)
    l2.release()
    # stale: dead pid
    lock.write_text(json.dumps({"pid": 999999, "ts": time.time()}))
    l3 = txn.VaultLock(lock)
    l3.acquire(timeout_s=1)   # steals stale lock
    l3.release()


# ---- discovery ---------------------------------------------------------------------
def test_recursive_discovery_and_basename_resolution(env):
    vault, _ = env
    (vault / "10_sources/note.md").write_text("---\nid: src_a\ntitle: A\n---\n\nbody")
    (vault / "10_sources/nested/deep/note.md").write_text(
        "---\nid: src_b\ntitle: B\n---\n\nsee [[note]] and [[10_sources/note]]")
    (vault / "20_concepts/c.md").write_text("---\nid: con_1\n---\n\n[[nonexistent]]")
    vi = VaultIndex.build(vault)
    rels = set(vi.by_rel)
    assert "10_sources/nested/deep/note" in rels          # recursion works
    tgt, prob = vi.resolve_link("note")
    assert tgt is None and "ambiguous" in prob            # ambiguity reported, not guessed
    tgt, prob = vi.resolve_link("10_sources/note")
    assert tgt == "10_sources/note" and prob is None      # full path resolves
    lr = vi.link_report()
    assert len(lr["ambiguous"]) == 1 and len(lr["broken"]) == 1


def test_malformed_note_surfaced_not_dropped(env):
    vault, _ = env
    (vault / "10_sources/bad.md").write_text("---\ntitle: [unclosed\n---\n\nbody")
    vi = VaultIndex.build(vault)
    assert vi.parse_errors and vi.parse_errors[0][0] == "10_sources/bad"


def test_duplicate_ids_detected(env):
    vault, _ = env
    (vault / "10_sources/a.md").write_text("---\nid: src_x\n---\n\na")
    (vault / "20_concepts/b.md").write_text("---\nid: src_x\n---\n\nb")
    vi = VaultIndex.build(vault)
    assert vi.duplicate_ids


# ---- taxonomy governance -----------------------------------------------------------------
def _registry(tmp_path):
    reg = TaxonomyRegistry.load(tmp_path / "registry.yaml")
    for n in default_registry_nodes():
        reg.nodes[n["category_id"]] = n
    return reg


def test_l1_creation_blocked_without_approval(tmp_path):
    reg = _registry(tmp_path)
    with pytest.raises(GovernanceError):
        reg.add_node("Brand New Domain", level=1, parent_id=None)
    n = reg.add_node("Brand New Domain", level=1, parent_id=None, approved_by="liang")
    assert n["status"] == "active"


def test_l3_threshold_enforced(tmp_path):
    reg = _registry(tmp_path)
    with pytest.raises(GovernanceError):
        reg.add_node("Tiny Topic", level=3, parent_id="cat_ai_llm_eng",
                     evidence_sources=1, evidence_occasions=1)
    n = reg.add_node("Real Topic", level=3, parent_id="cat_ai_llm_eng",
                     evidence_sources=3, evidence_occasions=2)
    assert n["status"] == "provisional"


def test_near_duplicate_blocked(tmp_path):
    reg = _registry(tmp_path)
    with pytest.raises(GovernanceError):
        reg.add_node("quant risk", level=2, parent_id="cat_ai",
                     evidence_sources=5, evidence_occasions=3)  # alias of cat_quant_risk


def test_merge_preserves_aliases_and_redirects(tmp_path):
    reg = _registry(tmp_path)
    reg.merge("cat_method", "cat_quant_risk", approved_by="liang")
    assert reg.nodes["cat_method"]["status"] == "merged"
    assert reg.get("cat_method")["category_id"] == "cat_quant_risk"  # redirect
    assert "Research Methods" in reg.nodes["cat_quant_risk"]["aliases"]


def test_wrong_parent_level_rejected(tmp_path):
    reg = _registry(tmp_path)
    with pytest.raises(GovernanceError):
        reg.add_node("X", level=3, parent_id="cat_ai",  # L3 under L1
                     evidence_sources=3, evidence_occasions=2)


def test_registry_roundtrip_schema(tmp_path):
    reg = _registry(tmp_path)
    path = tmp_path / "registry.yaml"
    path.write_text(reg.dumps(), encoding="utf-8")
    reg2 = TaxonomyRegistry.load(path)
    assert set(reg2.nodes) == set(reg.nodes)
