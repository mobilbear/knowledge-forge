"""OpenClaw Skill: single source of truth with atomic, verified installation.

Canonical source:  $KF_HOME/skills/knowledge-forge/
Installed copy:    $OPENCLAW_SKILL/
`install` stages → validates → backs up the old copy → atomically swaps (rename
of a fully-written temp dir) → writes .kf_skill_manifest.json (version + per-
file sha256). `verify` fails on any drift. `diff` reports it.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
import tempfile
from pathlib import Path

from ..config import KFConfig

SKILL_VERSION = "1.5.1"
MANIFEST_NAME = ".kf_skill_manifest.json"


def canonical_dir(cfg: KFConfig) -> Path:
    return cfg.kf_home / "skills" / "knowledge-forge"


def _file_map(root: Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.name != MANIFEST_NAME:
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def diff(cfg: KFConfig) -> dict:
    src = canonical_dir(cfg)
    dst = cfg.openclaw_skill
    if not src.exists():
        return {"error": f"canonical skill missing at {src}"}
    smap = _file_map(src)
    dmap = _file_map(dst) if dst.exists() else {}
    added = sorted(set(smap) - set(dmap))
    removed = sorted(set(dmap) - set(smap))
    changed = sorted(k for k in smap.keys() & dmap.keys() if smap[k] != dmap[k])
    return {"in_sync": not (added or removed or changed),
            "missing_in_installed": added, "extra_in_installed": removed,
            "changed": changed, "installed_exists": dst.exists()}


def install(cfg: KFConfig) -> dict:
    src = canonical_dir(cfg)
    if not src.exists():
        return {"ok": False, "error": f"canonical skill missing at {src}"}
    dst = cfg.openclaw_skill
    dst.parent.mkdir(parents=True, exist_ok=True)
    # stage a complete new copy next to the destination for an atomic rename
    with tempfile.TemporaryDirectory(dir=dst.parent, prefix=".kf-skill-stage-") as td:
        stage = Path(td) / "knowledge-forge"
        shutil.copytree(src, stage)
        manifest = {"skill_version": SKILL_VERSION,
                    "installed_at": dt.datetime.now().astimezone().isoformat(),
                    "source": str(src), "files": _file_map(stage)}
        (stage / MANIFEST_NAME).write_text(json.dumps(manifest, indent=1), encoding="utf-8")
        backup = None
        if dst.exists():
            backup = dst.parent / f".knowledge-forge.bak-{dt.datetime.now():%Y%m%d%H%M%S}"
            dst.rename(backup)
        try:
            stage.rename(dst)
        except OSError:  # cross-device fallback
            shutil.copytree(stage, dst)
        if backup and dst.exists():
            shutil.rmtree(backup, ignore_errors=True)
    v = verify(cfg)
    return {"ok": v["ok"], "installed_to": str(dst), "skill_version": SKILL_VERSION,
            "verify": v}


def verify(cfg: KFConfig) -> dict:
    dst = cfg.openclaw_skill
    mpath = dst / MANIFEST_NAME
    if not dst.exists() or not mpath.exists():
        return {"ok": False, "error": "installed skill or manifest missing",
                "installed": dst.exists()}
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    current = _file_map(dst)
    drift = {k: "changed" for k in manifest["files"].keys() & current.keys()
             if manifest["files"][k] != current[k]}
    drift.update({k: "missing" for k in manifest["files"].keys() - current.keys()})
    drift.update({k: "unexpected" for k in current.keys() - manifest["files"].keys()})
    # also verify against the canonical source, not just the install-time manifest
    d = diff(cfg)
    ok = not drift and d.get("in_sync", False)
    return {"ok": ok, "skill_version": manifest.get("skill_version"),
            "manifest_drift": drift, "canonical_diff": d}
