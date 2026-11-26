"""Governed taxonomy registry.

Machine-readable, versioned YAML at 90_meta/taxonomy/registry.yaml, validated
against schemas/taxonomy.schema.json. Independent of map filenames.

Governance (enforced by code, not prompts):
  L1  propose only — creation requires approved_by
  L2  provisional creation only when policy threshold met; reported
  L3  creatable when threshold + duplicate checks pass
  L4  sparingly; prefer tags/links
Merges preserve aliases and redirect old IDs (status=merged, merged_into set).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .. import frontmatter as fm
from .. import ids

SCHEMA_VERSION = "1.5.1"


class GovernanceError(RuntimeError):
    pass


@dataclass
class TaxonomyRegistry:
    path: Path
    nodes: dict = field(default_factory=dict)   # category_id -> node dict
    policy: dict = field(default_factory=dict)

    DEFAULT_POLICY = {
        "l3_min_sources": 3,
        "l3_min_occasions": 2,
        "l2_min_subclusters": 3,
        "candidate_pool_size": 6,
        "min_classification_confidence": 0.55,
    }

    # ---- persistence -------------------------------------------------------------
    @classmethod
    def load(cls, path: Path, policy: Optional[dict] = None) -> "TaxonomyRegistry":
        reg = cls(path=path, policy={**cls.DEFAULT_POLICY, **(policy or {})})
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            errors = fm.validate(data, "taxonomy")
            if errors:
                raise ValueError(f"taxonomy registry failed schema validation: {errors}")
            for n in data.get("nodes", []):
                reg.nodes[n["category_id"]] = n
        return reg

    def to_dict(self) -> dict:
        return {"schema_version": SCHEMA_VERSION,
                "nodes": [self.nodes[k] for k in sorted(self.nodes)]}

    def dumps(self) -> str:
        data = self.to_dict()
        errors = fm.validate(data, "taxonomy")
        if errors:
            raise ValueError(f"refusing to write invalid registry: {errors}")
        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)

    # ---- queries -----------------------------------------------------------------
    def get(self, cid: str, follow_merges: bool = True) -> Optional[dict]:
        n = self.nodes.get(cid)
        while follow_merges and n and n["status"] == "merged" and n.get("merged_into"):
            n = self.nodes.get(n["merged_into"])
        return n

    def children(self, cid: Optional[str]) -> list[dict]:
        return [n for n in self.nodes.values()
                if n.get("parent_id") == cid and n["status"] in ("active", "provisional")]

    def active(self, level: Optional[int] = None) -> list[dict]:
        return [n for n in self.nodes.values()
                if n["status"] in ("active", "provisional")
                and (level is None or n["level"] == level)]

    def path_of(self, cid: str) -> list[dict]:
        out, cur = [], self.get(cid)
        seen = set()
        while cur and cur["category_id"] not in seen:
            seen.add(cur["category_id"])
            out.append(cur)
            cur = self.nodes.get(cur.get("parent_id") or "")
        return list(reversed(out))

    # ---- governance-checked mutation ----------------------------------------------
    def add_node(self, title: str, level: int, parent_id: Optional[str],
                 created_by: str = "ohayo", approved_by: Optional[str] = None,
                 aliases: Optional[list] = None, description: str = "",
                 evidence_sources: int = 0, evidence_occasions: int = 0) -> dict:
        """The ONLY way a category comes into existence. Enforces level rules."""
        if level == 1 and not approved_by:
            raise GovernanceError(
                "L1 domains may only be proposed; creation requires explicit approval "
                "(approved_by). Use `kf taxonomy apply <proposal> --approved-by liang`."
            )
        if level in (2, 3) and not approved_by:
            need_src = self.policy["l3_min_sources"]
            need_occ = self.policy["l3_min_occasions"]
            if evidence_sources < need_src or evidence_occasions < need_occ:
                raise GovernanceError(
                    f"L{level} creation threshold not met "
                    f"({evidence_sources}/{need_src} sources, "
                    f"{evidence_occasions}/{need_occ} occasions). "
                    "Keep as tags / Emerging Topic."
                )
        if parent_id and parent_id not in self.nodes:
            raise GovernanceError(f"parent category does not exist: {parent_id}")
        if level > 1:
            parent = self.nodes.get(parent_id or "")
            if not parent or parent["level"] != level - 1:
                raise GovernanceError(
                    f"L{level} node must attach to an L{level-1} parent")
        cid = ids.category_id(title)
        if cid in self.nodes:
            raise GovernanceError(f"category already exists: {cid}")
        # near-duplicate check against titles + aliases
        dup = self.find_near_duplicate(title)
        if dup and not approved_by:
            raise GovernanceError(
                f"near-duplicate of existing category {dup['category_id']} "
                f"('{dup['title']}'); use it or merge instead")
        node = {
            "category_id": cid, "title": title, "parent_id": parent_id,
            "level": level,
            "status": "active" if approved_by else "provisional",
            "aliases": aliases or [], "description": description,
            "positive_examples": [], "negative_examples": [],
            "created_by": approved_by or created_by,
            "created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "review_status": None if approved_by else "provisional",
        }
        self.nodes[cid] = node
        return node

    def merge(self, from_id: str, into_id: str, approved_by: str) -> None:
        if not approved_by:
            raise GovernanceError("merges require explicit approval")
        src, dst = self.nodes.get(from_id), self.nodes.get(into_id)
        if not src or not dst:
            raise GovernanceError("merge endpoints must exist")
        dst.setdefault("aliases", [])
        for a in [src["title"]] + (src.get("aliases") or []):
            if a not in dst["aliases"] and a != dst["title"]:
                dst["aliases"].append(a)
        src["status"] = "merged"
        src["merged_into"] = into_id

    def find_near_duplicate(self, title: str) -> Optional[dict]:
        t = ids.normalize_tag(title)
        for n in self.nodes.values():
            if n["status"] not in ("active", "provisional"):
                continue
            names = [n["title"]] + (n.get("aliases") or [])
            for name in names:
                if ids.normalize_tag(name) == t:
                    return n
        return None

    # ---- candidate-first retrieval ---------------------------------------------------
    def candidates_for(self, text: str, tags: list[str], k: Optional[int] = None) -> list[dict]:
        """Deterministic candidate shortlist for the classifier model: token
        overlap over title+aliases+description+examples, tag hits weighted up."""
        k = k or self.policy["candidate_pool_size"]
        import re
        toks = set(re.findall(r"[a-z0-9]{3,}|[\u4e00-\u9fff]{2,}", text.lower())[:400])
        tagset = {ids.normalize_tag(t) for t in tags}
        scored = []
        for n in self.active():
            # L2+ are the normal filing targets; a bare L1 (no active children
            # yet) is allowed as an interim target so young domains still work.
            if n["level"] == 1 and self.children(n["category_id"]):
                continue
            hay = " ".join([n["title"], n.get("description", "")]
                           + (n.get("aliases") or []) + (n.get("positive_examples") or [])).lower()
            htoks = set(re.findall(r"[a-z0-9]{3,}|[\u4e00-\u9fff]{2,}", hay))
            score = len(toks & htoks)
            names = {ids.normalize_tag(x) for x in [n["title"]] + (n.get("aliases") or [])}
            score += 5 * len(tagset & names)
            if score > 0:
                scored.append((score, n))
        scored.sort(key=lambda x: (-x[0], x[1]["category_id"]))
        return [n for _, n in scored[:k]]


def default_registry_nodes() -> list[dict]:
    """Seed registry derived from Liang's v1.0 taxonomy.md starter domains."""
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")

    def n(cid, title, parent, level, desc, aliases=()):
        return {"category_id": cid, "title": title, "parent_id": parent, "level": level,
                "status": "active", "aliases": list(aliases), "description": desc,
                "positive_examples": [], "negative_examples": [],
                "created_by": "migration", "created_at": now, "review_status": None}

    return [
        n("cat_ai", "AI & LLM Engineering", None, 1,
          "AI systems, agents, governance and engineering", ["人工智能"]),
        n("cat_ai_llm_eng", "LLM Engineering", "cat_ai", 2,
          "Agents, context engineering, retrieval, prompting", ["llm-eng"]),
        n("cat_ai_governance", "AI Governance", "cat_ai", 2,
          "AI policy, safety, regulation, alignment governance", ["ai-governance"]),
        n("cat_quant_risk", "Quantitative Risk", None, 1,
          "Credit / market / liquidity / capital modeling", ["quant-risk", "风险计量"]),
        n("cat_political_economy", "Political Economy", None, 1,
          "政治经济学, macro, institutions, intelligence-capital", ["political-economy"]),
        n("cat_history", "History", None, 1,
          "Historical analysis and long-form", ["历史"]),
        n("cat_music", "Music", None, 1,
          "Songwriting and music production", ["音乐"]),
        n("cat_shushu", "术数", None, 1,
          "八字 / 风水 / 玄空 / 紫微 / 梅花 divination methods", ["shushu"]),
        n("cat_method", "Research Methods", None, 1,
          "Research methods, computational social science, ABM", ["method"]),
    ]
