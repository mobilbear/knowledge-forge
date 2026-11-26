"""A rebuildable passage index over the Markdown vault."""
from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Optional

from ..config import KFConfig
from ..discovery import EXCLUDE_DIRS, NOTE_DIRS, VaultIndex, rel

INDEX_SCHEMA_VERSION = "library-2"
FIELDS = ("note_id, path, note_type, title, tags, category, platform, "
          "capture_status, updated, body, section")
HEADING = re.compile(r"(?m)^#{1,6} +([^\n]+)$")
CJK = re.compile(r"[\u3400-\u9fff]")
WORDS = re.compile(r"[\w]+", re.UNICODE)


def _connect(db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db, timeout=15)
    connection.execute("PRAGMA journal_mode=MEMORY")
    return connection


def _fingerprint(vault):
    files = list(vault.glob("*.md"))
    for folder in NOTE_DIRS:
        files.extend((vault / folder).rglob("*.md"))
    digest = hashlib.sha256()
    for path in sorted(files):
        relative = path.relative_to(vault)
        if any(part in EXCLUDE_DIRS for part in relative.parts):
            continue
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        digest.update(repr((relative.as_posix(), stat.st_mtime_ns, stat.st_ctime_ns,
                            stat.st_size)).encode())
    return digest.hexdigest()


def _passages(body):
    parts = HEADING.split(body)
    sections = [("", parts[0])]
    sections.extend(zip(parts[1::2], parts[2::2]))
    for heading, text in sections:
        text = text.strip()
        if text:
            for offset in range(0, len(text), 1100):
                yield heading.strip(), text[max(0, offset - 120):offset + 1100]


def _make_table(con, table, trigram=False):
    try:
        options = ", tokenize='trigram'" if trigram else ""
        con.execute(f"CREATE VIRTUAL TABLE {table} USING fts5({FIELDS}{options})")
        return True
    except sqlite3.OperationalError:
        if trigram:
            return False
        con.execute(f"CREATE TABLE {table} ({FIELDS})")
        return False


def rebuild(cfg: KFConfig, vi: Optional[VaultIndex] = None) -> dict:
    fingerprint = _fingerprint(cfg.vault)
    vi = vi or VaultIndex.build(cfg.vault)
    with closing(_connect(cfg.index_db)) as con, con:
        con.execute("BEGIN IMMEDIATE")
        for table in ("docs", "docs_cjk", "meta"):
            con.execute(f"DROP TABLE IF EXISTS {table}")
        con.execute("CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT)")
        fts = _make_table(con, "docs")
        trigram = _make_table(con, "docs_cjk", trigram=True)
        count = 0
        for note in vi.notes:
            if note.parse_error:
                continue
            meta = note.meta
            base = (meta.get("id") or "", rel(cfg.vault, note.path), note.note_type,
                    meta.get("title") or note.path.stem, " ".join(meta.get("tags") or []),
                    meta.get("topic") or meta.get("primary_category_id") or "",
                    meta.get("source_platform") or meta.get("platform") or "",
                    meta.get("capture_status") or "", str(meta.get("updated") or ""))
            passages = list(_passages(note.body)) or [("", "")]
            rows = [(*base, text, heading) for heading, text in passages]
            con.executemany("INSERT INTO docs VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
            if trigram:
                con.executemany("INSERT INTO docs_cjk VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                (row for row in rows if CJK.search(" ".join(row[3:]))))
            count += 1
        con.executemany("INSERT INTO meta VALUES (?,?)", {
            "schema_version": INDEX_SCHEMA_VERSION, "built_at": str(time.time()),
            "note_count": str(count), "fingerprint": fingerprint,
            "fts": str(int(fts)), "trigram": str(int(trigram)),
        }.items())
    return {"count": count, "fts5": fts, "schema_version": INDEX_SCHEMA_VERSION}


def refresh_after_commit(cfg: KFConfig) -> dict:
    return rebuild(cfg)


def _fts_query(terms):
    return " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)


def _search_rows(con, table, match, filters, cap):
    weighted = (f"bm25({table}, 0,0,0,4,2,2,1,0,0,1,2)"
                if match else "0")
    excerpt = f"snippet({table}, 9, '«', '»', ' … ', 22)" if match else "substr(body,1,240)"
    where = f"{table} MATCH ? AND {filters}" if match else filters
    args = [match] if match else []
    return con.execute(
        f"""WITH matches AS MATERIALIZED (
              SELECT note_id,path,title,note_type,capture_status,section,{excerpt} AS excerpt,
                     updated,{weighted} AS score,
                     CASE WHEN note_type='source' THEN 0 WHEN note_type='concept' THEN 1 ELSE 2 END AS kind
              FROM {table} WHERE {where}
            ), ranked AS (
              SELECT *,ROW_NUMBER() OVER (PARTITION BY path ORDER BY
                     CASE WHEN section IN ('Source text','OCR (from images)')
                                OR section LIKE 'Page %' THEN 0 ELSE 1 END,score,updated DESC) AS best
              FROM matches
            )
            SELECT note_id,path,title,note_type,capture_status,section,excerpt FROM ranked
            WHERE best=1 ORDER BY kind,score,updated DESC,path LIMIT ?""",
        (*args, cap),
    ).fetchall()


def query(cfg: KFConfig, q: str, n: int = 10, include_quarantined: bool = False) -> list[dict]:
    q = q.strip()[:400]
    if not q or n <= 0:
        return []
    state = status(cfg)
    if state["stale"]:
        rebuild(cfg)
        state = status(cfg)
    n = min(n, 100)
    terms = WORDS.findall(q)
    filters = "1" if include_quarantined else (
        "note_type NOT IN ('quarantined_capture','pending_classification','resolved_capture') "
        "AND path NOT LIKE '80_queues/%' AND capture_status NOT IN ('metadata_only','failed')")
    cap = max(100, n * 12)
    with closing(_connect(cfg.index_db)) as con:
        rows = []
        if terms and not CJK.search(q) and state.get("fts"):
            rows = _search_rows(con, "docs", _fts_query(terms), filters, cap)
        if terms and CJK.search(q) and state.get("trigram") and min(map(len, terms)) >= 3:
            rows = _search_rows(con, "docs_cjk", _fts_query(terms), filters, cap)
        if not rows:
            escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            match = " OR ".join(f"{field} LIKE ? ESCAPE '\\'" for field in (
                "note_id", "title", "tags", "body", "path", "category", "section"))
            rows = con.execute(
                f"""WITH matches AS (
                      SELECT note_id,path,title,note_type,capture_status,section,
                             CASE WHEN instr(lower(body),lower(?))>0
                               THEN substr(body,max(1,instr(lower(body),lower(?))-65),210)
                               ELSE substr(body,1,210) END AS excerpt, updated,
                             CASE WHEN title LIKE ? ESCAPE '\\' THEN 0 ELSE 1 END AS title_rank,
                             CASE WHEN note_type='source' THEN 0 WHEN note_type='concept' THEN 1 ELSE 2 END AS kind,
                             CASE WHEN instr(lower(body),lower(?))>0 THEN 0 ELSE 1 END AS body_rank
                      FROM docs WHERE ({match}) AND {filters}
                    ), ranked AS (
                      SELECT *,ROW_NUMBER() OVER (PARTITION BY path ORDER BY body_rank,
                             CASE WHEN section IN ('Source text','OCR (from images)')
                                        OR section LIKE 'Page %' THEN 0 ELSE 1 END) AS best FROM matches
                    )
                    SELECT note_id,path,title,note_type,capture_status,section,excerpt FROM ranked
                    WHERE best=1 ORDER BY title_rank,kind,body_rank,updated DESC,path LIMIT ?""",
                (q, q, f"%{escaped}%", q, *([f"%{escaped}%"] * 7), cap),
            ).fetchall()
    results, seen = [], set()
    for note_id, path, title, kind, capture, section, snippet in rows:
        if path in seen:
            continue
        seen.add(path)
        results.append({"note_id": note_id, "path": path, "title": title, "note_type": kind,
                        "capture_status": capture, "section": section,
                        "evidence_link": path + ("#" + section if section else ""),
                        "snippet": " ".join((snippet or "").split())})
        if len(results) == n:
            break
    return results


def status(cfg: KFConfig) -> dict:
    if not cfg.index_db.exists():
        return {"exists": False, "stale": True}
    with closing(_connect(cfg.index_db)) as con:
        try:
            meta = dict(con.execute("SELECT k, v FROM meta"))
        except sqlite3.OperationalError:
            meta = {}
    return {
        "exists": True, "schema_version": meta.get("schema_version"),
        "note_count": int(meta.get("note_count", 0)), "built_at": float(meta.get("built_at", 0)),
        "fts": meta.get("fts") == "1", "trigram": meta.get("trigram") == "1",
        "stale": (meta.get("schema_version") != INDEX_SCHEMA_VERSION
                  or meta.get("fingerprint") != _fingerprint(cfg.vault)),
    }


def contains_note(cfg: KFConfig, note_id: str) -> bool:
    if not cfg.index_db.exists():
        return False
    with closing(_connect(cfg.index_db)) as con:
        return bool(con.execute("SELECT 1 FROM docs WHERE note_id=? LIMIT 1", (note_id,)).fetchone())
