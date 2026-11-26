"""Taxonomy gardener. `garden()` is strictly read-only analysis producing
proposals; `apply_proposal()` executes exactly one approved proposal through
the transaction layer. No L1 mutation ever occurs without approved_by.
"""
from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict

from .. import ids
from ..config import KFConfig
from ..discovery import VaultIndex, rel
from .registry import TaxonomyRegistry, GovernanceError


def garden(cfg: KFConfig, vi: VaultIndex, registry: TaxonomyRegistry) -> dict:
    """Dry-run report: near-duplicates, shell topics, overgrown categories,
    provisional nodes, orphans, and merge/promote proposals."""
    by_cat = defaultdict(list)
    all_tags = defaultdict(int)
    orphans = []
    for n in vi.notes:
        if n.parse_error:
            continue
        cid = n.meta.get("primary_category_id")
        if cid:
            by_cat[cid].append(n)
        elif n.note_type == "source" and not rel(cfg.vault, n.path).startswith("80_queues/"):
            orphans.append(rel(cfg.vault, n.path))
        for t in n.meta.get("tags") or []:
            all_tags[ids.normalize_tag(t)] += 1

    # near-duplicate categories (normalized title/alias collision across nodes)
    seen, near_dupes = {}, []
    for node in registry.active():
        for name in [node["title"]] + (node.get("aliases") or []):
            k = ids.normalize_tag(name)
            if k in seen and seen[k] != node["category_id"]:
                near_dupes.append({"a": seen[k], "b": node["category_id"], "name": name})
            seen.setdefault(k, node["category_id"])

    shells = [{"category_id": c["category_id"], "title": c["title"],
               "sources": len(by_cat.get(c["category_id"], []))}
              for c in registry.active(level=3)
              if len(by_cat.get(c["category_id"], [])) <= 1]
    overgrown = [{"category_id": cid, "count": len(notes)}
                 for cid, notes in by_cat.items() if len(notes) > 25]
    provisional = [{"category_id": n["category_id"], "title": n["title"],
                    "created_at": n.get("created_at")}
                   for n in registry.nodes.values() if n["status"] == "provisional"]

    proposals = []
    for d in near_dupes:
        proposals.append({"proposal_id": f"prop_merge_{d['a']}_{d['b']}"[:60],
                          "kind": "merge", "from_id": d["b"], "into_id": d["a"],
                          "reason": f"alias collision on '{d['name']}'"})
    for s in shells:
        proposals.append({"proposal_id": f"prop_demote_{s['category_id']}"[:60],
                          "kind": "demote_to_tag", "category_id": s["category_id"],
                          "reason": f"only {s['sources']} source(s) — shell topic"})
    for o in overgrown:
        proposals.append({"proposal_id": f"prop_split_{o['category_id']}"[:60],
                          "kind": "split_review", "category_id": o["category_id"],
                          "reason": f"{o['count']} notes — consider L2/L3 restructure "
                                    "(structural change: requires approval)"})
    report = {
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "near_duplicate_categories": near_dupes,
        "shell_topics": shells,
        "overgrown_categories": overgrown,
        "provisional_awaiting_review": provisional,
        "orphaned_notes": orphans[:50],
        "tag_stats": {"total": len(all_tags),
                      "singletons": sum(1 for v in all_tags.values() if v == 1)},
        "proposals": proposals,
        "note": "dry-run only; nothing was changed. Apply one proposal with "
                "`kf taxonomy apply <proposal-id> --approved-by liang`.",
    }
    # persist proposals so apply() can find them
    pdir = cfg.state_home / "proposals"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "latest.json").write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    return report


def apply_proposal(cfg: KFConfig, registry: TaxonomyRegistry, proposal_id: str,
                   approved_by: str) -> dict:
    if not approved_by:
        raise GovernanceError("taxonomy apply requires --approved-by")
    pfile = cfg.state_home / "proposals" / "latest.json"
    if not pfile.exists():
        raise GovernanceError("no gardener report found; run `kf taxonomy garden --dry-run` first")
    report = json.loads(pfile.read_text(encoding="utf-8"))
    prop = next((p for p in report["proposals"] if p["proposal_id"] == proposal_id), None)
    if not prop:
        raise GovernanceError(f"unknown proposal id: {proposal_id}")

    if prop["kind"] == "merge":
        registry.merge(prop["from_id"], prop["into_id"], approved_by)
    elif prop["kind"] == "demote_to_tag":
        node = registry.nodes.get(prop["category_id"])
        if not node:
            raise GovernanceError("category no longer exists")
        node["status"] = "deprecated"
    else:
        raise GovernanceError(f"proposal kind '{prop['kind']}' requires manual restructuring; "
                              "the gardener will not apply it automatically")
    cfg.taxonomy_registry_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.taxonomy_registry_path.write_text(registry.dumps(), encoding="utf-8")
    return {"applied": proposal_id, "kind": prop["kind"], "approved_by": approved_by}
