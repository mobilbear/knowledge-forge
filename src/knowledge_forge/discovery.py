"""Recursive vault note discovery and ID-aware link resolution.

Replaces v1.0's first-level ``glob('*.md')`` and basename-only link handling.
- Walks all note directories recursively with explicit exclusions.
- Resolves wikilinks by full vault-relative path first, then unique basename;
  ambiguous basenames are reported, never guessed.
- Builds an ID → path map so identity survives renames and moves.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

from . import frontmatter as fm

EXCLUDE_DIRS = {".obsidian", ".trash", ".git", ".kf-recovery", "node_modules", "_attachments", "70_media"}
NOTE_DIRS = ("10_sources", "20_concepts", "30_entities", "40_maps",
             "50_skills", "60_briefings", "80_queues", "90_meta")

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")


@dataclass
class Note:
    path: Path
    meta: dict
    body: str
    parse_error: Optional[str] = None

    @property
    def note_id(self) -> Optional[str]:
        return self.meta.get("id")

    @property
    def note_type(self) -> str:
        return self.meta.get("note_type") or self.meta.get("type") or "unknown"


def iter_notes(vault: Path, dirs: tuple = NOTE_DIRS, include_root: bool = True) -> Iterator[Note]:
    """Yield every markdown note recursively. Parse failures are yielded with
    parse_error set (callers decide whether to quarantine), never silently dropped."""
    roots = [vault / d for d in dirs if (vault / d).exists()]
    if include_root:
        for p in sorted(vault.glob("*.md")):
            yield _load(p)
    for root in roots:
        for p in sorted(root.rglob("*.md")):
            if any(part in EXCLUDE_DIRS for part in p.relative_to(vault).parts):
                continue
            yield _load(p)


def _load(p: Path) -> Note:
    try:
        meta, body = fm.read_note(p)
        return Note(p, meta, body)
    except fm.FrontmatterError as e:
        return Note(p, {}, p.read_text(encoding="utf-8", errors="replace"), parse_error=str(e))
    except Exception as e:  # unreadable file — surface, don't crash the pipeline
        return Note(p, {}, "", parse_error=f"{type(e).__name__}: {e}")


def rel(vault: Path, path: Path) -> str:
    return str(path.relative_to(vault).with_suffix("")).replace("\\", "/")


@dataclass
class VaultIndex:
    """In-memory view of the vault: notes, id map, basename map, link graph."""
    vault: Path
    notes: list[Note] = field(default_factory=list)
    by_id: dict = field(default_factory=dict)          # id -> Note
    by_rel: dict = field(default_factory=dict)         # rel path -> Note
    by_basename: dict = field(default_factory=dict)    # stem -> [rel...]
    duplicate_ids: list = field(default_factory=list)
    parse_errors: list = field(default_factory=list)

    @classmethod
    def build(cls, vault: Path) -> "VaultIndex":
        vi = cls(vault=vault)
        for n in iter_notes(vault):
            vi.notes.append(n)
            r = rel(vault, n.path)
            vi.by_rel[r] = n
            vi.by_basename.setdefault(n.path.stem, []).append(r)
            if n.parse_error:
                vi.parse_errors.append((r, n.parse_error))
                continue
            nid = n.note_id
            if nid:
                if nid in vi.by_id:
                    vi.duplicate_ids.append((nid, rel(vault, vi.by_id[nid].path), r))
                else:
                    vi.by_id[nid] = n
        return vi

    def resolve_link(self, target: str) -> tuple[Optional[str], Optional[str]]:
        """Resolve a wikilink target → (rel_path | None, problem | None).
        Full paths win; unique basenames resolve; ambiguous basenames report."""
        t = target.strip()
        if t.endswith(".md"):
            t = t[:-3]
        if t in self.by_rel:
            return t, None
        candidate = self.vault / t
        if candidate.suffix.lower() in {".base", ".canvas", ".pdf", ".vtt", ".srt", ".png", ".jpg",
                                       ".jpeg", ".webp", ".gif", ".wav", ".mp3", ".m4a",
                                       ".mp4", ".docx"} and candidate.is_file():
            if candidate.resolve().is_relative_to(self.vault.resolve()):
                return t, None
        if "/" in t:
            # explicit path that doesn't exist — do NOT guess by basename
            return None, f"unresolved link '{t}'"
        cands = self.by_basename.get(Path(t).name, [])
        if len(cands) == 1:
            return cands[0], None
        if len(cands) > 1:
            return None, f"ambiguous basename '{t}' → {sorted(cands)}"
        return None, f"unresolved link '{t}'"

    def outbound_links(self, note: Note) -> list[str]:
        return [m.group(1).strip() for m in WIKILINK_RE.finditer(note.body)]

    def link_report(self) -> dict:
        """Full-vault link audit: resolved, ambiguous, broken."""
        ambiguous, broken = [], []
        resolved = 0
        for n in self.notes:
            src = rel(self.vault, n.path)
            for t in self.outbound_links(n):
                tgt, problem = self.resolve_link(t)
                if tgt:
                    resolved += 1
                elif problem and problem.startswith("ambiguous"):
                    ambiguous.append((src, problem))
                else:
                    broken.append((src, t))
        return {"resolved": resolved, "ambiguous": ambiguous, "broken": broken}
