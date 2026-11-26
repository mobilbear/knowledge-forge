"""Migration tests against a representative v1.0 vault fixture, plus
backup/restore/skill/doctor operations tests."""
import json
from pathlib import Path

import pytest

from knowledge_forge.config import load_config
from knowledge_forge.operations import migrate, backup, skill, doctor
from knowledge_forge.vault.writer import stamp_vault


def make_v1_vault(root: Path) -> Path:
    """A faithful mini v1.0 vault: v1.0 frontmatter style, duplicate basenames,
    malformed YAML, generated navigation, attachments."""
    v = root / "v1vault"
    for d in ("00_inbox", "10_sources", "10_sources/old", "20_concepts",
              "30_entities", "40_maps", "50_skills", "90_meta", "_attachments"):
        (v / d).mkdir(parents=True)
    (v / "10_sources/2026-06-01-web-agent-notes.md").write_text(
        '---\ntype: source\ntitle: Agent Notes\ndescription: about agents\n'
        'tags: [llm-eng]\nlang: en\nstatus: active\nurl: https://a.com/x\n'
        'canonical_url: https://a.com/x\nplatform: web\n'
        'content_hash: sha256:' + 'a' * 64 + '\ncreated: 2026-06-01\nupdated: 2026-06-01\n'
        '---\n\n## TL;DR\n\n- agents\n\n## Source text\n\nAgent content here.\n',
        encoding="utf-8")
    # duplicate basename in a nested dir (v1.0 couldn't even see this one)
    (v / "10_sources/old/2026-06-01-web-agent-notes.md").write_text(
        '---\ntype: source\ntitle: Agent Notes (older)\ndescription: older\n'
        'tags: [quant-risk]\nlang: en\nstatus: active\nurl: https://a.com/y\n'
        'canonical_url: https://a.com/y\nplatform: web\n'
        'content_hash: sha256:' + 'b' * 64 + '\n---\n\nolder body\n', encoding="utf-8")
    (v / "20_concepts/agent-harness.md").write_text(
        '---\ntype: concept\ntitle: Agent Harness\ndescription: d\ntags: [llm-eng]\n'
        'lang: en\nstatus: active\nsources: [10_sources/2026-06-01-web-agent-notes]\n'
        '---\n\nBody linking [[2026-06-01-web-agent-notes]].\n', encoding="utf-8")
    # malformed frontmatter (real YAML parser must flag, not crash)
    (v / "20_concepts/broken.md").write_text(
        '---\ntitle: [unclosed\n---\n\nbody\n', encoding="utf-8")
    # v1.0 generated navigation to be replaced
    (v / "40_maps/_Dashboard.md").write_text("---\ntype: map\ntitle: Dashboard\n---\n\nold\n")
    (v / "40_maps/_Knowledge Map.md").write_text("---\ntype: map\ntitle: KMap\n---\n\nold\n")
    (v / "🏠 HOME.md").write_text("---\ntype: map\ntitle: HOME\n---\n\nold home\n")
    (v / "_attachments/shot.png").write_bytes(b"\x89PNG fakedata")
    return v


@pytest.fixture
def env(tmp_path, monkeypatch):
    v1 = make_v1_vault(tmp_path)
    monkeypatch.setenv("KF_VAULT", str(v1))
    monkeypatch.setenv("KF_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("KF_HOME", str(Path(__file__).resolve().parents[2]))
    monkeypatch.setenv("KF_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("KF_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("KF_BACKUP_ROOT", str(tmp_path / "bak"))
    return load_config()


def test_plan_is_readonly_and_complete(env):
    before = {p: p.read_text() for p in env.vault.rglob("*.md")}
    p = migrate.plan(env)
    after = {q: q.read_text() for q in env.vault.rglob("*.md")}
    assert before == after                                  # strictly read-only
    assert p["stats"]["total_notes"] >= 6                   # counted, not assumed
    assert p["stats"]["need_id"] >= 3
    assert any(c["basename"] == "2026-06-01-web-agent-notes"
               for c in p["basename_collisions"])
    assert any("broken" in m["path"] for m in p["malformed_frontmatter"])
    assert (env.state_home / "migration/plan.md").exists()


def test_apply_staged_then_verify(env):
    migrate.plan(env)
    r = migrate.apply(env, live=False)
    assert r["ok"], r["errors"]
    staged = Path(r["target"])
    # stable IDs added, source bodies unchanged
    from knowledge_forge import frontmatter as fm
    meta, body = fm.read_note(staged / "10_sources/2026-06-01-web-agent-notes.md")
    assert meta["id"].startswith("src_")
    assert meta["schema_version"] == "1.5.1"
    assert "Agent content here." in body                    # body untouched
    assert meta["primary_category_id"] == "cat_ai_llm_eng"  # v1 tag mapped
    # new navigation generated; old artifacts replaced
    assert (staged / "HOME.md").exists()
    assert not (staged / "40_maps/_Dashboard.md").exists()
    assert (staged / "40_maps/_All Domains.md").exists()
    # attachments survived
    assert (staged / "_attachments/shot.png").exists()
    v = migrate.verify(env)
    assert v["ok"], v
    assert v["source_count_migrated"] >= v["source_count_original"] - 0


def test_live_apply_requires_approval_and_backup(env):
    migrate.plan(env)
    r = migrate.apply(env, live=True)                       # no approval
    assert not r["ok"] and "approved-by" in r["error"]
    r = migrate.apply(env, live=True, approved_by="liang")  # no backup yet
    assert not r["ok"] and "backup" in r["error"]


def test_live_apply_and_rollback(env):
    migrate.plan(env)
    stamp_vault(env)
    backup.create(env, include_project=False)
    r = migrate.apply(env, live=True, approved_by="liang")
    assert r["ok"], r
    from knowledge_forge import frontmatter as fm
    meta, _ = fm.read_note(env.vault / "10_sources/2026-06-01-web-agent-notes.md")
    assert meta.get("id", "").startswith("src_")
    rb = migrate.rollback(env)
    assert rb["ok"]
    meta2, _ = fm.read_note(env.vault / "10_sources/2026-06-01-web-agent-notes.md")
    assert "id" not in meta2                                # pre-migration state restored


# ---- backup / restore ---------------------------------------------------------------
def test_backup_verify_and_isolated_restore(env):
    stamp_vault(env)
    r = backup.create(env, include_project=False)
    v = backup.verify(env, r["backup_id"])
    assert v["ok"]
    rt = backup.restore_test(env, r["backup_id"])
    assert rt["ok"], rt["errors"]
    assert rt["files_checked"] > 5


def test_backup_detects_corruption(env):
    stamp_vault(env)
    r = backup.create(env, include_project=False)
    ar = next((env.backup_root / r["backup_id"]).glob("vault.tar.gz"))
    data = bytearray(ar.read_bytes())
    data[len(data) // 2] ^= 0xFF
    ar.write_bytes(bytes(data))
    v = backup.verify(env, r["backup_id"])
    assert not v["ok"]


# ---- skill install / drift ---------------------------------------------------------------
def test_skill_install_verify_and_drift(env, monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCLAW_SKILL", str(tmp_path / "installed/knowledge-forge"))
    cfg = load_config()
    r = skill.install(cfg)
    assert r["ok"], r
    assert skill.verify(cfg)["ok"]
    # drift: tamper with the installed copy
    (cfg.openclaw_skill / "SKILL.md").write_text("tampered", encoding="utf-8")
    v = skill.verify(cfg)
    assert not v["ok"] and "SKILL.md" in v["manifest_drift"]
    # atomic repair
    r2 = skill.install(cfg)
    assert r2["ok"] and skill.verify(cfg)["ok"]


# ---- doctor --------------------------------------------------------------------------------
def test_doctor_detects_and_fix_safe(env, monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCLAW_SKILL", str(tmp_path / "installed/knowledge-forge"))
    cfg = load_config()
    r1 = doctor.run(cfg, fix_safe=False)
    assert not r1["ok"]                                     # unstamped, no index, no skill...
    skill.install(cfg)
    r2 = doctor.run(cfg, fix_safe=True, stamp=True)
    names_bad = {c["check"] for c in r2["checks"] if not c["ok"]}
    # after fix-safe: index rebuilt, dirs created, stamp applied
    assert "search_index" not in names_bad
    assert "vault_directories" not in names_bad
    assert "vault_stamp" not in names_bad
    assert any("rebuilt search index" in f for f in r2["fixes_applied"])
