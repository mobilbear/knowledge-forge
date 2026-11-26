"""Stable machine identity for notes, categories, tags, runs and transactions.

IDs are independent of filename and folder: renaming or moving a note never
changes its ID. Source IDs are *deterministic* over source identity
(canonical URL, or content hash for local material) so the same share always
maps to the same ID — the foundation of idempotent capture.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import unicodedata


def _h(text: str, n: int = 16) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


def source_id(canonical_url: str | None, content_hash: str | None = None) -> str:
    """Deterministic: same canonical URL (or same content) → same ID."""
    basis = canonical_url or content_hash
    if not basis:
        raise ValueError("source_id requires a canonical_url or content_hash")
    return "src_" + _h(basis)


def category_id(slug: str) -> str:
    ascii_slug = re.sub(r"[^a-z0-9_]+", "_", slug.strip().lower()).strip("_")
    return "cat_" + (ascii_slug or _h(normalize_tag(slug), 8))


def tag_id(name: str) -> str:
    norm = normalize_tag(name)
    return "tag_" + (re.sub(r"[^a-z0-9\u4e00-\u9fff_]+", "_", norm).strip("_") or _h(norm, 8))


def run_id() -> str:
    return "run_" + secrets.token_hex(8)


def txn_id() -> str:
    return "txn_" + secrets.token_hex(8)


def note_id(kind: str) -> str:
    """Random ID for notes without stable external identity (concepts, entities...)."""
    prefix = {"concept": "con", "entity": "ent", "map": "map", "briefing": "brf"}.get(kind, "nte")
    return f"{prefix}_{secrets.token_hex(8)}"


def normalize_tag(name: str) -> str:
    """Lowercase, NFKC-normalised, hyphen-joined tag form. CJK preserved."""
    s = unicodedata.normalize("NFKC", name).strip().lower()
    s = re.sub(r"[\s_/]+", "-", s)
    s = re.sub(r"[^\w\-\u4e00-\u9fff]", "", s)
    return s.strip("-")


def content_sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def slugify(text: str, maxlen: int = 60) -> str:
    """ASCII+CJK-safe filename slug (ported from v1.0, behaviour preserved)."""
    text = text.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = re.sub(r"[\s_]+", "-", text).strip("-")
    return text[:maxlen] or "untitled"
