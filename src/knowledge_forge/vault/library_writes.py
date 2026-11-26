"""Capture transactions that retain receipts, backups and staged files."""
from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager

from .. import ids
from .transactions import PlannedWrite, Transaction, find_incomplete


@contextmanager
def library_lock(path, *, blocking=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        mode = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        fcntl.flock(fd, mode)
        yield
    finally:
        os.close(fd)


class LibraryTransaction(Transaction):
    def stage_artifact(self, path, data):
        self._check_rel(path)
        self.stage_dir.mkdir(parents=True, exist_ok=True)
        staged = f"{len(self.ops):04d}.binary"
        (self.stage_dir / staged).write_bytes(data)
        self.ops.append(PlannedWrite("write", path, staged))

    def diff(self):
        changes = []
        for op in self.ops:
            target = self.vault / op.vault_rel
            content = (self.stage_dir / op.staged_name).read_bytes()
            old = target.read_bytes() if target.is_file() else None
            kind = "create" if old is None else "noop" if old == content else "modify"
            changes.append({"op": kind, "path": op.vault_rel,
                            "new_bytes": len(content), "old_bytes": len(old or b"")})
        return changes

    def rollback(self):
        import shutil
        retained = self.backup_dir.parent / "rollback"
        retained.mkdir(exist_ok=True)
        for operation in reversed(self.ops):
            target = self.vault / operation.vault_rel
            key = operation.vault_rel.replace("/", "__")
            backup = self.backup_dir / key
            if backup.exists():
                shutil.copy2(backup, target)
            elif target.exists() and not (retained / key).exists():
                target.rename(retained / key)
        self._write_journal("rolled_back")

    def _cleanup_stage(self):
        # The journal and staged inputs are retained together for inspection.
        if self.stage_dir.exists():
            self.stage_dir.rename(self.journal_path.parent / "staged")


def new_transaction(cfg, meta):
    return LibraryTransaction(ids.txn_id(), cfg.state_home, cfg.vault, meta=meta)


def recover(cfg, refresh):
    for entry in find_incomplete(cfg.state_home):
        if not entry.get("meta", {}).get("action", "").startswith("library."):
            raise RuntimeError("a legacy transaction needs review before writing")
        txn = LibraryTransaction(entry["txn_id"], cfg.state_home, cfg.vault,
                                 ops=[PlannedWrite(**op) for op in entry["ops"]],
                                 meta=entry["meta"])
        if entry["phase"] == "committing":
            txn.rollback()
        elif entry["phase"] == "committed":
            refresh()
            txn._write_journal("done")
        elif entry["phase"] == "pending":
            txn._write_journal("rolled_back")
        else:
            raise RuntimeError("unknown capture journal phase")
        txn._cleanup_stage()
