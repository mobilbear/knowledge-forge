"""Backup / verify / isolated restore test.

A backup is valid only after `verify` recomputes checksums against the
manifest. `restore_test` extracts into an isolated temp directory and runs
structural checks — an archive existing is not a backup.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
import tarfile
import tempfile
from pathlib import Path

from ..config import KFConfig

VERSION = "1.5.1"


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _collect(root: Path) -> list[tuple[Path, str]]:
    out = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and ".trash" not in p.parts:
            out.append((p, _sha256_file(p)))
    return out


def create(cfg: KFConfig, include_project: bool = True) -> dict:
    """Backup vault (incl. .obsidian), installed skill, config, and state DBs."""
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    bid = f"kfbak-{ts}"
    bdir = cfg.backup_root / bid
    bdir.mkdir(parents=True, exist_ok=True)
    targets = {"vault": cfg.vault}
    if cfg.openclaw_skill.exists():
        targets["skill"] = cfg.openclaw_skill
    if cfg.config_home.exists():
        targets["config"] = cfg.config_home
    if include_project and cfg.kf_home.exists():
        targets["project"] = cfg.kf_home
    if cfg.state_home.exists():
        targets["state"] = cfg.state_home

    manifest = {"backup_id": bid, "created_at": dt.datetime.now().astimezone().isoformat(),
                "software_version": VERSION, "components": {}}
    for name, root in targets.items():
        files = _collect(root)
        tar_path = bdir / f"{name}.tar.gz"
        with tarfile.open(tar_path, "w:gz") as tf:
            tf.add(root, arcname=name)
        manifest["components"][name] = {
            "source_path": str(root), "file_count": len(files),
            "total_bytes": sum(p.stat().st_size for p, _ in files),
            "archive": tar_path.name, "archive_sha256": _sha256_file(tar_path),
            "files": {str(p.relative_to(root)): h for p, h in files},
        }
    (bdir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
    return {"backup_id": bid, "path": str(bdir),
            "components": {k: v["file_count"] for k, v in manifest["components"].items()}}


def verify(cfg: KFConfig, backup_id: str) -> dict:
    bdir = cfg.backup_root / backup_id
    mpath = bdir / "manifest.json"
    if not mpath.exists():
        return {"ok": False, "errors": [f"no manifest at {mpath}"]}
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    errors = []
    for name, comp in manifest["components"].items():
        ar = bdir / comp["archive"]
        if not ar.exists():
            errors.append(f"{name}: archive missing")
            continue
        if _sha256_file(ar) != comp["archive_sha256"]:
            errors.append(f"{name}: archive checksum mismatch")
    return {"ok": not errors, "errors": errors,
            "components": list(manifest["components"])}


def restore_test(cfg: KFConfig, backup_id: str) -> dict:
    """Extract to an isolated temp dir; verify per-file hashes + vault structure."""
    v = verify(cfg, backup_id)
    if not v["ok"]:
        return {"ok": False, "errors": ["archive verification failed"] + v["errors"]}
    bdir = cfg.backup_root / backup_id
    manifest = json.loads((bdir / "manifest.json").read_text(encoding="utf-8"))
    errors, checked = [], 0
    with tempfile.TemporaryDirectory(prefix="kf-restore-test-") as td:
        tdp = Path(td)
        for name, comp in manifest["components"].items():
            with tarfile.open(bdir / comp["archive"], "r:gz") as tf:
                tf.extractall(tdp, filter="data")
            root = tdp / name
            for relf, expected in comp["files"].items():
                p = root / relf
                if not p.exists():
                    errors.append(f"{name}/{relf}: missing after restore")
                elif _sha256_file(p) != expected:
                    errors.append(f"{name}/{relf}: hash mismatch after restore")
                checked += 1
        # structural checks on the restored vault
        rv = tdp / "vault"
        if rv.exists():
            if not (rv / "90_meta").exists():
                errors.append("restored vault missing 90_meta/")
    return {"ok": not errors, "files_checked": checked, "errors": errors[:20]}


def latest(cfg: KFConfig) -> dict | None:
    if not cfg.backup_root.exists():
        return None
    dirs = sorted(cfg.backup_root.glob("kfbak-*"))
    if not dirs:
        return None
    m = dirs[-1] / "manifest.json"
    if not m.exists():
        return None
    j = json.loads(m.read_text(encoding="utf-8"))
    return {"backup_id": j["backup_id"], "created_at": j["created_at"]}
