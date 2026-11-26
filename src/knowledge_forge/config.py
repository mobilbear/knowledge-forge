"""Typed configuration layer for Knowledge Forge v1.5.1.

Single source of truth for paths and settings. Loads, in order of precedence:
  1. process environment variables (KF_*, OPENCLAW_*)
  2. the env file at $KF_CONFIG_HOME/kf.env
  3. defaults relative to the user's home directory and installed project data

Every entry point (CLI, OpenClaw-launched process, systemd unit, script) calls
``load_config()`` so behaviour does not depend on .bashrc.
No secrets are ever logged or written into the Vault.
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

SOURCE_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = (SOURCE_ROOT if (SOURCE_ROOT / "schemas").is_dir()
             else Path(sys.prefix) / "share" / "knowledge-forge")

#: Documented production defaults (also used to generate kf.env.example).
PRODUCTION_DEFAULTS = {
    "KF_HOME": str(DATA_ROOT),
    "KF_VAULT": "~/Documents/ObsidianVaults/LiangWiki",
    "OPENCLAW_HOME": "~/.openclaw",
    "OPENCLAW_WORKSPACE": "~/.openclaw/workspace",
    "OPENCLAW_SKILL": "~/.openclaw/workspace/skills/knowledge-forge",
    "KF_CONFIG_HOME": "~/.config/knowledge-forge",
    "KF_STATE_HOME": "~/.local/state/knowledge-forge",
    "KF_CACHE_HOME": "~/.cache/knowledge-forge",
    "KF_BACKUP_ROOT": "~/Backups/KnowledgeForge",
}

_SECRET_KEYS = re.compile(r"(KEY|TOKEN|SECRET|COOKIE|PASSWORD)", re.I)
_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=\s*(.*?)\s*$")


def parse_env_file(path: Path) -> dict:
    """Parse a simple KEY=VALUE env file (supports 'export', quotes, comments)."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _ENV_LINE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[key] = val
    return out


@dataclass
class KFConfig:
    kf_home: Path
    vault: Path
    openclaw_home: Path
    openclaw_workspace: Path
    openclaw_skill: Path
    config_home: Path
    state_home: Path
    cache_home: Path
    backup_root: Path
    env_file: Path
    #: raw merged environment (secrets included; never serialise this wholesale)
    raw: dict = field(default_factory=dict, repr=False)

    # ---- derived vault paths -----------------------------------------------------
    @property
    def dir_inbox(self) -> Path: return self.vault / "00_inbox"
    @property
    def dir_sources(self) -> Path: return self.vault / "10_sources"
    @property
    def dir_concepts(self) -> Path: return self.vault / "20_concepts"
    @property
    def dir_entities(self) -> Path: return self.vault / "30_entities"
    @property
    def dir_maps(self) -> Path: return self.vault / "40_maps"
    @property
    def dir_briefings(self) -> Path: return self.vault / "60_briefings"
    @property
    def dir_media(self) -> Path: return self.vault / "70_media"
    @property
    def dir_queues(self) -> Path: return self.vault / "80_queues"
    @property
    def dir_meta(self) -> Path: return self.vault / "90_meta"
    @property
    def taxonomy_registry_path(self) -> Path:
        return self.dir_meta / "taxonomy" / "registry.yaml"
    @property
    def vault_marker(self) -> Path:
        return self.dir_meta / ".kf_vault_id"
    @property
    def home_note(self) -> Path:
        return self.vault / "HOME.md"

    # ---- derived state paths -----------------------------------------------------
    @property
    def staging_root(self) -> Path: return self.state_home / "staging"
    @property
    def journal_root(self) -> Path: return self.state_home / "journal"
    @property
    def log_root(self) -> Path: return self.state_home / "logs"
    @property
    def lock_path(self) -> Path: return self.state_home / "vault.lock"
    @property
    def index_db(self) -> Path: return self.state_home / "index" / "kf-index.sqlite"
    @property
    def queue_db(self) -> Path: return self.state_home / "queues.sqlite"

    def secret(self, key: str) -> Optional[str]:
        """Fetch a secret value; never log the result."""
        return self.raw.get(key) or os.environ.get(key)

    def public_dict(self) -> dict:
        """Paths only — safe to log / print. Secrets are excluded."""
        return {
            "KF_HOME": str(self.kf_home), "KF_VAULT": str(self.vault),
            "OPENCLAW_HOME": str(self.openclaw_home),
            "OPENCLAW_WORKSPACE": str(self.openclaw_workspace),
            "OPENCLAW_SKILL": str(self.openclaw_skill),
            "KF_CONFIG_HOME": str(self.config_home),
            "KF_STATE_HOME": str(self.state_home),
            "KF_CACHE_HOME": str(self.cache_home),
            "KF_BACKUP_ROOT": str(self.backup_root),
            "env_file": str(self.env_file),
        }


def load_config(env_file: Optional[str] = None, require_vault: bool = False) -> KFConfig:
    """Merge process env > kf.env file > production defaults into a typed config."""
    env_file_path = Path(
        env_file
        or os.environ.get("KF_ENV_FILE")
        or os.environ.get("KF_CONFIG_HOME", PRODUCTION_DEFAULTS["KF_CONFIG_HOME"]) + "/kf.env"
    ).expanduser()
    file_env = parse_env_file(env_file_path)

    def get(key: str) -> str:
        return os.environ.get(key) or file_env.get(key) or PRODUCTION_DEFAULTS[key]

    merged = dict(PRODUCTION_DEFAULTS)
    merged.update(file_env)
    merged.update({k: v for k, v in os.environ.items() if k.startswith(("KF_", "OPENCLAW_"))})

    cfg = KFConfig(
        kf_home=Path(get("KF_HOME")).expanduser(),
        vault=Path(get("KF_VAULT")).expanduser(),
        openclaw_home=Path(get("OPENCLAW_HOME")).expanduser(),
        openclaw_workspace=Path(get("OPENCLAW_WORKSPACE")).expanduser(),
        openclaw_skill=Path(get("OPENCLAW_SKILL")).expanduser(),
        config_home=Path(get("KF_CONFIG_HOME")).expanduser(),
        state_home=Path(get("KF_STATE_HOME")).expanduser(),
        cache_home=Path(get("KF_CACHE_HOME")).expanduser(),
        backup_root=Path(get("KF_BACKUP_ROOT")).expanduser(),
        env_file=env_file_path,
        raw=merged,
    )
    if require_vault and not cfg.vault.exists():
        raise FileNotFoundError(
            f"KF_VAULT does not exist: {cfg.vault}\n"
            f"Set KF_VAULT in the environment or in {env_file_path}."
        )
    return cfg


def redact(mapping: dict) -> dict:
    """Return a copy with secret-looking values masked, for safe logging."""
    return {k: ("***" if _SECRET_KEYS.search(k) else v) for k, v in mapping.items()}


def write_env_example(dest: Path) -> None:
    lines = [
        "# Knowledge Forge v1.5.1 — canonical environment file",
        "# Install to: ~/.config/knowledge-forge/kf.env   (chmod 0600 if it holds secrets)",
        "# Loaded explicitly by the CLI, OpenClaw-launched processes, systemd units and scripts.",
        "",
    ]
    lines += [f"{k}={v}" for k, v in PRODUCTION_DEFAULTS.items()]
    lines += [
        "",
        "# ---- optional capture / model secrets (never written to Vault, logs or prompts) ----",
        "#KF_JINA_KEY=",
        "#KF_COOKIES_ZHIHU=",
        "#KF_COOKIES_XIAOHONGSHU=",
        "#KF_BROWSER_PROFILE=~/.kf-browser",
        "#KF_MEDIACRAWLER=",
        "#KF_MINIMAX_API_KEY=",
        "#KF_MINIMAX_MODEL=MiniMax-Text-01",
        "#KF_MINIMAX_BASE_URL=https://api.minimax.chat/v1",
        "#KF_ANTHROPIC_KEY=",
        "#KF_TTS_COMMAND=   # e.g. a local piper/edge-tts wrapper; leave unset to disable audio",
    ]
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
