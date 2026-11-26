"""Daily briefing, weekly synthesis, podcast script and optional TTS.

Hard rules honoured here:
- usable material only (quarantined captures are excluded by construction);
- briefing/audio failure never blocks capture (this module is only invoked by
  `kf brief` / `kf podcast`, never inside the capture pipeline);
- if TTS is unconfigured or fails, the script remains the valid deliverable
  and the failure is reported truthfully.
"""
from __future__ import annotations

import datetime as dt
import shlex
import subprocess
from typing import Optional

from ..config import KFConfig
from ..discovery import VaultIndex, rel
from ..enrichment import harness
from ..vault import transactions as txn
from ..vault.writer import verify_vault
from ..index import search


def _usable_sources(cfg: KFConfig, vi: VaultIndex, since: dt.date, until: dt.date) -> list:
    out = []
    for n in vi.notes:
        if n.parse_error or n.note_type != "source":
            continue
        if n.meta.get("capture_status") not in ("complete", "usable_partial"):
            continue
        cap = str(n.meta.get("captured_at", ""))[:10]
        if cap and since.isoformat() <= cap <= until.isoformat():
            out.append(n)
    return out


def _incomplete(cfg: KFConfig, vi: VaultIndex) -> list:
    return [n for n in vi.notes
            if rel(cfg.vault, n.path).startswith("80_queues/capture_retry/")]


def _tldr_of(n) -> str:
    body = n.body
    if "## TL;DR" in body:
        seg = body.split("## TL;DR", 1)[1].split("##", 1)[0]
        return seg.strip()[:600]
    return body.strip()[:400]


def build_briefing(cfg: KFConfig, kind: str = "daily",
                   date: Optional[dt.date] = None,
                   provider: Optional[harness.Provider] = None) -> dict:
    """Generate the briefing note content. Returns {rel, content, items, model_used}."""
    date = date or dt.date.today()
    since = date if kind == "daily" else date - dt.timedelta(days=6)
    vi = VaultIndex.build(cfg.vault)
    items = _usable_sources(cfg, vi, since, date)
    incomplete = _incomplete(cfg, vi)

    model_notes, model_used = {}, False
    top_ideas, followups, connections = [], [], []
    if items and provider is not None:
        src_block = "\n".join(
            f"{n.meta['id']} | {n.meta.get('title','')} | {_tldr_of(n)[:300]}" for n in items[:20])
        prompt = harness.load_prompt("synthesize", "v1").format(sources=src_block)
        try:
            res = harness.call_with_contract(provider, prompt, "briefing", max_tokens=1600)
            if res.ok and not res.data.get("abstained"):
                model_used = True
                model_notes = {x["source_id"]: x["note"] for x in res.data["item_notes"]}
                top_ideas = res.data.get("top_ideas", [])
                followups = res.data.get("followups", [])
                connections = res.data.get("connections", [])
        except Exception:
            # Narration is optional (ARCHITECTURE: model failures degrade to a
            # deterministic brief). Any provider error — unavailable model, bad
            # endpoint/URL, timeout, malformed response — must fall back to the
            # extractive brief instead of crashing the whole command.
            pass

    title = f"{'每日简报' if kind == 'daily' else '每周综合'} {date.isoformat()}"
    L = [f"# {title}", ""]
    L.append(f"## 今日收录 / Captured ({len(items)})" if kind == "daily"
             else f"## 本周收录 / Captured this week ({len(items)})")
    for n in items:
        r = rel(cfg.vault, n.path)
        L.append(f"### [[{r}|{n.meta.get('title','')}]]")
        note = model_notes.get(n.meta.get("id"))
        L.append(note if note else _tldr_of(n))
        L.append("")
    if not items:
        L += ["_(no usable captures in this period)_", ""]
    if incomplete:
        # queue items are transient (they get promoted or cleared); reference
        # by title so historical briefings never hold broken links
        L += [f"## 未完成的抓取 / Incomplete captures ({len(incomplete)})"]
        L += [f"- ⚠️ {n.meta.get('title','(untitled)')} — "
              f"{n.meta.get('capture_status','')}（见 80_queues 队列）"
              for n in incomplete[:10]]
        L.append("")
    if connections:
        L += ["## 与已有概念的连接 / Connections"] + [f"- {c}" for c in connections] + [""]
    if top_ideas:
        L += ["## 今日三个最值得记住的想法 / Top 3 ideas"] + [f"1. {i}" for i in top_ideas] + [""]
    if followups:
        L += ["## 值得深挖 / Follow-ups"] + [f"- {f}" for f in followups] + [""]
    if not model_used:
        L += ["> _注: 本简报为确定性汇编（无模型润色）。TL;DR 摘自各来源笔记。_"]

    sub = "daily" if kind == "daily" else "weekly"
    relpath = f"60_briefings/{sub}/{date.isoformat()}-{sub}.md"
    content = (f"---\nid: brf_{sub}_{date.isoformat().replace('-', '')}\n"
               f"note_type: briefing\ntitle: \"{title}\"\nupdated: {date.isoformat()}\n---\n\n"
               + "\n".join(L) + "\n")
    return {"rel": relpath, "content": content, "items": len(items),
            "model_used": model_used}


def commit_briefing(cfg: KFConfig, built: dict) -> dict:
    verify_vault(cfg)
    t = txn.new_transaction(cfg.state_home, cfg.vault, meta={"action": "briefing"})
    t.stage_write(built["rel"], built["content"])
    with txn.VaultLock(cfg.lock_path):
        t.commit(after_commit=lambda: search.refresh_after_commit(cfg))
    return {"note": built["rel"]}


# ---- podcast -------------------------------------------------------------------------
def build_podcast_script(cfg: KFConfig, kind: str = "daily",
                         date: Optional[dt.date] = None,
                         provider: Optional[harness.Provider] = None) -> dict:
    """8–15 minute conversational script assembled from usable material."""
    date = date or dt.date.today()
    briefing = build_briefing(cfg, kind, date, provider)
    vi = VaultIndex.build(cfg.vault)
    since = date if kind == "daily" else date - dt.timedelta(days=6)
    items = _usable_sources(cfg, vi, since, date)
    L = [f"# 知识播客脚本 — {date.isoformat()}", "",
         "（口播稿：自然语速约 8–15 分钟）", "",
         f"大家好，这是 {date.isoformat()} 的知识简报。今天一共收录了 {len(items)} 条新内容。", ""]
    for i, n in enumerate(items, 1):
        L += [f"第{i}条：《{n.meta.get('title','')}》。", _tldr_of(n).replace("- ", ""), ""]
    L += ["以上就是今天的全部内容。明天见。"]
    relpath = f"60_briefings/scripts/{date.isoformat()}-{kind}-podcast.md"
    content = (f"---\nid: brf_pod_{date.isoformat().replace('-', '')}\nnote_type: briefing\n"
               f"title: \"Podcast script {date.isoformat()}\"\nupdated: {date.isoformat()}\n---\n\n"
               + "\n".join(L) + "\n")
    return {"rel": relpath, "content": content, "items": len(items)}


def synthesize_audio(cfg: KFConfig, script_rel: str) -> dict:
    """Optional TTS through a user-configured shell command (KF_TTS_COMMAND,
    receiving INPUT and OUTPUT paths). No command configured → honest skip."""
    cmd_tpl = cfg.secret("KF_TTS_COMMAND")
    script_path = cfg.vault / script_rel
    if not cmd_tpl:
        return {"ok": False, "reason": "TTS not configured (set KF_TTS_COMMAND); "
                                       "the script is still available", "audio": None}
    out_rel = script_rel.replace("scripts/", "audio/").replace(".md", ".mp3")
    out_path = cfg.vault / out_rel
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [c.replace("INPUT", str(script_path)).replace("OUTPUT", str(out_path))
           for c in shlex.split(cmd_tpl)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if r.returncode != 0 or not out_path.exists():
            return {"ok": False, "reason": f"TTS command failed (rc={r.returncode}): "
                                           f"{r.stderr[:200]}", "audio": None}
    except Exception as e:
        return {"ok": False, "reason": f"TTS invocation error: {e}", "audio": None}
    return {"ok": True, "reason": None, "audio": out_rel}
