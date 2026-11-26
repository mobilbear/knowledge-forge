"""Modular source adapters. Ports and hardens the v1.0 cascade.

Each adapter returns a raw dict; ``normalize.build_record`` turns it into a
schema-valid capture record, and ``quality.gate.assess`` grades it. Adapter
failure never breaks the pipeline: it degrades to the next method and finally
to a truthful failed/metadata_only record.

CAPABILITY (the single source of truth — docs/CAPABILITY_MATRIX.md is generated
from this dict; nothing may be documented above its status here):
"""
from __future__ import annotations

import json
import ipaddress
import socket
import os
import re
import subprocess
import urllib.error
import urllib.request
import urllib.parse
from pathlib import Path
from shutil import which
from typing import Callable, Optional

from ..urlnorm import platform_of
from .documents import MAX_PDF_BYTES, extract_pdf

CAPABILITY = {
    # status: supported | partial | experimental | unsupported
    "web":         {"status": "supported",    "methods": ["direct", "jina", "browser"],
                    "notes": "readability extraction (trafilatura if installed) with HTML-strip fallback"},
    "substack":    {"status": "supported",    "methods": ["direct", "jina"],
                    "notes": "treated as ordinary web; direct fetch usually complete"},
    "github":      {"status": "supported",    "methods": ["api"],
                    "notes": "README/raw/markdown via api.github.com; tested live"},
    "arxiv":       {"status": "supported",    "methods": ["api"],
                    "notes": "export.arxiv.org Atom API: title, author list, submission date, "
                             "abstract and cs.* categories; falls back to generic fetch"},
    "hackernews":  {"status": "supported",    "methods": ["api"], "notes": "Algolia API"},
    "reddit":      {"status": "partial",      "methods": ["api"],
                    "notes": ".json endpoint; may be blocked without credentials"},
    "youtube":     {"status": "partial",      "methods": ["transcript"],
                    "notes": "captions via yt-dlp when installed; NO Whisper fallback is "
                             "implemented — absent captions queue for manual transcript"},
    "wechat":      {"status": "partial",      "methods": ["direct", "jina", "browser"],
                    "notes": "dedicated mp.weixin.qq.com parser (js_content, meta, truncation "
                             "markers); network access to WeChat not verifiable from the build "
                             "environment — verify on Lubuntu"},
    "zhihu":       {"status": "partial",      "methods": ["jina", "browser"],
                    "notes": "login-wall detection implemented; logged-in cookies via "
                             "KF_COOKIES_ZHIHU improve results"},
    "toutiao":     {"status": "partial",      "methods": ["jina", "browser"],
                    "notes": "generic extraction + login-wall detection"},
    "xiaohongshu": {"status": "experimental", "methods": ["ocr", "browser"],
                    "notes": "image-post OCR merge implemented (page order preserved, "
                             "low-confidence flagged); requires a configured OCR backend "
                             "(paddleocr or vision API); live XHS fetch typically needs "
                             "MediaCrawler or manual screenshots"},
    "x":           {"status": "experimental", "methods": ["jina", "browser"],
                    "notes": "thread order preserved when Jina returns it; no private auth"},
    "linkedin":    {"status": "experimental", "methods": ["jina", "browser"],
                    "notes": "login-wall/preview detection; full text usually needs an "
                             "authorized local browser session"},
    "weibo":       {"status": "experimental", "methods": ["jina", "browser"], "notes": ""},
    "bilibili":    {"status": "experimental", "methods": ["jina", "browser"], "notes": ""},
    "douyin":      {"status": "unsupported",  "methods": [],
                    "notes": "video platform; queue for manual transcript"},
    "mediacrawler":{"status": "unsupported",  "methods": [],
                    "notes": "v1.0 stub removed from the cascade; can be wired as an external "
                             "adapter later — was never functional and is not pretended to be"},
    "whisper":     {"status": "unsupported",  "methods": [],
                    "notes": "not implemented; do not claim audio transcription"},
    "file":        {"status": "supported",    "methods": ["manual"],
                    "notes": "txt/md direct; other formats stored as attachment + queued"},
    "image":       {"status": "partial",      "methods": ["ocr"],
                    "notes": "OCR via paddleocr if installed; otherwise queued with the images "
                             "preserved as evidence"},
}

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")


MOBILE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Mobile/15E148 Safari/604.1"),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def _open_public(url, headers=None, timeout=20, allowed_hosts=(), method="GET"):
    """Check the destination and each redirect before opening a public fetch."""
    def check(target):
        parsed = urllib.parse.urlsplit(target)
        host = parsed.hostname
        if (parsed.scheme != "https" or not host or parsed.username or parsed.password
                or parsed.port not in (None, 443)):
            raise ValueError("expected a public HTTPS source")
        if allowed_hosts and not any(host == h or host.endswith("." + h) for h in allowed_hosts):
            raise ValueError("redirect outside the capture source")
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise ValueError("source resolves to a private or reserved network")

    class CheckedRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            check(newurl)
            return super().redirect_request(req, fp, code, msg, hdrs, newurl)

    check(url)
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})}, method=method)
    return urllib.request.build_opener(CheckedRedirect()).open(req, timeout=timeout)


def http_get(url: str, headers: Optional[dict] = None, timeout: int = 20) -> str:
    limit = 6 * 1024 * 1024
    with _open_public(url, headers=headers, timeout=timeout) as response:
        body = response.read(limit + 1)
        if len(body) > limit:
            raise ValueError("page exceeds capture size limit")
        return body.decode(response.headers.get_content_charset() or "utf-8", "replace")


def strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript|nav|header|footer|aside)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def title_of(html: str) -> Optional[str]:
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    return strip_html(m.group(1)) if m else None


def guess_lang(text: str) -> str:
    if not text:
        return "en"
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    return "zh" if cjk / max(len(text), 1) > 0.12 else "en"


def extract_readable(html: str) -> str:
    try:
        import trafilatura
        return trafilatura.extract(html, include_comments=False, favor_recall=True,
                                   output_format="markdown", include_links=True) or strip_html(html)
    except Exception:
        return strip_html(html)


# ---- adapters --------------------------------------------------------------------------
def _matches_article_body(html: str, extracted: str) -> bool:
    """Use a declared article body as evidence for a complete short article."""
    from html import unescape
    text = " ".join(extracted.split())
    for data in re.findall(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                           html, flags=re.I | re.S):
        try:
            parsed = json.loads(unescape(data))
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            body = parsed.get("articleBody")
            if isinstance(body, str) and len(body.strip()) >= 12 and " ".join(body.split()) in text:
                return True
    return False


def cap_generic(url: str, platform: str, secrets: dict) -> dict:
    html = http_get(url)
    text = extract_readable(html)
    return {"platform": platform, "title": title_of(html), "text": text,
            "capture_method": "direct", "content_kind": "abstract" if platform == "arxiv" else "article",
            "body_verified": _matches_article_body(html, text)}


def _rednote_image_set(html, url):
    item = re.fullmatch(r"/(?:explore|discovery/item)/([a-zA-Z0-9]+)/?",
                        urllib.parse.urlsplit(url).path)
    if not item:
        return None
    state = re.search(r"window\.__INITIAL_STATE__\s*=\s*", html)
    if not state:
        return None
    try:
        payload = re.split(r"</script\s*>", html[state.end():], maxsplit=1, flags=re.I)[0]
        # JavaScript hydration includes bare undefined. Leave string values untouched.
        token = re.compile(r'"(?:\\.|[^"\\])*"|\b(?:undefined|NaN)\b')
        payload = token.sub(lambda m: m[0] if m[0].startswith('"') else "null", payload)
        parsed, _ = json.JSONDecoder().raw_decode(payload)
        desktop = (parsed.get("note") or {}).get("noteDetailMap") or {}
        mobile = ((parsed.get("noteData") or {}).get("data") or {}).get("noteData")
        note = (desktop.get(item[1]) or {}).get("note") or mobile
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    if not isinstance(note, dict) or note.get("type") == "video":
        return None
    if (note.get("noteId") and note.get("noteId") != item[1]) or (note is mobile and not note.get("noteId")):
        return None
    images = note.get("imageList") or []
    if not isinstance(images, list) or not 1 <= len(images) <= 12:
        return None
    return note, images


def _image_extension(data):
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def cap_rednote(url: str, platform: str, secrets: dict, html=None) -> dict:
    html = http_get(url, headers=MOBILE_HEADERS) if html is None else html
    found = _rednote_image_set(html, url)
    if not found:
        text = extract_readable(html)
        return {"platform": platform, "title": title_of(html), "text": text,
                "capture_method": "direct", "content_kind": "article"}
    note, images = found
    files, pages, remaining = [], [], 24 * 1024 * 1024
    for i, image in enumerate(images, 1):
        if not isinstance(image, dict) or not remaining:
            continue
        image_url = image.get("urlDefault") or image.get("url")
        if not isinstance(image_url, str):
            continue
        try:
            parsed_image = urllib.parse.urlsplit(image_url)
            if parsed_image.scheme == "http":
                image_url = parsed_image._replace(scheme="https").geturl()
            data = _fetch_bytes(image_url, min(remaining, 5 * 1024 * 1024),
                                allowed_hosts=("xhscdn.com", "xiaohongshu.com", "rednote.com"))
        except (OSError, ValueError):
            continue
        ext = _image_extension(data)
        if ext is None:
            continue
        files.append((f"image-{i:02d}.{ext}", data))
        remaining -= len(data)
        if which("tesseract"):
            try:
                ocr = subprocess.run(["tesseract", "stdin", "stdout", "-l", "chi_sim+eng"],
                                     input=data, capture_output=True, timeout=12, check=True)
                text = ocr.stdout.decode("utf-8", errors="replace").strip()
                if text:
                    pages.append(f"### Page {i}\n\n{text}")
            except (OSError, subprocess.SubprocessError):
                pass
    return {"platform": platform, "title": note.get("title") or title_of(html),
            "text": note.get("desc") or "", "ocr_text": "\n\n".join(pages),
            "images": [name for name, _ in files], "artifacts": files,
            "capture_method": "ocr" if pages else "direct", "content_kind": "image post",
            "body_verified": len(files) == len(images),
            "partial_reason": "some post images could not be archived" if len(files) != len(images) else None}


def cap_jina(url: str, platform: str, secrets: dict) -> dict:
    if secrets.get("KF_PUBLIC_READER") != "jina":
        raise RuntimeError("public reader fallback disabled")
    headers = {"X-Return-Format": "markdown"}
    if secrets.get("KF_JINA_KEY"):
        headers["Authorization"] = f"Bearer {secrets['KF_JINA_KEY']}"
    md = http_get("https://r.jina.ai/" + url, headers=headers, timeout=60)
    title = None
    m = re.search(r"Title:\s*(.+)", md[:400])
    if m:
        title = m.group(1).strip()
    return {"platform": platform, "title": title, "text": md, "capture_method": "jina",
            "content_kind": "abstract" if platform == "arxiv" else "article"}


def cap_browser(url: str, platform: str, secrets: dict) -> dict:
    if not secrets.get("KF_BROWSER_PROFILE"):
        raise RuntimeError("local browser fallback is not configured; user can provide a clip")
    from playwright.sync_api import sync_playwright  # raises if not installed → cascade continues
    profile = secrets.get("KF_BROWSER_PROFILE")
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(profile, headless=True)
        try:
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=18000)
            try:
                page.locator("article, main, [role=main]").first.wait_for(timeout=3000)
            except Exception:
                pass
            html = page.content()
            final_url = page.url
        finally:
            ctx.close()
    if platform == "xiaohongshu" and platform_of(final_url) == platform:
        return cap_rednote(final_url, platform, secrets, html=html)
    return {"platform": platform, "title": title_of(html),
            "text": extract_readable(html), "capture_method": "browser",
            "content_kind": "abstract" if platform == "arxiv" else "article"}


def cap_public_firecrawl(url: str, platform: str, secrets: dict) -> dict:
    """Optional single-page fetch for recognized, token-free public articles."""
    if secrets.get("KF_PUBLIC_READER") != "firecrawl":
        raise RuntimeError("public Firecrawl fallback is disabled")
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    allowed = ((host == "zhuanlan.zhihu.com" and re.fullmatch(r"/p/\d+/?", parsed.path))
               or (host in ("toutiao.com", "www.toutiao.com", "m.toutiao.com")
                   and re.fullmatch(r"/article/\d+/?", parsed.path)))
    if not allowed or parsed.scheme != "https" or parsed.username or parsed.password:
        raise ValueError("not a recognized public Zhihu or Toutiao article")
    public_url = urllib.parse.urlunsplit(("https", host, parsed.path, "", ""))
    cli = which("firecrawl")
    if not cli:
        raise RuntimeError("Firecrawl CLI is unavailable")
    proc = subprocess.run([cli, "scrape", public_url, "--only-main-content", "--format",
                           "markdown", "--json"],
                          env={**os.environ, "FIRECRAWL_NO_ENDPOINT_FEEDBACK": "1"},
                          capture_output=True, text=True, timeout=45)
    if proc.returncode:
        raise RuntimeError("Firecrawl did not return a public article")
    payload = json.loads(proc.stdout)
    data = payload.get("data") or payload
    meta = data.get("metadata") or {}
    if not 200 <= (meta.get("statusCode") or 200) < 300:
        raise RuntimeError("Firecrawl received a non-success response from the publisher")
    date = meta.get("datePublished")
    return {"platform": platform, "title": meta.get("title") or meta.get("ogTitle"),
            "text": data.get("markdown") or "", "capture_method": "firecrawl",
            "content_kind": "article", "published_at": date[:10] if isinstance(date, str) else None}


# ---- WeChat Official Accounts (dedicated parser, fixture-tested) ------------------------
_WECHAT_META = {
    "title": re.compile(r'property="og:title"\s+content="([^"]*)"'),
    "author": re.compile(r'"nickname"[^>]*>([^<]+)<|var nickname\s*=\s*"([^"]*)"'),
    "published": re.compile(r'var\s+(?:ct|createTime)\s*=\s*"?(\d{10})"?'),
}


def parse_wechat_html(html: str) -> dict:
    """Parse an mp.weixin.qq.com article page. Verifies real body presence."""
    out: dict = {"platform": "wechat"}
    m = _WECHAT_META["title"].search(html)
    if m:
        out["title"] = m.group(1).strip()
    m = _WECHAT_META["author"].search(html)
    if m:
        out["authors"] = [(m.group(1) or m.group(2) or "").strip()]
    m = _WECHAT_META["published"].search(html)
    if m:
        import datetime as dt
        out["published_at"] = dt.datetime.fromtimestamp(int(m.group(1))).isoformat()
    from lxml import etree
    from lxml import html as lxml_html

    out["text"] = ""
    try:
        document = lxml_html.fromstring(html)
    except (ValueError, etree.ParserError):
        document = None
    bodies = document.xpath('//*[@id="js_content"]') if document is not None else []
    if bodies:
        body = bodies[0]
        images = []
        unavailable_images = 0
        for img in body.xpath(".//img"):
            url = img.get("data-src") or img.get("src") or ""
            if url.startswith("//"):
                url = "https:" + url
            address = urllib.parse.urlsplit(url)
            if address.hostname == "mmbiz.qpic.cn" and address.scheme in ("http", "https"):
                url = address._replace(scheme="https").geturl()
                images.append(url)
            elif address.scheme in ("http", "https") and address.hostname:
                unavailable_images += 1
            img.set("alt", img.get("alt") or "Article image")
            # Original images are stored separately when available.
            img.attrib.pop("src", None)
        for unsafe in body.xpath(".//script | .//style | .//noscript"):
            unsafe.drop_tree()
        for link in body.xpath(".//a[@href]"):
            if urllib.parse.urlsplit(link.get("href")).scheme not in ("https", "http"):
                link.attrib.pop("href", None)
        try:
            from markdownify import markdownify
        except ImportError:
            out["text"] = body.text_content().strip()
        else:
            out["text"] = markdownify(lxml_html.tostring(body, encoding="unicode"),
                                      heading_style="ATX").strip()
        out["body_verified"] = bool(out["text"])
        out["image_urls"] = list(dict.fromkeys(images))
        out["unavailable_images"] = unavailable_images
    elif any(word in html for word in ("环境异常", "完成验证", "访问过于频繁", "安全验证")):
        out["adapter_error"] = "WeChat public page requires verification or an authorized clip"
    if "此内容因违规无法查看" in html or "该内容已被发布者删除" in html:
        out["adapter_error"] = "article removed or blocked by platform"
    return out


def cap_wechat(url: str, platform: str, secrets: dict) -> dict:
    html = http_get(url, headers=MOBILE_HEADERS)
    out = parse_wechat_html(html)
    out["capture_method"] = "direct"
    out["content_kind"] = "article"
    artifacts, cap = [], 18 * 1024 * 1024
    for i, image in enumerate(out.get("image_urls", [])[:16], 1):
        if not cap:
            break
        try:
            data = _fetch_bytes(image, min(cap, 4 * 1024 * 1024), allowed_hosts=("mmbiz.qpic.cn",))
        except (ValueError, OSError):
            continue
        ext = _image_extension(data)
        if ext:
            artifacts.append((f"image-{i:02d}.{ext}", data))
            cap -= len(data)
    out["artifacts"] = artifacts
    if out.get("unavailable_images") or len(artifacts) < len(out.get("image_urls", [])):
        out["partial_reason"] = "article text saved; some original images were unavailable or exceed the archive limit"
    return out


# ---- structured API adapters (ported from v1.0) -----------------------------------------
def cap_arxiv(url: str, platform: str, secrets: dict) -> dict:
    """arXiv Atom API → clean title, full author list, submission date, abstract
    and subject categories. Raises on any failure so the cascade falls through to
    a generic fetch of the abs page rather than emitting a half-parsed record."""
    import xml.etree.ElementTree as ET
    m = re.search(r"arxiv\.org/(?:abs|pdf|html)/([0-9]{4}\.[0-9]{4,5})", url)
    if not m:
        raise ValueError("no arXiv id in URL")
    arxiv_id = m.group(1)
    ns = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    xml = http_get(f"https://export.arxiv.org/api/query?id_list={arxiv_id}", timeout=45)
    entry = ET.fromstring(xml).find("a:entry", ns)
    if entry is None or entry.find("a:title", ns) is None:
        raise ValueError("arXiv API returned no entry")
    title = " ".join(entry.findtext("a:title", default="", namespaces=ns).split())
    summary = " ".join(entry.findtext("a:summary", default="", namespaces=ns).split())
    published = (entry.findtext("a:published", default="", namespaces=ns) or "")[:10] or None
    authors = [n.strip() for a in entry.findall("a:author", ns)
               if (n := a.findtext("a:name", default="", namespaces=ns)).strip()]
    cats = [c.get("term") for c in entry.findall("a:category", ns) if c.get("term")]
    primary = entry.find("arxiv:primary_category", ns)
    if primary is not None and primary.get("term"):
        cats = [primary.get("term")] + [c for c in cats if c != primary.get("term")]
    text = f"arXiv:{arxiv_id}" + (f" [{', '.join(cats)}]" if cats else "") + "\n\n" + summary
    return {"platform": "arxiv", "title": title, "authors": authors,
            "published_at": published, "text": text, "lang": guess_lang(summary),
            "capture_method": "api", "content_kind": "abstract"}


def cap_paper_pdf(url: str, platform: str, secrets: dict) -> dict:
    match = re.search(r"/(?:abs|pdf|html)/([\w.-]+)", urllib.parse.urlsplit(url).path)
    if not match:
        raise ValueError("no arXiv identifier in URL")
    identifier = match[1].removesuffix(".pdf")
    return extract_pdf(_fetch_bytes(f"https://arxiv.org/pdf/{identifier}", MAX_PDF_BYTES, allowed_hosts=("arxiv.org",)),
                       f"arXiv {identifier}", "arxiv")


def cap_public_pdf(url: str, platform: str, secrets: dict) -> dict:
    title = urllib.parse.unquote(Path(urllib.parse.urlsplit(url).path).stem)
    return extract_pdf(_fetch_bytes(url, MAX_PDF_BYTES), title or "PDF", platform)


def _fetch_bytes(url: str, limit: int, allowed_hosts: tuple[str, ...] = ()) -> bytes:
    with _open_public(url, allowed_hosts=allowed_hosts) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("response exceeds capture size limit")
    return data


def cap_github(url: str, platform: str, secrets: dict) -> dict:
    if "raw.githubusercontent.com" in url or url.endswith((".md", ".txt")):
        text = http_get(url)
        return {"platform": "github", "title": url.rsplit("/", 1)[-1], "text": text,
                "capture_method": "api"}
    m = re.search(r"github\.com/([^/]+)/([^/#?]+)", url)
    if m:
        owner, repo = m.group(1), m.group(2)
        text = http_get(f"https://api.github.com/repos/{owner}/{repo}/readme",
                        headers={"Accept": "application/vnd.github.raw"})
        return {"platform": "github", "title": f"{owner}/{repo}", "text": text,
                "capture_method": "api"}
    html = http_get(url)
    return {"platform": "github", "title": title_of(html), "text": strip_html(html),
            "capture_method": "direct"}


def cap_hn(url: str, platform: str, secrets: dict) -> dict:
    m = re.search(r"id=(\d+)", url)
    if not m:
        raise ValueError("no HN item id in URL")
    item = json.loads(http_get(f"https://hn.algolia.com/api/v1/items/{m.group(1)}"))
    parts = [item.get("title") or "", item.get("text") or "", item.get("url") or ""]
    parts += [f"- {k.get('author','?')}: {strip_html(k['text'])}"
              for k in item.get("children", [])[:20] if k.get("text")]
    return {"platform": "hackernews", "title": item.get("title"),
            "authors": [item["author"]] if item.get("author") else [],
            "text": "\n\n".join(p for p in parts if p), "capture_method": "api"}


def cap_reddit(url: str, platform: str, secrets: dict) -> dict:
    j = json.loads(http_get(url.rstrip("/") + ".json"))
    post = j[0]["data"]["children"][0]["data"]
    body = post.get("selftext", "") or post.get("url", "")
    comments = [f"- u/{c['data'].get('author','?')}: {c['data']['body']}"
                for c in (j[1]["data"]["children"][:15] if len(j) > 1 else [])
                if c.get("data", {}).get("body")]
    return {"platform": "reddit", "title": post.get("title", ""),
            "authors": [post["author"]] if post.get("author") else [],
            "text": body + ("\n\n## Top comments\n" + "\n".join(comments) if comments else ""),
            "capture_method": "api"}


def cap_youtube(url: str, platform: str, secrets: dict) -> dict:
    """Read creator captions first, with auto captions as a separate fallback."""
    if not which("yt-dlp"):
        raise RuntimeError("yt-dlp is unavailable")
    proc = subprocess.run(
        ["yt-dlp", "--ignore-config", "--no-cache-dir", "--no-playlist",
         "--socket-timeout", "10", "--retries", "0", "--extractor-retries", "0",
         "--skip-download", "--dump-single-json", "--", url],
        capture_output=True, text=True, timeout=38,
    )
    if proc.returncode:
        detail = proc.stderr.lower()
        if any(flag in detail for flag in ("sign in", "log in", "login", "not a bot")):
            raise RuntimeError("video access requires sign-in or browser confirmation")
        if "429" in detail:
            raise RuntimeError("video provider rate-limited the request (429)")
        raise RuntimeError("video metadata could not be retrieved")
    info = json.loads(proc.stdout)
    if info.get("entries") or not info.get("id"):
        raise RuntimeError("select an individual video or Bilibili part")
    uploader = info.get("uploader") or info.get("channel")
    date = info.get("upload_date") or ""
    resolved_url = info.get("webpage_url") or url
    if platform == "bilibili" and "p" in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query):
        resolved_url = url
    result = {
        "platform": platform, "title": info.get("title") or "Video",
        "authors": [uploader] if uploader else [],
        "published_at": f"{date[:4]}-{date[4:6]}-{date[6:8]}" if len(date) == 8 else None,
        "resolved_url": resolved_url, "content_kind": "transcript", "text": "",
    }
    domain_suffixes = ("youtube.com", "googlevideo.com") if platform == "youtube" else (
        "bilibili.com", "hdslb.com", "bilivideo.com")
    preferred = ("zh", "ai-zh", "en") if platform == "bilibili" else ("en", "zh")
    for field in ("subtitles", "automatic_captions"):
        tracks = info.get(field) or {}
        languages = [lang for lang in tracks if re.fullmatch(r"[a-zA-Z0-9-]{2,24}", lang)
                     and lang != "danmaku"]
        def language_rank(lang):
            return next((i for i, prefix in enumerate(preferred)
                         if lang == prefix or lang.startswith(prefix + "-")), len(preferred))
        for lang in sorted(languages, key=language_rank)[:4]:
            for track in tracks[lang]:
                ext = track.get("ext")
                if ext not in ("vtt", "srt"):
                    continue
                try:
                    data = (track["data"].encode("utf-8") if track.get("data") else
                            _fetch_bytes(track["url"], 4 * 1024 * 1024, allowed_hosts=domain_suffixes))
                except (OSError, ValueError, KeyError):
                    continue
                text = _vtt_to_text(data.decode("utf-8-sig", errors="replace"))
                if text:
                    last = re.findall(r"\[(\d{2}):(\d{2}):(\d{2})\]", text)
                    seconds = sum(int(v) * unit for v, unit in zip(last[-1], (3600, 60, 1)))
                    duration = info.get("duration")
                    partial = (duration and seconds < duration * 0.7 and duration - seconds > 60)
                    return {**result, "text": text, "lang": lang, "capture_method": "transcript",
                            "body_verified": not partial, "transcript_origin": field,
                            "partial_reason": ("captions end well before the video ends" if partial else None),
                            "artifacts": [(f"captions-{lang}.{ext}", data)]}
    if secrets.get("KF_TRANSCRIBE_PREVIEW") == "true":
        from . import speech
        audio_formats = [item for item in info.get("formats", []) if item.get("vcodec") == "none"
                         and item.get("acodec") not in (None, "none") and item.get("url")]
        for item in sorted(audio_formats, key=lambda f: f.get("abr") or 0)[:1]:
            try:
                with _open_public(item["url"], allowed_hosts=domain_suffixes,
                                  headers={"Range": "bytes=0-2097151", "Referer": resolved_url}) as audio:
                    data = audio.read(2 * 1024 * 1024)
                track, seconds = speech.recognize(data)
            except (ValueError, OSError, RuntimeError, subprocess.SubprocessError):
                continue
            duration = info.get("duration")
            full = bool(duration and duration <= speech.PREVIEW_SECONDS and seconds >= duration * 0.95)
            return {**result, "capture_method": "whisper", "text": _vtt_to_text(track.decode()),
                    "transcript_origin": "local speech recognition",
                    "body_verified": True,
                    "partial_reason": None if full else f"machine transcript preview, first {int(seconds)} seconds",
                    "artifacts": [("machine-transcript.vtt", track)]}
    return {**result, "capture_method": "api", "content_kind": "video metadata",
            "text": info.get("description") or "",
            "partial_reason": "no readable captions; supply a transcript or accessible audio"}


def _pick_vtt(vtts: list, prefs=("en", "zh-Hans", "zh")) -> Path:
    """Deterministic subtitle choice: honour language priority, then filename, so
    re-captures of the same video never pick a different track (which would change
    content_hash and spawn a bogus new version)."""
    def rank(p: Path):
        parts = p.name.split(".")
        lang = parts[-2] if len(parts) >= 3 else ""
        try:
            r = prefs.index(lang)
        except ValueError:
            r = len(prefs)
        return (r, p.name)
    return sorted(vtts, key=rank)[0]


def _vtt_to_text(vtt: str) -> str:
    from html import unescape
    lines = []
    for block in re.split(r"\n\s*\n", vtt.replace("\r\n", "\n")):
        match = re.search(r"(?m)^(?P<time>(?:\d{2}:)?\d{2}:\d{2})[.,]\d+[^\n]*-->[^\n]*\n", block)
        if not match:
            continue
        text = " ".join(unescape(re.sub(r"<[^>]*>", "", block[match.end():])).split())
        if text:
            timestamp = match["time"]
            timestamp = timestamp if timestamp.count(":") == 2 else "00:" + timestamp
            lines.append(f"- [{timestamp}] {text}")
    return "\n".join(lines)


# ---- images / files ----------------------------------------------------------------------
def ocr_images(paths: list, secrets: dict) -> tuple[str, list[str]]:
    """Return (merged_text, warnings). Page order preserved, boundaries marked.
    Backends: paddleocr if installed. No backend → empty text + warning (the
    caller quarantines with the images kept as evidence — never fake OCR)."""
    warnings: list[str] = []
    if which("tesseract"):
        pages = []
        for i, path in enumerate(paths, 1):
            try:
                proc = subprocess.run(["tesseract", str(path), "stdout", "-l", "chi_sim+eng"],
                                      capture_output=True, text=True, timeout=18, check=True)
                value = proc.stdout.strip()
            except (OSError, subprocess.SubprocessError):
                value = ""
            if value:
                pages.append(f"### Page {i}\n\n{value}")
            else:
                warnings.append(f"no readable OCR for image {i}")
        return "\n\n".join(pages), warnings
    try:
        from paddleocr import PaddleOCR
    except Exception:
        return "", ["no OCR backend available (paddleocr not installed; "
                    "vision-API OCR not configured)"]
    ocr = PaddleOCR(use_angle_cls=True, lang="ch")
    pages = []
    for i, img in enumerate(paths, 1):
        try:
            res = ocr.ocr(str(img), cls=True)[0] or []
        except Exception as e:
            warnings.append(f"page {i} OCR failed: {e}")
            res = []
        lines, confs = [], []
        for l in res:
            lines.append(l[1][0])
            confs.append(l[1][1])
        if confs and sum(confs) / len(confs) < 0.75:
            warnings.append(f"page {i}: low OCR confidence ({sum(confs)/len(confs):.0%})")
        pages.append(f"--- page {i} ---\n" + "\n".join(lines))
    return "\n\n".join(pages), warnings


def cap_file(path: str, secrets: dict) -> dict:
    p = Path(path)
    if p.suffix.lower() == ".pdf":
        with p.open("rb") as source:
            return extract_pdf(source.read(MAX_PDF_BYTES + 1), p.stem)
    if p.suffix.lower() in (".vtt", ".srt"):
        if p.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("subtitle file exceeds the capture size limit")
        data = p.read_bytes()
        transcript = _vtt_to_text(data.decode("utf-8-sig", errors="replace"))
        return {"platform": "manual", "title": p.stem, "text": transcript,
                "content_kind": "transcript", "body_verified": bool(transcript),
                "capture_method": "transcript" if transcript else "none",
                "artifacts": [("captions" + p.suffix.lower(), data)]}
    if p.suffix.lower() in (".html", ".htm"):
        if p.stat().st_size > 6 * 1024 * 1024:
            raise ValueError("HTML clip exceeds the capture size limit")
        html = p.read_text(encoding="utf-8", errors="replace")
        text = extract_readable(html)
        return {"platform": "manual", "title": title_of(html) or p.stem, "text": text,
                "content_kind": "article", "body_verified": _matches_article_body(html, text),
                "capture_method": "manual"}
    if p.suffix.lower() in (".txt", ".md"):
        text = p.read_text(encoding="utf-8", errors="replace")
        return {"platform": "manual", "title": p.stem, "text": text, "capture_method": "manual",
                "content_kind": "article", "body_verified": bool(text.strip())}
    return {"platform": "manual", "title": p.stem, "text": "",
            "capture_method": "none", "attachments": [str(p)],
            "adapter_error": f"unsupported file format {p.suffix}; supply a PDF or extracted text"}


# ---- cascade orchestration ---------------------------------------------------------------
CASCADES: dict[str, list[Callable]] = {
    "github": [cap_github],
    "arxiv": [cap_paper_pdf, cap_arxiv, cap_generic, cap_jina],
    "hackernews": [cap_hn],
    "reddit": [cap_reddit],
    "youtube": [cap_youtube],
    "bilibili": [cap_youtube],
    "xiaohongshu": [cap_rednote, cap_browser],
    "wechat": [cap_wechat, cap_jina, cap_browser],
    "default": [cap_generic, cap_browser, cap_jina],
    "cn": [cap_generic, cap_browser, cap_jina],
}
CN_PLATFORMS = {"xiaohongshu", "zhihu", "weibo", "bilibili", "douyin", "toutiao"}


def _resolve_short_url(url, platform):
    host = urllib.parse.urlsplit(url).hostname or ""
    if host == "b23.tv":
        allowed = ("b23.tv", "bilibili.com")
    elif host in ("xhslink.com", "xhslink.cn"):
        allowed = ("xhslink.com", "xhslink.cn", "xiaohongshu.com", "rednote.com")
    else:
        return url
    with _open_public(url, timeout=12, allowed_hosts=allowed,
                      headers=MOBILE_HEADERS if platform == "xiaohongshu" else None) as response:
        final = response.geturl()
    path = urllib.parse.urlsplit(final).path
    item_path = (r"/video/(?:BV|av)[a-zA-Z0-9]+/?$" if platform == "bilibili"
                 else r"/(?:explore|discovery/item)/[a-zA-Z0-9]+/?$")
    if platform_of(final) != platform or not re.fullmatch(item_path, path):
        raise ValueError("short link redirected outside its platform")
    return final


def fetch_url(url: str, secrets: dict, quality_check: Callable[[dict], bool],
              grader: Optional[Callable[[dict], dict]] = None) -> dict:
    """Prefer the first complete capture, retaining the best partial evidence."""
    plat = platform_of(url)
    try:
        target = _resolve_short_url(url, plat)
    except (OSError, ValueError):
        target = url
    chain = CASCADES.get(plat) or (CASCADES["cn"] if plat in CN_PLATFORMS else CASCADES["default"])
    if plat != "arxiv" and urllib.parse.urlsplit(target).path.lower().endswith(".pdf"):
        chain = [cap_public_pdf, *chain]
    if plat in ("zhihu", "toutiao") and secrets.get("KF_PUBLIC_READER") == "firecrawl":
        chain = [*chain, cap_public_firecrawl]
    best, best_rank, tried = None, None, []
    ranks = {"complete": 3, "usable_partial": 2, "metadata_only": 1, "failed": 0}
    for adapter in chain:
        try:
            cap = adapter(target, plat, secrets)
            cap.setdefault("platform", plat)
            if target != url:
                cap.setdefault("resolved_url", target)
            grade = grader(cap) if grader else None
        except Exception as e:
            # urllib error bodies and URLs can contain private share tokens.
            reason = e.reason if isinstance(e, urllib.error.URLError) else e
            detail = str(reason) if isinstance(e, RuntimeError) else type(reason).__name__
            if isinstance(e, urllib.error.HTTPError):
                detail = f"HTTP {e.code}"
            tried.append(f"{adapter.__name__}: {detail}")
            continue
        if (grade["capture_status"] == "complete" if grade else quality_check(cap)):
            cap["via"] = adapter.__name__
            return cap
        status = grade["capture_status"] if grade else "partial"
        tried.append(f"{adapter.__name__}: {status}")
        rank = (ranks.get(status, 0), bool(cap.get("artifacts")),
                grade["content_completeness"] if grade else len(cap.get("text") or ""))
        if best_rank is None or rank > best_rank:
            best, best_rank = cap, rank
    if best is None:
        best = {"platform": plat, "title": None, "text": "", "capture_method": "none"}
    if target != url:
        best.setdefault("resolved_url", target)
    if best_rank is None or (grader and best_rank[0] == 0):
        best.setdefault("adapter_error", " | ".join(tried) or "all methods failed")
    best["via"] = "cascade_exhausted"
    return best
