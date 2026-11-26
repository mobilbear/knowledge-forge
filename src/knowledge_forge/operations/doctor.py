"""`kf doctor` — full-system diagnostics. `--fix-safe` may only: rebuild the
index, create missing non-content directories, clear verified-stale locks, and
resolve incomplete transaction journals. It never moves knowledge, deletes
notes, changes taxonomy, or overwrites content.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
from pathlib import Path

from ..config import KFConfig
from ..discovery import VaultIndex
from ..index import search
from ..taxonomy.registry import TaxonomyRegistry
from ..vault import transactions as txn
from . import backup as backup_ops
from . import skill as skill_ops

REQUIRED_VAULT_DIRS = ["00_inbox", "10_sources", "20_concepts", "30_entities",
                       "40_maps", "60_briefings/daily", "60_briefings/weekly",
                       "60_briefings/scripts", "60_briefings/audio", "70_media",
                       "80_queues/capture_retry", "80_queues/pending_classification",
                       "80_queues/review_required", "90_meta/taxonomy",
                       "90_meta/templates", "90_meta/manifests", "90_meta/help"]


def run(cfg: KFConfig, fix_safe: bool = False, stamp: bool = False) -> dict:
    checks, fixes = [], []

    def check(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        return ok

    # ---- paths ---------------------------------------------------------------------
    for label, p in [("KF_HOME", cfg.kf_home), ("KF_VAULT", cfg.vault)]:
        check(f"path:{label}", p.exists(), str(p))
    if not cfg.config_home.exists() and fix_safe:
        cfg.config_home.mkdir(parents=True, exist_ok=True)
        fixes.append(f"created {cfg.config_home}")
    check("path:KF_CONFIG_HOME", cfg.config_home.exists(), str(cfg.config_home))
    for p in (cfg.state_home, cfg.cache_home, cfg.backup_root):
        if not p.exists() and fix_safe:
            p.mkdir(parents=True, exist_ok=True)
            fixes.append(f"created {p}")
        check(f"path:{p.name}", p.exists(), str(p))
    check("env_file", cfg.env_file.exists(), str(cfg.env_file))
    if cfg.env_file.exists():
        mode = oct(cfg.env_file.stat().st_mode & 0o777)
        check("env_file_permissions", mode in ("0o600", "0o644"),
              f"{mode} (0600 recommended if it holds secrets)")

    # ---- vault structure -------------------------------------------------------------
    if cfg.vault.exists():
        missing = [d for d in REQUIRED_VAULT_DIRS if not (cfg.vault / d).exists()]
        if missing and fix_safe:
            for d in missing:
                (cfg.vault / d).mkdir(parents=True, exist_ok=True)
                fixes.append(f"created vault dir {d}")
            missing = []
        check("vault_directories", not missing, f"missing: {missing}" if missing else "all present")
        if stamp and not cfg.vault_marker.exists():
            from ..vault.writer import stamp_vault
            stamp_vault(cfg)
            fixes.append("stamped vault")
        check("vault_stamp", cfg.vault_marker.exists(),
              "stamped" if cfg.vault_marker.exists() else
              "unstamped — run `kf doctor --stamp` for the intended vault")

        # ---- notes: ids, links, schema versions --------------------------------------
        vi = VaultIndex.build(cfg.vault)
        check("frontmatter_parse", not vi.parse_errors,
              f"{len(vi.parse_errors)} unparseable notes" if vi.parse_errors else
              f"{len(vi.notes)} notes parsed")
        check("duplicate_ids", not vi.duplicate_ids,
              str(vi.duplicate_ids[:3]) if vi.duplicate_ids else "none")
        lr = vi.link_report()
        check("links_ambiguous", not lr["ambiguous"],
              f"{len(lr['ambiguous'])} ambiguous basenames" if lr["ambiguous"] else "none")
        check("links_broken", not lr["broken"],
              f"{len(lr['broken'])} broken links" if lr["broken"] else
              f"{lr['resolved']} resolved")

        # ---- taxonomy ---------------------------------------------------------------
        try:
            reg = TaxonomyRegistry.load(cfg.taxonomy_registry_path)
            check("taxonomy_registry", True,
                  f"{len(reg.nodes)} nodes" if reg.nodes else "empty (seed on first capture)")
            orphan_nodes = [n["category_id"] for n in reg.nodes.values()
                            if n.get("parent_id") and n["parent_id"] not in reg.nodes]
            check("taxonomy_orphan_parents", not orphan_nodes, str(orphan_nodes[:5]))
        except Exception as e:
            check("taxonomy_registry", False, str(e))

    # ---- index -----------------------------------------------------------------------
    st = search.status(cfg)
    stale = st.get("stale", True)
    if stale and fix_safe and cfg.vault.exists():
        search.rebuild(cfg)
        fixes.append("rebuilt search index")
        st = search.status(cfg)
        stale = st.get("stale", True)
    check("search_index", st.get("exists", False) and not stale,
          "stale" if stale else f"{st.get('note_count', 0)} notes, "
                                f"schema {st.get('schema_version')}")

    # ---- transactions + lock ------------------------------------------------------------
    incomplete = txn.find_incomplete(cfg.state_home)
    library_entries = [item for item in incomplete
                       if item.get("meta", {}).get("action", "").startswith("library.")]
    if library_entries and len(library_entries) == len(incomplete) and fix_safe:
        from ..vault import library_writes
        with library_writes.library_lock(cfg.state_home / "library.lock"):
            library_writes.recover(cfg, lambda: search.refresh_after_commit(cfg))
        fixes.append(f"recovered {len(incomplete)} library transactions")
        incomplete = txn.find_incomplete(cfg.state_home)
    elif incomplete and not library_entries and fix_safe:
        acts = txn.recover(cfg.state_home, cfg.vault,
                           index_refresh=lambda: search.refresh_after_commit(cfg))
        fixes.append(f"recovered {len(acts)} transactions")
        incomplete = txn.find_incomplete(cfg.state_home)
    check("incomplete_transactions", not incomplete,
          f"{len(incomplete)} pending journals" if incomplete else "none")
    if cfg.lock_path.exists():
        try:
            info = json.loads(cfg.lock_path.read_text())
            age = time.time() - info.get("ts", 0)
            is_stale = age > 3600
            if is_stale:
                alive = False
                try:
                    os.kill(info.get("pid", -1), 0)
                    alive = True
                except Exception:
                    pass
                if not alive and fix_safe:
                    cfg.lock_path.unlink()
                    fixes.append("cleared verified-stale lock")
                    is_stale = False
            check("stale_lock", not is_stale, f"lock age {age:.0f}s")
        except Exception:
            check("stale_lock", False, "unreadable lock file")
    else:
        check("stale_lock", True, "no lock held")

    # ---- skill drift ----------------------------------------------------------------------
    sv = skill_ops.verify(cfg)
    check("skill_installed_verified", sv.get("ok", False),
          json.dumps({k: v for k, v in sv.items() if k != "canonical_diff"})[:200])

    # ---- backups ----------------------------------------------------------------------------
    lb = backup_ops.latest(cfg)
    if lb:
        age_days = (dt.datetime.now().astimezone()
                    - dt.datetime.fromisoformat(lb["created_at"])).days
        check("backup_recent", age_days <= 7, f"latest {lb['backup_id']} ({age_days}d old)")
    else:
        check("backup_recent", False, "no backups found — run `kf backup create`")

    # ---- queues --------------------------------------------------------------------------------
    if cfg.vault.exists():
        qdepth = sum(1 for _ in (cfg.vault / "80_queues").rglob("*.md")) \
            if (cfg.vault / "80_queues").exists() else 0
        check("queue_depth", qdepth < 25, f"{qdepth} queued items")

    ok = all(c["ok"] for c in checks)
    return {"ok": ok, "checks": checks, "fixes_applied": fixes}
