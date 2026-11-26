"""Deterministic capture quality gate + state machine.

States (see capture_record.schema.json):
  received → capturing → normalized → quality_checked
  → usable | retry_required | metadata_only | failed
  → enriched → classified → staged → validated → committed → indexed → reported

Only `usable` (incl. accepted usable_partial) material may enter the formal
taxonomy, maps, synthesis and briefings. Everything else is quarantined into
80_queues/ with truthful reasons. Rules run first; an optional model second
opinion may *downgrade* but never upgrade a verdict.
"""
from __future__ import annotations

import re

VALID_TRANSITIONS = {
    "received": {"capturing"},
    "capturing": {"normalized", "failed"},
    "normalized": {"quality_checked"},
    "quality_checked": {"usable", "retry_required", "metadata_only", "failed"},
    "usable": {"enriched"},
    "retry_required": {"capturing", "staged"},   # queue note gets staged/committed too
    "metadata_only": {"staged"},
    "failed": {"staged"},
    "enriched": {"classified"},
    "classified": {"staged"},
    "staged": {"validated"},
    "validated": {"committed"},
    "committed": {"indexed"},
    "indexed": {"reported"},
}


class StateError(RuntimeError):
    pass


def transition(record: dict, new_state: str) -> dict:
    cur = record.get("state", "received")
    if new_state not in VALID_TRANSITIONS.get(cur, set()):
        raise StateError(f"illegal capture state transition {cur} → {new_state}")
    record["state"] = new_state
    return record


LOGIN_HINTS = ("请登录", "登录后查看", "sign in to continue", "log in to", "captcha",
               "安全验证", "验证码", "you must log in", "attention required",
               "登录后即可查看", "微信扫一扫", "环境异常", "verify you are human")
TRUNCATION_HINTS = ("阅读全文", "展开全文", "continue reading", "read more", "…展开",
                    "此内容因违规无法查看", "该内容已被发布者删除")

MIN_COMPLETE_CHARS = 600      # below → at best usable_partial
MIN_USABLE_CHARS = 200        # below → metadata_only
# Platforms whose posts carry content in images/video, not page text: a text-only
# scrape with no images/OCR is metadata only, not a complete capture.
IMAGE_FIRST_PLATFORMS = {"xiaohongshu", "weibo", "douyin", "bilibili"}
_WS = re.compile(r"\s+")

# "Readable prose" = body with markdown links/images and bare URLs removed. A page
# that is mostly navigation chrome and link lists (e.g. a scraped image-post whose
# real content is in un-fetched pictures) has a large raw length but tiny prose
# length — that must not grade as a complete capture.
_MD_LINK = re.compile(r"!?\[[^\]]*\]\([^)]*\)")
_URL = re.compile(r"(?:https?://|blob:)\S+")


def _prose_len(text: str) -> int:
    return len(_WS.sub(" ", _URL.sub(" ", _MD_LINK.sub(" ", text))).strip())


def assess(record: dict) -> dict:
    """Set capture_status, content_completeness, missing_elements, quality_reasons,
    needs_retry on the record and move state to the verdict. Deterministic."""
    text = _WS.sub(" ", (record.get("text") or "") + " " + (record.get("ocr_text") or "")).strip()
    reasons, missing = [], []
    low = text[:3000].lower()

    login_wall = any(h.lower() in low for h in LOGIN_HINTS)
    truncated = any(h.lower() in text.lower() for h in TRUNCATION_HINTS)
    n = len(text)
    prose_n = _prose_len(text)
    kind = record.get("content_kind")
    method = record.get("capture_method")
    abstract = kind == "abstract" or (
        record.get("platform") == "arxiv" and method == "api" and kind != "document")
    verified = bool(record.get("body_verified")) and prose_n >= 12
    if kind in ("document", "transcript", "image post") or method in ("manual", "ocr"):
        login_wall = False
        truncated = False
    # An image-/video-first post (Xiaohongshu pictures, Weibo, Douyin, Bilibili)
    # carries its content in media, not page text. If we captured neither images
    # nor OCR text, the readable body is just site chrome (nav + CN footer
    # boilerplate) — that is metadata only, never a complete source. `blob:` image
    # placeholders (fetcher rendered images it could not serialize) are the same
    # failure on any platform.
    no_media = not record.get("images") and not record.get("ocr_text")
    image_content_missing = no_media and (
        (record.get("platform") in IMAGE_FIRST_PLATFORMS
         and method not in ("transcript", "whisper")
         and not (kind in ("article", "document") and verified))
        or ("blob:" in text and prose_n < MIN_COMPLETE_CHARS))

    if kind == "video metadata":
        status_ = "metadata_only"
        completeness = 0.2
        reasons.append(record.get("partial_reason") or "video metadata was retrieved without captions")
        missing.append("spoken transcript")
    elif record.get("capture_method") == "none" or record.get("adapter_error"):
        status_ = "failed"
        reasons.append(record.get("adapter_error") or "no capture method succeeded")
        completeness = 0.0
    elif login_wall or (n < MIN_USABLE_CHARS and not (verified or (abstract and n))):
        status_ = "metadata_only"
        completeness = min(n / MIN_COMPLETE_CHARS, 0.3)
        if login_wall:
            reasons.append("login wall / anti-bot page detected")
        if n < MIN_USABLE_CHARS:
            reasons.append(f"body too short ({n} chars)")
            missing.append("article body")
    elif image_content_missing:
        # image-/video-first post whose media was not captured; readable text is chrome.
        status_ = "metadata_only"
        completeness = min(prose_n / MIN_COMPLETE_CHARS, 0.3)
        reasons.append("image/video-first post: no images or OCR text captured — "
                       "readable body is page chrome; needs image download + OCR "
                       "(MediaCrawler / logged-in browser / manual screenshots)")
        missing.append("post images / OCR text")
    elif abstract or truncated or record.get("partial_reason") or (
        not verified and (n < MIN_COMPLETE_CHARS or prose_n < MIN_USABLE_CHARS)
    ):
        status_ = "usable_partial"
        completeness = min(0.5 + prose_n / (4 * MIN_COMPLETE_CHARS), 0.85)
        if truncated:
            reasons.append("truncation marker present — body may be cut off")
            missing.append("full body after truncation point")
        if abstract:
            reasons.append("abstract only")
            missing.append("full paper")
        if record.get("partial_reason"):
            reasons.append(record["partial_reason"])
            missing.append("remaining transcript" if kind == "transcript" else "remaining original content")
        if not verified and n < MIN_COMPLETE_CHARS:
            reasons.append(f"short body ({n} chars)")
        if not verified and prose_n < MIN_USABLE_CHARS:
            reasons.append(f"little readable prose ({prose_n} chars); body is mostly links/chrome")
            missing.append("article body")
    else:
        status_ = "complete"
        completeness = 0.92 if not record.get("images") else 0.95
        if not record.get("title") or record["title"] == "untitled":
            status_ = "usable_partial"
            completeness = 0.7
            reasons.append("no title extracted")
            missing.append("title")

    record["capture_status"] = status_
    record["content_completeness"] = round(completeness, 2)
    record["quality_reasons"] = reasons
    record["missing_elements"] = missing
    record["needs_retry"] = status_ != "complete"
    transition(record, "quality_checked")
    verdict_state = {"complete": "usable", "usable_partial": "usable",
                     "metadata_only": "metadata_only", "failed": "failed"}[status_]
    transition(record, verdict_state)
    return record


def is_usable(record: dict) -> bool:
    return record.get("capture_status") in ("complete", "usable_partial")
