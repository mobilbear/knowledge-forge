"""Local receipt queue for authenticated user forwards."""
from __future__ import annotations

from contextlib import closing
import re
import sqlite3
import time
from urllib.parse import urlsplit

from .. import ids
from ..urlnorm import canonicalize


def share_url(message):
    matches = [url.rstrip(".,!?;:)]}，。！？；）】") for url in
               re.findall(r"https://[^\s<>\x00-\x1f，。！？；）】]+", message)]
    if len(set(matches)) != 1:
        raise ValueError("a forwarded item must contain one HTTPS source link")
    url = matches[0]
    parsed = urlsplit(url)
    if (not parsed.hostname or parsed.username or parsed.password or len(url) > 4096
            or parsed.port not in (None, 443) or "." not in parsed.hostname):
        raise ValueError("invalid HTTPS share link")
    return url


def _connect(cfg):
    cfg.state_home.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(cfg.queue_db, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=PERSIST")
    con.execute("""CREATE TABLE IF NOT EXISTS library_jobs (
        receipt TEXT PRIMARY KEY, url TEXT NOT NULL, note TEXT, status TEXT NOT NULL,
        attempts INTEGER NOT NULL, available_at REAL NOT NULL, result_path TEXT,
        source_id TEXT, capture_status TEXT)""")
    return con


def lookup(cfg, receipt):
    with closing(_connect(cfg)) as con:
        row = con.execute(
            """SELECT receipt,status,attempts,result_path,source_id,capture_status
               FROM library_jobs WHERE receipt=?""", (receipt,)).fetchone()
        return dict(row) if row else None


def submit(cfg, message, note=None):
    url = share_url(message)
    receipt = "job_" + ids.source_id(canonicalize(url))[4:]
    with closing(_connect(cfg)) as con, con:
        con.execute(
            """INSERT INTO library_jobs
               (receipt,url,note,status,attempts,available_at) VALUES (?,?,?,'ready',0,?)
               ON CONFLICT(receipt) DO UPDATE SET
               url=excluded.url, note=COALESCE(NULLIF(excluded.note,''),library_jobs.note),
               status='ready', available_at=excluded.available_at, attempts=0
               WHERE library_jobs.status IN ('needs_input','partial')""",
            (receipt, url, (note or "")[:2000], time.time()),
        )
    return lookup(cfg, receipt)


def reconcile_archived(cfg, source_url, source_id, result_path):
    if not source_url or not cfg.queue_db.is_file():
        return
    receipt = "job_" + ids.source_id(canonicalize(source_url))[4:]
    with closing(_connect(cfg)) as con, con:
        con.execute(
            """UPDATE library_jobs SET status='archived',result_path=?,source_id=?,capture_status='complete'
               WHERE status IN ('partial','needs_input','ready','retry')
               AND (receipt=? OR source_id=?)""",
            (result_path, source_id, receipt, source_id),
        )


def _claim(cfg, receipt):
    now = time.time()
    with closing(_connect(cfg)) as con, con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            """SELECT * FROM library_jobs WHERE status IN ('ready','retry','running')
               AND available_at<=? AND (? IS NULL OR receipt=?)
               ORDER BY available_at,receipt LIMIT 1""", (now, receipt, receipt)).fetchone()
        if row is None:
            return None
        con.execute(
            """UPDATE library_jobs SET status='running',attempts=attempts+1,available_at=?
               WHERE receipt=?""", (now + 900, row["receipt"]),
        )
        return {**dict(row), "attempts": row["attempts"] + 1}


def process(cfg, capture, profile, receipt=None):
    job = _claim(cfg, receipt)
    if not job:
        return None
    status, result, temporary = "needs_input", {}, False
    try:
        outcome = capture(cfg, job["url"], note=job["note"], profile=profile)
        result = outcome.get("result") or {}
        if result.get("capture_status") == "complete":
            status = "archived"
        elif result.get("capture_status") == "usable_partial":
            status = "partial"
        error = " ".join(outcome.get("warnings", []) + outcome.get("errors", [])).lower()
        temporary = any(code in error for code in ("http 429", "http 503", "rate-limit", "timeout"))
    except (OSError, RuntimeError):
        temporary = True
    if temporary and job["attempts"] < 3 and status == "needs_input":
        status = "retry"
    with closing(_connect(cfg)) as con, con:
        con.execute(
            """UPDATE library_jobs SET status=?,available_at=?,result_path=?,source_id=?,
               capture_status=? WHERE receipt=? AND attempts=?""",
            (status, time.time() + 60 * 2 ** job["attempts"],
             result.get("note") or result.get("existing_note"), result.get("source_id"),
             result.get("capture_status"), job["receipt"], job["attempts"]),
        )
    return lookup(cfg, job["receipt"])
