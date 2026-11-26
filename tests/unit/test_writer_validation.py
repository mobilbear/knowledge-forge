import errno
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from knowledge_forge.briefing.builder import commit_briefing
from knowledge_forge.cli.main import main
from knowledge_forge.config import load_config
from knowledge_forge.discovery import VaultIndex
from knowledge_forge.index import search
from knowledge_forge.vault import library_writes, transactions
from knowledge_forge.vault.writer import stamp_vault


def test_manual_navigation_and_briefing_keep_the_library_journal(tmp_path, monkeypatch):
    cfg = replace(load_config(), vault=tmp_path / "vault", state_home=tmp_path / "state")
    stamp_vault(cfg)
    monkeypatch.setenv("KF_VAULT", str(cfg.vault))
    monkeypatch.setenv("KF_STATE_HOME", str(cfg.state_home))

    def rejected_legacy_lock(*args, **kwargs):
        raise AssertionError("manual writer attempted the separate legacy lock")

    monkeypatch.setattr(transactions, "VaultLock", rejected_legacy_lock)
    assert main(["--json", "maps", "rebuild"]) == 0
    path = "60_briefings/daily/example.md"
    commit_briefing(cfg, {"rel": path, "content": "# Draft\n\nContent."})
    actions = {}
    for path in cfg.journal_root.glob("*/journal.json"):
        entry = json.loads(path.read_text())
        actions[entry["meta"]["action"]] = path.parent
    assert {"library.rebuild", "library.briefing"} <= set(actions)
    for action in ("library.rebuild", "library.briefing"):
        assert (actions[action] / "staged").is_dir()
    assert not search.status(cfg)["stale"]


def test_recovery_refuses_an_incomplete_journal_from_another_vault(tmp_path):
    cfg = replace(load_config(), vault=tmp_path / "vault", state_home=tmp_path / "state")
    other = tmp_path / "other"
    original = cfg.vault / "10_sources/source.md"
    target = other / "10_sources/source.md"
    for path, text in ((original, "First library"), (target, "Second library")):
        path.parent.mkdir(parents=True)
        path.write_text(text)
    transaction = library_writes.new_transaction(cfg, {"action": "library.capture"})
    transaction.stage_write("10_sources/source.md", "Interrupted value")
    backup = transaction.backup_dir / "10_sources__source.md"
    backup.parent.mkdir(parents=True)
    backup.write_bytes(original.read_bytes())
    transaction._write_journal("committing")
    digest = hashlib.sha256(target.read_bytes()).digest()
    with pytest.raises(RuntimeError, match="different vault"):
        library_writes.recover(replace(cfg, vault=other), lambda: None)
    assert hashlib.sha256(target.read_bytes()).digest() == digest


def test_rollback_retains_new_notes_when_vault_and_state_use_separate_volumes(tmp_path, monkeypatch):
    cfg = replace(load_config(), vault=tmp_path / "vault", state_home=tmp_path / "state")
    target = cfg.vault / "10_sources/source.md"
    target.parent.mkdir(parents=True)
    target.write_text("Interrupted archive")
    transaction = library_writes.new_transaction(cfg, {"action": "library.capture"})
    transaction.stage_write("10_sources/source.md", target.read_text())
    transaction._write_journal("committing")
    rename = Path.rename

    def different_volumes(self, destination):
        if self == target and Path(destination).is_relative_to(cfg.state_home):
            raise OSError(errno.EXDEV, "different filesystems")
        return rename(self, destination)

    monkeypatch.setattr(Path, "rename", different_volumes)
    library_writes.recover(cfg, lambda: None)
    assert not target.exists()
    assert not VaultIndex.build(cfg.vault).notes
    retained = list(target.parent.glob(".kf-recovery/*/source.md"))
    assert len(retained) == 1 and retained[0].read_text() == "Interrupted archive"
