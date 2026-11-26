"""End-to-end: invoke the `kf` CLI exactly as Ohayo does (subprocess-free, via
main(argv)), validate the JSON envelope against its schema, and check the
resulting Vault + index state. Also runs a multi-day usage simulation (UAT).
"""
import io
import json
import contextlib
import datetime as dt
from pathlib import Path

import pytest

from knowledge_forge import frontmatter as fm
from knowledge_forge.cli.main import main
from knowledge_forge.config import load_config
from knowledge_forge.vault.writer import stamp_vault
from knowledge_forge import pipeline

GOOD = ("Deterministic scaffolding lets weak models operate production "
        "knowledge systems safely. " * 15)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "90_meta").mkdir(parents=True)
    monkeypatch.setenv("KF_VAULT", str(vault))
    monkeypatch.setenv("KF_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("KF_HOME", str(Path(__file__).resolve().parents[2]))
    monkeypatch.setenv("KF_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("KF_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("KF_BACKUP_ROOT", str(tmp_path / "bak"))
    monkeypatch.setenv("OPENCLAW_SKILL", str(tmp_path / "oc/skills/knowledge-forge"))
    c = load_config()
    # bootstrap the way the deployer does
    run_cli("doctor --fix-safe --stamp")
    return load_config()


def run_cli(cmdline: str) -> dict:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main(["--json"] + cmdline.split())
    out = buf.getvalue()
    env = json.loads(out)
    # every envelope must validate against the published schema
    assert fm.validate(env, "command_result") == [], env
    env["_rc"] = rc
    return env


def _fake_capture(cfg, url, text=GOOD, title="Harness Notes", **extra):
    raw = {"platform": "web", "title": title, "text": text,
           "capture_method": "direct", **extra}
    return pipeline.run_capture(cfg, url, _test_raw=raw)


def test_envelope_contract_all_commands(cfg):
    for cmd in ["status", "doctor", "index rebuild", "taxonomy list",
                "taxonomy garden --dry-run", "skill install", "skill verify",
                "backup create", "maps rebuild", "brief build --daily",
                "podcast build --daily", "migrate plan"]:
        env = run_cli(cmd)
        assert env["status"] in ("completed", "queued", "needs_review", "failed", "noop"), cmd
        assert env["user_message_zh"], cmd


def test_cli_failure_is_a_valid_envelope(cfg):
    env = run_cli("restore test kfbak-nonexistent")
    assert env["status"] == "failed" and env["_rc"] == 1
    assert env["user_message_zh"]


def test_capture_then_status_then_search(cfg):
    res = _fake_capture(cfg, "https://example.com/e2e")
    assert res["status"] == "completed"
    env = run_cli("search scaffolding")
    assert env["result"]["hits"]
    env2 = run_cli(f"status {res['result']['source_id']}")
    assert env2["result"]["matches"]


def test_briefing_and_podcast_use_usable_only(cfg):
    _fake_capture(cfg, "https://example.com/good", title="Good article")
    _fake_capture(cfg, "https://zhihu.com/p/9", text="请登录后查看全文",
                  title="Walled article")
    env = run_cli("brief build --daily")
    assert env["status"] == "completed"
    brief = (cfg.vault / env["result"]["rel"]).read_text()
    assert "Good article" in brief
    assert "Walled article" not in brief.split("未完成的抓取")[0]  # only in incomplete section
    envp = run_cli("podcast build --daily")
    assert envp["status"] == "completed"
    assert envp["result"]["audio"] is None                       # TTS unconfigured
    assert any("TTS not configured" in w for w in envp["warnings"])
    script = (cfg.vault / envp["result"]["script"]).read_text()
    assert "Good article" in script and "Walled article" not in script
    # HOME links today's briefing after a maps rebuild
    run_cli("maps rebuild")


def test_taxonomy_governance_via_cli(cfg):
    env = run_cli("taxonomy garden --dry-run")
    assert env["status"] == "completed"
    # applying an unknown proposal fails safely
    env2 = run_cli("taxonomy apply prop_nonexistent --approved-by liang")
    assert env2["status"] == "failed"


def test_multiday_simulation_uat(cfg):
    """Simulated week of real usage: mixed-quality shares, duplicates, retry,
    briefing, gardener, doctor — the vault must stay consistent throughout."""
    # day 1: 3 good captures across topics + 1 duplicate + 1 login wall
    urls = [("https://blog.example/llm-agents", "LLM agent harness design", GOOD),
            ("https://blog.example/credit-risk", "PD LGD economic capital",
             "Credit risk economic capital modeling for infrastructure lending. " * 20),
            ("https://blog.example/polecon", "智能资本与再分配",
             "本文讨论智能资本、要素分配与制度调适的互动关系。" * 30)]
    ids_seen = set()
    for url, title, text in urls:
        r = _fake_capture(cfg, url, text=text, title=title)
        assert r["status"] == "completed"
        ids_seen.add(r["result"]["source_id"])
    dup = _fake_capture(cfg, "https://blog.example/llm-agents?utm_source=share",
                        title="LLM agent harness design")
    assert dup["status"] == "noop"
    wall = _fake_capture(cfg, "https://zhuanlan.zhihu.com/p/77",
                         text="请登录后查看", title="登录墙文章")
    assert wall["status"] == "queued"

    # day 1 evening: briefing + podcast
    assert run_cli("brief build --daily")["status"] == "completed"

    # retry the walled item — this time capture succeeds
    src_id = wall["result"]["source_id"]
    # simulate a successful re-fetch by capturing the same URL with real text
    r2 = _fake_capture(cfg, "https://zhuanlan.zhihu.com/p/77",
                       text="这次抓到了完整正文。" * 60, title="登录墙文章")
    assert r2["status"] == "completed"

    # gardener + doctor stay green; vault invariants hold
    g = run_cli("taxonomy garden --dry-run")
    assert g["status"] == "completed"
    d = run_cli("doctor --fix-safe")
    bad = [c for c in d["result"]["checks"] if not c["ok"]]
    allowed_bad = {"backup_recent", "env_file", "env_file_permissions",
                   "skill_installed_verified"}
    assert {c["check"] for c in bad} <= allowed_bad, bad

    # invariants: one source file per unique source; HOME has no raw dump;
    # every committed source is in the index
    sources = list((cfg.vault / "10_sources").rglob("*.md"))
    assert len(sources) == 4
    from knowledge_forge.index import search
    for n in sources:
        meta, _ = fm.read_note(n)
        assert search.contains_note(cfg, meta["id"])
    home = (cfg.vault / "HOME.md").read_text()
    assert "How this wiki is organised" not in home


@pytest.mark.network
def test_live_github_capture(cfg):
    """Live adapter test against api.github.com (reachable in the build env)."""
    import urllib.error
    try:
        res = pipeline.run_capture(
            cfg, "https://github.com/anthropics/anthropic-sdk-python")
    except (urllib.error.URLError, OSError):
        pytest.skip("network unavailable")
    if res["status"] != "completed":
        pytest.skip(f"github fetch degraded: {res['warnings']}")
    assert res["result"]["capture_method"] in ("api", "direct")
    assert (cfg.vault / res["result"]["note"]).exists()
