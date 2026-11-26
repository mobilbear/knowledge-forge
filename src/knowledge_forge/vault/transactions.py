"""Vault mutation safety: one lock, a staging area, a transaction journal,
atomic per-file commits, and crash recovery.

Write path (mandatory for every mutation):
    lock → stage files under $KF_STATE_HOME/staging/<txn_id>/ → diff
    → validate → journal(pending) → commit (backup originals, atomic replace)
    → journal(committed) → index refresh → journal(done)

If the process dies mid-commit, the next invocation calls ``recover()``:
- journal 'pending'   → staging never touched the vault → discard stage.
- journal 'committing'→ roll back from the per-txn backup of originals.
- journal 'committed' → finish the index refresh step, mark done.
No model ever calls these APIs with free-form paths: all paths are generated
by deterministic code and validated to live inside the vault.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .. import ids


class LockError(RuntimeError):
    pass


class VaultLock:
    """Single-writer file lock with stale detection (pid + timestamp)."""

    def __init__(self, lock_path: Path, stale_after_s: int = 3600):
        self.lock_path = lock_path
        self.stale_after_s = stale_after_s
        self._held = False

    def acquire(self, timeout_s: float = 10.0) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + timeout_s
        while True:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w") as f:
                    json.dump({"pid": os.getpid(), "ts": time.time()}, f)
                self._held = True
                return
            except FileExistsError:
                if self._is_stale():
                    self.lock_path.unlink(missing_ok=True)
                    continue
                if time.time() > deadline:
                    raise LockError(
                        f"vault is locked by another operation ({self.lock_path}); "
                        "retry later or run `kf doctor` if you believe it is stale"
                    )
                time.sleep(0.2)

    def _is_stale(self) -> bool:
        try:
            info = json.loads(self.lock_path.read_text())
        except Exception:
            return True
        if time.time() - info.get("ts", 0) > self.stale_after_s:
            return True
        pid = info.get("pid")
        if pid:
            try:
                os.kill(pid, 0)
                return False  # holder alive
            except ProcessLookupError:
                return True
            except PermissionError:
                return False
        return False

    def release(self) -> None:
        if self._held:
            self.lock_path.unlink(missing_ok=True)
            self._held = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


@dataclass
class PlannedWrite:
    """One file operation. op ∈ {write, delete_generated}. 'delete_generated'
    is allowed only for regenerable navigation artifacts, never source notes."""
    op: str
    vault_rel: str
    staged_name: Optional[str] = None  # file inside the staging dir


@dataclass
class Transaction:
    txn_id: str
    state_home: Path
    vault: Path
    ops: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    @property
    def stage_dir(self) -> Path:
        return self.state_home / "staging" / self.txn_id

    @property
    def backup_dir(self) -> Path:
        return self.state_home / "journal" / self.txn_id / "backup"

    @property
    def journal_path(self) -> Path:
        return self.state_home / "journal" / self.txn_id / "journal.json"

    # ---- staging -----------------------------------------------------------------
    def stage_write(self, vault_rel: str, content: str) -> None:
        self._check_rel(vault_rel)
        self.stage_dir.mkdir(parents=True, exist_ok=True)
        staged = f"{len(self.ops):04d}__{vault_rel.replace('/', '__')}"
        (self.stage_dir / staged).write_text(content, encoding="utf-8")
        self.ops.append(PlannedWrite("write", vault_rel, staged))

    def stage_delete_generated(self, vault_rel: str) -> None:
        self._check_rel(vault_rel)
        allowed = ("40_maps/", "80_queues/", "90_meta/index", "90_meta/provenance")
        if not vault_rel.startswith(allowed) and vault_rel != "HOME.md":
            raise ValueError(
                f"delete is only permitted for generated navigation/queue files, not {vault_rel}")
        self.ops.append(PlannedWrite("delete_generated", vault_rel))

    def _check_rel(self, vault_rel: str) -> None:
        p = (self.vault / vault_rel).resolve()
        if not str(p).startswith(str(self.vault.resolve()) + os.sep):
            raise ValueError(f"path escapes vault: {vault_rel}")
        if "/." in "/" + vault_rel:
            raise ValueError(f"hidden path segments are not allowed: {vault_rel}")

    def diff(self) -> list[dict]:
        """Human/machine-readable planned changes."""
        out = []
        for op in self.ops:
            target = self.vault / op.vault_rel
            if op.op == "write":
                new = (self.stage_dir / op.staged_name).read_text(encoding="utf-8")
                if target.exists():
                    old = target.read_text(encoding="utf-8", errors="replace")
                    kind = "noop" if old == new else "modify"
                else:
                    kind = "create"
                out.append({"op": kind, "path": op.vault_rel,
                            "new_bytes": len(new.encode()),
                            "old_bytes": target.stat().st_size if target.exists() else 0})
            else:
                out.append({"op": "delete_generated", "path": op.vault_rel,
                            "exists": target.exists()})
        return out

    # ---- journal + commit ----------------------------------------------------------
    def _write_journal(self, phase: str) -> None:
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.journal_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "txn_id": self.txn_id, "phase": phase, "ts": time.time(),
            "meta": self.meta,
            "ops": [{"op": o.op, "vault_rel": o.vault_rel, "staged_name": o.staged_name}
                    for o in self.ops],
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.journal_path)

    def commit(self, after_commit: Optional[Callable[[], None]] = None) -> dict:
        """Apply staged ops to the vault. ``after_commit`` (index refresh) runs
        inside the transaction; overall success requires it to succeed."""
        self._write_journal("pending")
        # backup every file we will touch
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        for op in self.ops:
            target = self.vault / op.vault_rel
            if target.exists():
                b = self.backup_dir / op.vault_rel.replace("/", "__")
                shutil.copy2(target, b)
        self._write_journal("committing")
        applied = []
        try:
            for op in self.ops:
                target = self.vault / op.vault_rel
                if op.op == "write":
                    target.parent.mkdir(parents=True, exist_ok=True)
                    tmp = target.with_suffix(target.suffix + f".{self.txn_id}.tmp")
                    shutil.copyfile(self.stage_dir / op.staged_name, tmp)
                    os.replace(tmp, target)
                elif op.op == "delete_generated":
                    target.unlink(missing_ok=True)
                applied.append(op.vault_rel)
        except Exception:
            self.rollback()
            raise
        self._write_journal("committed")
        if after_commit is not None:
            after_commit()  # e.g. index refresh; failure leaves journal 'committed' for recovery
        self._write_journal("done")
        self._cleanup_stage()
        return {"txn_id": self.txn_id, "applied": applied}

    def rollback(self) -> None:
        for op in self.ops:
            target = self.vault / op.vault_rel
            b = self.backup_dir / op.vault_rel.replace("/", "__")
            if b.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(b, target)
            else:
                target.unlink(missing_ok=True)
        self._write_journal("rolled_back")

    def _cleanup_stage(self) -> None:
        shutil.rmtree(self.stage_dir, ignore_errors=True)


def new_transaction(state_home: Path, vault: Path, meta: Optional[dict] = None) -> Transaction:
    return Transaction(ids.txn_id(), state_home, vault, meta=meta or {})


def find_incomplete(state_home: Path) -> list[dict]:
    out = []
    jroot = state_home / "journal"
    if not jroot.exists():
        return out
    for jp in sorted(jroot.glob("*/journal.json")):
        try:
            j = json.loads(jp.read_text(encoding="utf-8"))
        except Exception:
            continue
        if j.get("phase") not in ("done", "rolled_back"):
            out.append(j)
    return out


def recover(state_home: Path, vault: Path,
            index_refresh: Optional[Callable[[], None]] = None) -> list[dict]:
    """Resolve every incomplete transaction. Returns actions taken."""
    actions = []
    for j in find_incomplete(state_home):
        txn = Transaction(j["txn_id"], state_home, vault)
        txn.ops = [PlannedWrite(**o) for o in j["ops"]]
        txn.meta = j.get("meta", {})
        phase = j["phase"]
        if phase == "pending":
            txn._cleanup_stage()
            txn._write_journal("rolled_back")
            actions.append({"txn_id": j["txn_id"], "action": "discarded_unapplied_stage"})
        elif phase == "committing":
            txn.rollback()
            actions.append({"txn_id": j["txn_id"], "action": "rolled_back_partial_commit"})
        elif phase == "committed":
            if index_refresh is not None:
                index_refresh()
            txn._write_journal("done")
            txn._cleanup_stage()
            actions.append({"txn_id": j["txn_id"], "action": "completed_index_refresh"})
    return actions
