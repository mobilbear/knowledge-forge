"""`kf` — the single deterministic entry point Ohayo calls.

Every command supports --json and returns the common envelope
(schemas/command_result.schema.json). A success envelope is emitted only after
commit and index verification. Ohayo relays user_message_zh.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import traceback

from .. import ids
from .. import frontmatter as fm
from ..config import load_config
from ..discovery import VaultIndex
from ..taxonomy.registry import TaxonomyRegistry, GovernanceError
from ..taxonomy import gardener
from ..index import search
from ..briefing import builder as briefing
from ..enrichment import harness
from ..operations import backup as backup_ops
from ..operations import doctor as doctor_ops
from ..operations import migrate as migrate_ops
from ..operations import skill as skill_ops
from ..vault import transactions as txn
from .. import pipeline
from ..capture import jobs


def envelope(command: str, status: str, result=None, warnings=None, errors=None,
             needs_user_action=False, zh="", run: str | None = None) -> dict:
    env = {"schema_version": "1.0", "command": command,
           "run_id": run or ids.run_id(), "status": status,
           "result": result if isinstance(result, (dict, list)) or result is None else {"value": result},
           "warnings": warnings or [], "errors": errors or [],
           "needs_user_action": bool(needs_user_action), "user_message_zh": zh}
    problems = fm.validate(env, "command_result")
    if problems:  # never emit an invalid envelope
        env = {"schema_version": "1.0", "command": command, "run_id": env["run_id"],
               "status": "failed", "result": None, "warnings": [],
               "errors": [f"internal envelope validation: {problems}"],
               "needs_user_action": True,
               "user_message_zh": "内部返回格式错误，请运行 kf doctor。"}
    return env


def emit(env: dict, as_json: bool) -> int:
    if as_json:
        print(json.dumps(env, ensure_ascii=False, indent=1))
    else:
        print(env["user_message_zh"])
        for w in env["warnings"]:
            print(f"  ⚠ {w}")
        for e in env["errors"]:
            print(f"  ✗ {e}", file=sys.stderr)
    return 0 if env["status"] in ("completed", "queued", "noop") else 1


# ---- command handlers ---------------------------------------------------------------
def cmd_capture(a, cfg):
    if a.transcribe_preview and a.what not in ("url", "video", "drain"):
        raise ValueError("transcript previews support URL, video, and queued-link capture")
    if a.follow_references and a.what not in ("url", "video", "drain"):
        raise ValueError("reference review supports URL, video, and queued-link capture")
    if a.what in ("url", "video"):
        res = pipeline.run_capture(cfg, a.target, kind="url", note=a.note, profile=a.profile,
                                   public_fallback=a.public_fallback, transcribe_preview=a.transcribe_preview,
                                   follow_references=a.follow_references)
    elif a.what == "images":
        res = pipeline.run_capture(cfg, a.target[0], kind="images",
                                   image_paths=a.target, profile=a.profile,
                                   note=a.note, source_url=a.source_url)
    elif a.what == "file":
        res = pipeline.run_capture(cfg, a.target, kind="file", profile=a.profile,
                                   note=a.note, source_url=a.source_url)
    elif a.what == "submit":
        receipt = jobs.submit(cfg, a.target, a.note)
        return envelope("capture.submit", "queued", receipt,
                        zh=f"已收到并记录。收据编号 {receipt['receipt']} · {receipt['status']}")
    elif a.what == "drain":
        from functools import partial
        handler = partial(pipeline.run_capture, public_fallback=a.public_fallback,
                          transcribe_preview=a.transcribe_preview, follow_references=a.follow_references)
        receipt = jobs.process(cfg, handler, a.profile, a.target[0] if a.target else None)
        if receipt is None:
            return envelope("capture.drain", "noop", zh="没有已到期的待处理收据。")
        state = receipt["status"]
        return envelope("capture.drain", "completed" if state in ("archived", "partial") else "queued",
                        receipt, needs_user_action=state in ("partial", "needs_input"),
                        zh=f"收据 {receipt['receipt']} · {state} · {receipt['result_path'] or '无存档'}")
    elif a.what == "retry":
        res = pipeline.retry_capture(cfg, a.target, profile=a.profile)
    else:
        raise ValueError(a.what)
    return envelope(f"capture.{a.what}", res["status"], res.get("result"),
                    res["warnings"], res["errors"], res["needs_user_action"],
                    res["user_message_zh"], run=res["run_id"])


def cmd_status(a, cfg):
    if a.ref and a.ref.startswith("job_"):
        result = jobs.lookup(cfg, a.ref)
        return envelope("status", "completed" if result else "noop", result,
                        zh=f"收据 {a.ref} · {result['status'] if result else '未找到'}")
    st = search.status(cfg)
    inc = txn.find_incomplete(cfg.state_home)
    qn = sum(1 for note in VaultIndex.build(cfg.vault).notes
             if note.path.relative_to(cfg.vault).parts[0] == "80_queues"
             and note.note_type != "resolved_capture")
    if a.ref:
        hits = search.query(cfg, a.ref, n=3, include_quarantined=True)
        res = {"lookup": a.ref, "matches": hits}
        zh = (f"找到 {len(hits)} 条相关记录。" if hits else "没有找到相关记录。")
    else:
        res = {"index": st, "incomplete_transactions": len(inc), "queued_items": qn}
        zh = (f"系统正常：索引含 {st.get('note_count', 0)} 条笔记"
              f"{'（需重建）' if st.get('stale') else ''}，队列 {qn} 条，"
              f"未完成事务 {len(inc)} 个。")
    return envelope("status", "completed", res, zh=zh)


def cmd_search(a, cfg):
    hits = search.query(cfg, a.query, n=a.n)
    zh = f"搜索「{a.query}」共 {len(hits)} 条结果。"
    return envelope("search", "completed", {"hits": hits}, zh=zh)


def cmd_library(a, cfg):
    if a.action == "init":
        if a.cards:
            raise ValueError("library init uses the configured KF_VAULT")
        pipeline.initialize_library(cfg)
        return envelope("library.init", "completed", {"vault": str(cfg.vault), "index": search.status(cfg)},
                        zh=f"知识库已初始化：{cfg.vault}")
    if a.action == "edition":
        if len(a.cards) != 2:
            raise ValueError("edition expects the triggering source and a Markdown edition draft")
        result = pipeline.publish_edition(cfg, *a.cards)
        return envelope("library.edition", "completed", result, zh=f"个人简报已更新：{result['edition']}")
    if a.action == "lesson":
        if len(a.cards) != 2:
            raise ValueError("lesson expects an archived source and a Markdown lesson draft")
        lesson = pipeline.attach_lesson(cfg, *a.cards)
        return envelope("library.lesson", "completed", lesson, zh=f"已链接导读课程：{lesson['lesson']}")
    if a.action == "resources":
        if len(a.cards) != 1:
            raise ValueError("resources expects one archived source note ID or path")
        review = pipeline.review_resources(cfg, a.cards[0])
        return envelope("library.resources", "completed", review,
                        zh=f"找到 {review['links']} 条正文引用，已检查 {len(review['evidence'])} 条链接。")
    if a.action == "import-clips":
        if a.cards:
            raise ValueError("import-clips reads 00_inbox/Web Clips without arguments")
        clips = pipeline.import_web_clips(cfg)
        return envelope("library.import-clips", "failed" if clips["errors"] else "completed", clips,
                        errors=clips["errors"], needs_user_action=bool(clips["errors"]),
                        zh=f"已处理 {len(clips['filed'])} 条浏览器剪藏，跳过 {clips['unchanged']} 条未变更剪藏。")
    if a.action == "reading-guides":
        if not a.cards:
            raise ValueError("provide at least one reading-guide JSON file")
        count = pipeline.apply_reading_guides(cfg, a.cards)
        return envelope("library.reading-guides", "completed", {"updated_sources": count},
                        zh=f"已更新 {count} 篇阅读导览，原文与笔记保留。")
    if a.action == "prepare":
        prepared = pipeline.prepare_library(cfg)
        return envelope("library.prepare", "completed", {"prepared_sources": prepared},
                        zh=f"知识库导航已更新，本次补充 {prepared} 条旧资料。")
    pipeline.rebuild_library(cfg)
    return envelope("library.rebuild", "completed", {"index": search.status(cfg)},
                    zh="知识库导航和本地索引已更新。")


def cmd_taxonomy(a, cfg):
    reg = TaxonomyRegistry.load(cfg.taxonomy_registry_path)
    if a.action == "garden":
        vi = VaultIndex.build(cfg.vault)
        rep = gardener.garden(cfg, vi, reg)
        zh = (f"园丁报告（未做任何修改）：{len(rep['proposals'])} 条建议，"
              f"{len(rep['provisional_awaiting_review'])} 个待审临时类目，"
              f"{len(rep['orphaned_notes'])} 条未分类笔记。")
        return envelope("taxonomy.garden", "completed", rep, zh=zh)
    if a.action == "apply":
        try:
            res = gardener.apply_proposal(cfg, reg, a.proposal, a.approved_by or "")
        except GovernanceError as e:
            return envelope("taxonomy.apply", "failed", None, errors=[str(e)],
                            needs_user_action=True, zh=f"未执行：{e}")
        return envelope("taxonomy.apply", "completed", res,
                        zh=f"已执行建议 {res['applied']}（{res['kind']}），批准人 {res['approved_by']}。")
    if a.action == "list":
        nodes = [{"id": n["category_id"], "title": n["title"], "level": n["level"],
                  "status": n["status"]} for n in reg.active()]
        return envelope("taxonomy.list", "completed", {"nodes": nodes},
                        zh=f"当前分类树共 {len(nodes)} 个活跃节点。")
    raise ValueError(a.action)


def cmd_maps(a, cfg):
    files = pipeline.rebuild_library(cfg, scope=a.scope)
    return envelope("maps.rebuild", "completed", {"files": files},
                    zh=f"已重建 {len(files)} 个导航页（HOME/子主页/地图），索引已刷新。")


def cmd_index(a, cfg):
    res = search.rebuild(cfg)
    return envelope("index.rebuild", "completed", res,
                    zh=f"索引已重建：{res['count']} 条笔记（{'FTS5' if res['fts5'] else 'LIKE'}）。")


def _provider_for(cfg, profile):
    profiles = harness.load_profiles(cfg.kf_home / "config")
    return harness.provider_for_profile(profile, profiles, cfg.secret)


def cmd_brief(a, cfg):
    kind = "weekly" if a.weekly else "daily"
    date = dt.date.fromisoformat(a.date) if a.date else None
    built = briefing.build_briefing(cfg, kind, date, _provider_for(cfg, a.profile))
    res = briefing.commit_briefing(cfg, built)
    zh = (f"{'每周综合' if kind == 'weekly' else '每日简报'}已生成（{built['items']} 条内容"
          f"{'，含模型润色' if built['model_used'] else '，确定性汇编'}）：{res['note']}")
    return envelope(f"brief.{kind}", "completed", {**built, **res, "content": None}, zh=zh)


def cmd_podcast(a, cfg):
    kind = "weekly" if a.weekly else "daily"
    date = dt.date.fromisoformat(a.date) if a.date else None
    built = briefing.build_podcast_script(cfg, kind, date, _provider_for(cfg, a.profile))
    briefing.commit_briefing(cfg, built)
    audio = briefing.synthesize_audio(cfg, built["rel"])
    warnings = [] if audio["ok"] else [f"audio not generated: {audio['reason']}"]
    zh = (f"播客脚本已生成：{built['rel']}"
          + (f"，音频：{audio['audio']}" if audio["ok"] else "（音频未生成，脚本仍可用）"))
    return envelope(f"podcast.{kind}", "completed",
                    {"script": built["rel"], "audio": audio["audio"]},
                    warnings=warnings, zh=zh)


def cmd_skill(a, cfg):
    if a.action == "diff":
        d = skill_ops.diff(cfg)
        zh = "技能副本一致。" if d.get("in_sync") else f"技能副本存在漂移：{d}"
        return envelope("skill.diff", "completed", d, zh=zh)
    if a.action == "install":
        r = skill_ops.install(cfg)
        return envelope("skill.install", "completed" if r["ok"] else "failed", r,
                        errors=[] if r["ok"] else [str(r)],
                        zh="技能已原子安装并校验通过。" if r["ok"] else "技能安装校验失败。")
    if a.action == "verify":
        r = skill_ops.verify(cfg)
        return envelope("skill.verify", "completed" if r["ok"] else "failed", r,
                        errors=[] if r["ok"] else ["skill drift detected"],
                        zh="已安装技能与规范源一致。" if r["ok"] else "检测到技能漂移，请运行 kf skill install。")
    raise ValueError(a.action)


def cmd_backup(a, cfg):
    if a.action == "create":
        r = backup_ops.create(cfg)
        v = backup_ops.verify(cfg, r["backup_id"])
        st = "completed" if v["ok"] else "failed"
        return envelope("backup.create", st, {**r, "verified": v["ok"]},
                        errors=v["errors"],
                        zh=f"备份 {r['backup_id']} 已创建并校验{'通过' if v['ok'] else '失败'}。")
    if a.action == "verify":
        v = backup_ops.verify(cfg, a.backup_id)
        return envelope("backup.verify", "completed" if v["ok"] else "failed", v,
                        errors=v["errors"],
                        zh="备份校验通过。" if v["ok"] else "备份校验失败！")
    raise ValueError(a.action)


def cmd_restore(a, cfg):
    r = backup_ops.restore_test(cfg, a.backup_id)
    return envelope("restore.test", "completed" if r["ok"] else "failed", r,
                    errors=r.get("errors", []),
                    zh=(f"隔离恢复测试通过（校验 {r.get('files_checked', 0)} 个文件）。"
                        if r["ok"] else "隔离恢复测试失败！"))


def cmd_migrate(a, cfg):
    if a.action == "plan":
        p = migrate_ops.plan(cfg)
        zh = (f"迁移演练完成（未修改任何文件）：{p['stats']['total_notes']} 条笔记，"
              f"{p['stats']['need_id']} 条需要补 ID，"
              f"{len(p['basename_collisions'])} 个同名冲突。报告见 state/migration/plan.md")
        return envelope("migrate.plan", "completed", p["stats"], zh=zh)
    if a.action == "apply":
        r = migrate_ops.apply(cfg, live=a.live, approved_by=a.approved_by)
        st = "completed" if r["ok"] else "failed"
        zh = (f"迁移已应用到{'生产库' if a.live else '暂存副本'}：{r.get('applied', 0)} 项变更。"
              if r["ok"] else f"迁移失败：{r.get('error') or r.get('errors')}")
        return envelope("migrate.apply", st, r, errors=r.get("errors", []),
                        needs_user_action=not r["ok"], zh=zh)
    if a.action == "verify":
        r = migrate_ops.verify(cfg)
        return envelope("migrate.verify", "completed" if r["ok"] else "failed", r,
                        errors=[] if r["ok"] else ["migration verification failed"],
                        zh="迁移校验通过：所有来源、URL、哈希、附件完整。" if r["ok"]
                        else "迁移校验失败！")
    if a.action == "rollback":
        r = migrate_ops.rollback(cfg)
        return envelope("migrate.rollback", "completed" if r["ok"] else "failed", r,
                        errors=[] if r["ok"] else [r.get("error", "")],
                        zh="已回滚到迁移前检查点。" if r["ok"] else f"回滚失败：{r.get('error')}")
    raise ValueError(a.action)


def cmd_doctor(a, cfg):
    r = doctor_ops.run(cfg, fix_safe=a.fix_safe, stamp=a.stamp)
    bad = [c for c in r["checks"] if not c["ok"]]
    zh = ("系统体检全部通过。" if r["ok"]
          else "体检发现问题：" + "；".join(f"{c['check']}（{c['detail']}）" for c in bad[:5]))
    if r["fixes_applied"]:
        zh += f" 已安全修复：{len(r['fixes_applied'])} 项。"
    return envelope("doctor", "completed" if r["ok"] else "needs_review", r,
                    needs_user_action=not r["ok"], zh=zh)


# ---- parser -----------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kf", description="Knowledge Forge v1.5.1")
    p.add_argument("--json", action="store_true", help="emit the JSON envelope")
    p.add_argument("--profile", default="low", choices=["high", "standard", "low", "offline"])
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("capture")
    c.add_argument("what", choices=["url", "images", "video", "file", "retry", "submit", "drain"])
    c.add_argument("target", nargs="*")
    c.add_argument("--note", default=None)
    c.add_argument("--source-url", default=None, help="attach a local file or image set to an existing share")
    c.add_argument("--public-fallback", action="store_true",
                   help="allow Firecrawl for a public Zhihu or Toutiao article")
    c.add_argument("--transcribe-preview", action="store_true",
                   help="transcribe up to 18 seconds of accessible audio locally")
    c.add_argument("--follow-references", action="store_true",
                   help="review cited public resources after the primary source is saved")
    c.set_defaults(func=cmd_capture)

    s = sub.add_parser("status"); s.add_argument("ref", nargs="?"); s.set_defaults(func=cmd_status)
    s = sub.add_parser("search"); s.add_argument("query"); s.add_argument("-n", type=int, default=10)
    s.set_defaults(func=cmd_search)
    library = sub.add_parser("library")
    library.add_argument("action", choices=["init", "prepare", "rebuild", "reading-guides", "import-clips", "resources", "lesson", "edition"])
    library.add_argument("cards", nargs="*")
    library.set_defaults(func=cmd_library)

    t = sub.add_parser("taxonomy")
    t.add_argument("action", choices=["garden", "apply", "list"])
    t.add_argument("proposal", nargs="?")
    t.add_argument("--dry-run", action="store_true")  # garden is always dry-run
    t.add_argument("--approved-by", default=None)
    t.set_defaults(func=cmd_taxonomy)

    m = sub.add_parser("maps"); m.add_argument("action", choices=["rebuild"])
    m.add_argument("--scope", default=None); m.set_defaults(func=cmd_maps)

    i = sub.add_parser("index"); i.add_argument("action", choices=["rebuild"])
    i.set_defaults(func=cmd_index)

    b = sub.add_parser("brief"); b.add_argument("action", choices=["build"])
    b.add_argument("--daily", action="store_true"); b.add_argument("--weekly", action="store_true")
    b.add_argument("--date", default=None); b.set_defaults(func=cmd_brief)

    pc = sub.add_parser("podcast"); pc.add_argument("action", choices=["build"])
    pc.add_argument("--daily", action="store_true"); pc.add_argument("--weekly", action="store_true")
    pc.add_argument("--date", default=None); pc.set_defaults(func=cmd_podcast)

    sk = sub.add_parser("skill"); sk.add_argument("action", choices=["diff", "install", "verify"])
    sk.set_defaults(func=cmd_skill)

    bk = sub.add_parser("backup"); bk.add_argument("action", choices=["create", "verify"])
    bk.add_argument("backup_id", nargs="?"); bk.set_defaults(func=cmd_backup)

    rs = sub.add_parser("restore"); rs.add_argument("action", choices=["test"])
    rs.add_argument("backup_id"); rs.set_defaults(func=cmd_restore)

    mg = sub.add_parser("migrate")
    mg.add_argument("action", choices=["plan", "apply", "verify", "rollback"])
    mg.add_argument("--live", action="store_true")
    mg.add_argument("--approved-by", default=None)
    mg.set_defaults(func=cmd_migrate)

    d = sub.add_parser("doctor")
    d.add_argument("--fix-safe", action="store_true")
    d.add_argument("--stamp", action="store_true")
    d.set_defaults(func=cmd_doctor)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    # capture takes one target for url/file/retry, many for images
    if getattr(args, "what", None) in ("url", "video", "file", "retry", "submit"):
        if len(args.target) != 1:
            raise SystemExit("capture requires one target")
        args.target = args.target[0]
    if getattr(args, "what", None) == "images" and not args.target:
        raise SystemExit("capture images requires at least one file")
    try:
        cfg = load_config()
        env = args.func(args, cfg)
    except Exception as e:
        env = envelope(getattr(args, "cmd", "kf"), "failed", None,
                       errors=[f"{type(e).__name__}: {e}"],
                       needs_user_action=True,
                       zh=f"命令执行失败：{type(e).__name__}: {e}")
        if "--debug" in (argv or sys.argv):
            traceback.print_exc()
    return emit(env, args.json)


if __name__ == "__main__":
    sys.exit(main())
