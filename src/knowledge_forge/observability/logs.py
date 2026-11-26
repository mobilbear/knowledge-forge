"""Structured JSONL operation logs under $KF_STATE_HOME/logs — never in the Vault.
Each line: ts, run_id, command, event, details. Secrets are redacted; captured
source content is truncated to a short preview so logs stay small and safe.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

_SECRET = re.compile(r"(key|token|secret|cookie|password|authorization)", re.I)


def _scrub(obj, depth=0):
    if depth > 6:
        return "…"
    if isinstance(obj, dict):
        return {k: ("***" if _SECRET.search(str(k)) else _scrub(v, depth + 1))
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub(x, depth + 1) for x in obj[:50]]
    if isinstance(obj, str) and len(obj) > 500:
        return obj[:500] + f"…(+{len(obj)-500} chars)"
    return obj


class OpLog:
    def __init__(self, cfg, run_id: str, command: str):
        self.path = cfg.log_root / f"kf-{dt.date.today().isoformat()}.jsonl"
        self.run_id = run_id
        self.command = command
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def event(self, event: str, **details):
        rec = {"ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
               "run_id": self.run_id, "command": self.command,
               "event": event, **_scrub(details)}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def rotate(cfg, keep_days: int = 30) -> int:
    """Delete log files older than keep_days. Returns count removed."""
    removed = 0
    if not cfg.log_root.exists():
        return 0
    cutoff = dt.date.today() - dt.timedelta(days=keep_days)
    for p in cfg.log_root.glob("kf-*.jsonl"):
        try:
            d = dt.date.fromisoformat(p.stem[3:])
        except ValueError:
            continue
        if d < cutoff:
            p.unlink()
            removed += 1
    return removed
