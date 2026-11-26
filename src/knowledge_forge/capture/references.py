"""Discover explicit source references and read bounded public publisher text."""
from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import time
from dataclasses import dataclass
from html import escape, unescape
from html.parser import HTMLParser
from typing import Sequence
from urllib.parse import SplitResult, parse_qsl, quote, unquote, urljoin, urlsplit, urlunsplit

from ..urlnorm import canonicalize
from .adapters import extract_readable, title_of


_HTTPS = re.compile(r"(?<![a-z0-9_:/=])https://[^\s<>\[\]\"'`，。；：！？、（）【】《》〈〉『』「」…]+", re.I)
_IMAGE = re.compile(r"!\[[^\]\n]*\]\s*(?:\(([^()\n]|\([^()\n]*\))*\)|\[[^\]\n]*\])|<img\b[^>]*>", re.I)
_MEDIA_TAG = re.compile(r"<(?:audio|video|source|iframe)\b[^>]*>", re.I)
_REFERENCE = re.compile(r"\[([^\]\n]+)\][ \t]*\[([^\]\n]*)\]")
_REFERENCE_DEFINITION = re.compile(r"(?m)^[ \t]*\[([^\]\n]+)\]:[^\n]*")
_URL_ENTITY = re.compile(r"&(?:[a-z]+|#[0-9]+|#x[0-9a-f]+);", re.I)
_CJK = re.compile(r"[ぁ-ヿ一-鿿가-힣]")
_ACCESS_TITLE = re.compile(r"(?:access denied|just a moment|verify you are human|attention required|forbidden|sign in|log in|登录|请先登录)\b", re.I)
_NONCONTENT = frozenset((
    "t.co", "bit.ly", "tinyurl.com", "lnkd.in", "ow.ly", "goo.gl", "xhslink.com", "xhslink.cn",
    "twitter.com", "x.com", "linkedin.com", "facebook.com", "instagram.com", "threads.net",
    "reddit.com", "weibo.com", "xiaohongshu.com", "tiktok.com", "douyin.com", "youtube.com",
    "youtu.be", "bilibili.com", "b23.tv", "shields.io", "badgen.net", "google-analytics.com",
    "googletagmanager.com", "doubleclick.net", "gravatar.com",
))
_ASSET = re.compile(
    r"\.(?:7z|bz2|deb|dmg|exe|gz|iso|jar|msi|pkg|rar|rpm|tar|tgz|whl|xz|zip|"
    r"apk|appimage|bin|dll|gguf|ipynb|onnx|pt|pth|safetensors|so|wasm|"
    r"avif|gif|heic|ico|jpe?g|png|svg|webp|mp[34]|ogg|wav|webm|mov|m4a|"
    r"css|js|ps1|sh|pdf|docx?|pptx?|xlsx?)$", re.I,
)
_SECRET_PARAMS = frozenset((
    "token", "accesstoken", "authtoken", "refreshtoken", "oauthtoken", "idtoken", "xsectoken", "apitoken",
    "auth", "authkey", "authorization", "apikey", "key", "password", "secret", "signature", "sig",
    "session", "sessionid",
    "awsaccesskeyid", "credential", "policy", "keypairid", "privatetoken", "clientsecret",
    "sessiontoken", "accesskey", "jwt", "bearertoken", "clienttoken",
))
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_GITHUB_PAGES = frozenset(("about", "collections", "features", "marketplace", "orgs", "search", "sponsors", "topics", "users"))
_HF_PAGES = frozenset(("api", "blog", "docs", "join", "login", "models", "organizations", "papers", "posts", "settings"))
_PAGE_TYPES = {"text/plain", "text/markdown", "text/html", "application/xhtml+xml", "application/vnd.github.raw", "application/vnd.github.raw+json"}
_READ_LIMIT = 384 * 1024
_REDIRECT_LIMIT = 2
_TIME_LIMIT = 10.0


@dataclass(frozen=True)
class ResourceEvidence:
    url: str
    status: str
    title: str = ""
    summary: str = ""
    evidence_url: str = ""
    detail: str = ""


class _Blocked(ValueError):
    pass


class _Unavailable(ValueError):
    pass


def _within_domain(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _destination(url: str) -> tuple[SplitResult, str]:
    if len(url) > 2048 or re.search(r"[\x00-\x20\x7f\s<>\"`\\]", url):
        raise _Blocked("Invalid public source URL")
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").encode("idna").decode("ascii")
        port = parsed.port
        parameters = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=50)
        fragment = parsed.fragment.rsplit("?", 1)[-1]
        if "=" in fragment:
            parameters.extend(parse_qsl(fragment, keep_blank_values=True, max_num_fields=50))
    except (ValueError, UnicodeError) as exc:
        raise _Blocked("Invalid public source URL") from exc
    if parsed.scheme != "https" or parsed.username is not None or parsed.password is not None or port not in (None, 443):
        raise _Blocked("Expected a public HTTPS URL without credentials")
    if not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z](?:[a-z0-9-]*[a-z0-9])?", host):
        raise _Blocked("Expected a public domain name")
    if host.endswith((".local", ".localhost", ".internal", ".lan", ".home", ".corp", ".onion", ".invalid")):
        raise _Blocked("Private or reserved domain")
    if any(_within_domain(host, domain) for domain in _NONCONTENT):
        raise _Blocked("Social, tracking, or redirect link")
    for key, _ in parameters:
        key = re.sub(r"[^a-z0-9]", "", key.lower())
        if key in _SECRET_PARAMS or key.startswith(("xamz", "xgoog")):
            raise _Blocked("Credential or signed URL")
    path = unquote(parsed.path).lower().rstrip("/")
    if _ASSET.search(path) or any(part in path.split("/") for part in ("login", "signin", "logout", "download")):
        raise _Blocked("Download or account link")
    if _within_domain(host, "huggingface.co") and "/resolve/" in path:
        raise _Blocked("Download or account link")
    return parsed, host


def _reference_name(name: str) -> str:
    return " ".join(name.casefold().split())


def _link_url(raw: str, context: str) -> str:
    if context == "](":
        depth = 0
        for index, char in enumerate(raw):
            if char == "(":
                depth += 1
            elif char == ")":
                if depth == 0:
                    raw = raw[:index]
                    break
                depth -= 1
    elif not context.endswith("<"):
        raw = raw.rstrip(".,;:!?")
        while raw.endswith(")") and raw.count(")") > raw.count("("):
            raw = raw[:-1].rstrip(".,;:!?")
    return _URL_ENTITY.sub(lambda match: unescape(match[0]), raw)


def discover(text: str, origin: str | None = None) -> list[str]:
    """Return cited HTTPS URLs in article order, preserving their spelling."""
    content = _MEDIA_TAG.sub(" ", _IMAGE.sub(" ", text))
    image_refs = {_reference_name(match[2] or match[1]) for match in _REFERENCE.finditer(text)
                  if match.start() > 0 and text[match.start() - 1] == "!"}
    link_refs = {_reference_name(match[2] or match[1]) for match in _REFERENCE.finditer(content)}
    image_only = image_refs - link_refs
    if image_only:
        content = _REFERENCE_DEFINITION.sub(
            lambda match: " " if _reference_name(match[1]) in image_only else match[0], content
        )
    seen = set()
    if origin:
        try:
            seen.add(canonicalize(origin))
        except ValueError:
            pass
    links = []
    for match in _HTTPS.finditer(content):
        start = match.start()
        url = _link_url(match[0], content[max(0, start - 2):start])
        try:
            _destination(url)
            key = canonicalize(url)
        except ValueError:
            continue
        if key not in seen:
            seen.add(key)
            links.append(url)
    return links


def _open_connection(host: str, timeout: float) -> http.client.HTTPConnection:
    """Resolve once and connect to a checked address with the publisher's TLS name."""
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses:
        raise _Unavailable("Publisher domain did not resolve")
    for _, _, _, _, address in addresses:
        ip = ipaddress.ip_address(address[0])
        ip = getattr(ip, "ipv4_mapped", None) or ip
        if not ip.is_global or ip.is_multicast or ip.is_reserved or getattr(ip, "is_site_local", False):
            raise _Blocked("Domain resolves to a private or reserved network")

    context = ssl.create_default_context()
    deadline = time.monotonic() + timeout
    last_error = None
    for family, kind, protocol, _, address in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        raw = socket.socket(family, kind, protocol)
        try:
            raw.settimeout(remaining)
            raw.connect(address)
            secured = context.wrap_socket(raw, server_hostname=host)
        except OSError as exc:
            raw.close()
            last_error = exc
            continue
        connection = http.client.HTTPConnection(host, timeout=timeout)
        connection.auto_open = False
        connection.sock = secured
        return connection
    raise _Unavailable("Could not connect to the publisher over HTTPS") from last_error


def _fetch_text(url: str, *, accept: str) -> tuple[str, str, str]:
    """Read small text responses directly, checking all redirects before connecting."""
    _, source_host = _destination(url)
    deadline = time.monotonic() + _TIME_LIMIT
    for attempt in range(_REDIRECT_LIMIT + 1):
        parsed, host = _destination(url)
        if host.removeprefix("www.") != source_host.removeprefix("www."):
            raise _Blocked("Redirect destination is outside the publisher")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _Unavailable("Publisher response timed out")
        path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
        if parsed.query:
            path += "?" + quote(parsed.query, safe="/%?:@!$&'()*+,;=-._~[]")
        connection = _open_connection(host, min(remaining, 5))
        try:
            connection.request("GET", path, headers={
                "Accept": accept, "Accept-Encoding": "identity", "User-Agent": "KnowledgeForge/1.5",
            })
            response = connection.getresponse()
            headers = response.headers
            if response.status in (301, 302, 303, 307, 308):
                if attempt == _REDIRECT_LIMIT or not headers.get("Location"):
                    raise _Blocked("Publisher redirect limit exceeded")
                url = urljoin(url, headers["Location"])
                continue
            if response.status in (401, 403, 451):
                raise _Blocked(f"Publisher refused public access (HTTP {response.status})")
            if response.status != 200:
                raise _Unavailable(f"Publisher returned HTTP {response.status}")
            media_type = headers.get_content_type()
            if headers.get("Cf-Mitigated", "").lower() == "challenge":
                raise _Blocked("Publisher returned an access challenge")
            if (not headers.get("Content-Type") or media_type not in _PAGE_TYPES
                    or headers.get_content_disposition() == "attachment"
                    or headers.get("Content-Encoding", "identity").lower() != "identity"):
                raise _Blocked("Publisher response is not supported public text")
            length = headers.get("Content-Length", "")
            if length.isdecimal() and (len(length) > 9 or int(length) > _READ_LIMIT):
                raise _Blocked("Publisher text exceeds the review size limit")
            body = bytearray()
            while len(body) <= _READ_LIMIT:
                if time.monotonic() >= deadline:
                    raise _Unavailable("Publisher response timed out")
                chunk = response.read1(min(32768, _READ_LIMIT + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
            if len(body) > _READ_LIMIT:
                raise _Blocked("Publisher text exceeds the review size limit")
            if b"\0" in body:
                raise _Blocked("Publisher response is not supported public text")
            charset = headers.get_content_charset() or "utf-8"
            try:
                text = body.decode(charset, "replace")
            except (LookupError, ValueError):
                text = body.decode("utf-8", "replace")
            return text, media_type, urlunsplit(parsed._replace(fragment=""))
        finally:
            connection.close()
    raise _Blocked("Publisher redirect limit exceeded")


def _publisher_page(url: str, parsed: SplitResult, host: str) -> tuple[str, str, str]:
    parts = parsed.path.strip("/").split("/")
    if host in ("github.com", "www.github.com") and len(parts) >= 2 and parts[0].lower() not in _GITHUB_PAGES:
        owner, repo = parts[:2]
        repo = repo.removesuffix(".git")
        if all(_SEGMENT.fullmatch(part) for part in (owner, repo)):
            endpoint = f"https://api.github.com/repos/{owner}/{repo}/readme"
            if len(parts) == 2:
                return endpoint, f"{owner}/{repo}", "Repository README"
            if len(parts) == 4 and parts[2] == "tree" and _SEGMENT.fullmatch(parts[3]):
                return f"{endpoint}?ref={parts[3]}", f"{owner}/{repo}", "Repository README"
    if host in ("huggingface.co", "www.huggingface.co") and parts[0].lower() not in _HF_PAGES:
        if parts[0] in ("datasets", "spaces"):
            valid = len(parts) == 3
            label = "Dataset card" if parts[0] == "datasets" else "Space README"
        else:
            valid = 1 <= len(parts) <= 2
            label = "Model card"
        if valid and all(_SEGMENT.fullmatch(part) for part in parts):
            names = "/".join(parts)
            return f"https://huggingface.co/{names}/raw/main/README.md", names, label
    return url, host + (parsed.path if parsed.path != "/" else ""), "Publisher page"


class _PageDescription(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = ""

    def handle_starttag(self, tag, attrs):
        if tag == "meta" and not self.text:
            meta = dict(attrs)
            if (meta.get("name") or meta.get("property")) in ("description", "og:description"):
                self.text = meta.get("content") or ""


def _clean_paragraph(paragraph: str) -> str:
    paragraph = re.sub(r"\[([^\]\n]+)\]\([^\)\n]+\)", r"\1", paragraph)
    paragraph = _HTTPS.sub("", unescape(paragraph))
    paragraph = re.sub(r"<[^>]+>", " ", paragraph)
    return " ".join(paragraph.split())


def _description(body: str) -> str:
    body = re.sub(r"\A\s*---\s*\n.*?\n---[^\S\n]*\n", "", body, count=1, flags=re.S)
    body = re.sub(r"(?ms)^\s*(?:```|~~~).*?^\s*(?:```|~~~)[^\S\n]*$", "", body)
    body = _IMAGE.sub("", body)
    for paragraph in re.split(r"\n\s*\n", body):
        text = _clean_paragraph(paragraph)
        cjk = bool(_CJK.search(text))
        if len(text) < (12 if cjk else 24) or text.startswith(("#", "|", "- ", "* ", "[!", "$ ", "Copyright")):
            continue
        excerpt = " ".join(text.split(maxsplit=24)[:24])
        limit = 120 if cjk else 240
        if len(excerpt) > limit:
            excerpt = excerpt[:limit]
            if not cjk and " " in excerpt:
                excerpt = excerpt.rsplit(" ", 1)[0]
        return excerpt if excerpt == text else excerpt + "…"
    return ""


def inspect_resource(url: str) -> ResourceEvidence:
    """Read only the cited page or its first-party README/card endpoint."""
    try:
        parsed, host = _destination(url)
    except _Blocked as exc:
        return ResourceEvidence(url, "blocked", detail=str(exc))

    endpoint, title, source = _publisher_page(url, parsed, host)
    accept = "text/html, text/plain, text/markdown, application/xhtml+xml"
    if source == "Repository README":
        accept = "application/vnd.github.raw"
    try:
        body, media_type, fetched_url = _fetch_text(endpoint, accept=accept)
    except _Blocked as exc:
        return ResourceEvidence(url, "blocked", title=title, detail=str(exc))
    except _Unavailable as exc:
        return ResourceEvidence(url, "unavailable", title=title, detail=str(exc))
    except (OSError, http.client.HTTPException, ValueError):
        return ResourceEvidence(url, "unavailable", title=title, detail="Could not read public publisher text")

    if media_type in ("text/html", "application/xhtml+xml"):
        parser = _PageDescription()
        title = title_of(body) or title
        if _ACCESS_TITLE.match(title.strip()):
            return ResourceEvidence(url, "blocked", title=title, detail="Publisher returned an access challenge")
        try:
            parser.feed(body)
        except AssertionError:
            pass
        summary = _description(parser.text) or _description(extract_readable(body))
    else:
        heading = re.search(r"(?m)^#\s+([^\n]+)", body)
        if heading and source != "Repository README":
            title = _clean_paragraph(heading[1]) or title
        summary = _description(body)
    if not summary:
        return ResourceEvidence(url, "unavailable", title=title, evidence_url=fetched_url,
                                detail="Public page provided no usable description")
    return ResourceEvidence(url, "reviewed", title, summary, fetched_url, source)


def _inline(text: str) -> str:
    escaped = escape(" ".join(text.split()), quote=False)
    return re.sub(r"([\\`*_{}\[\]])", r"\\\1", escaped)


def render_review(links: Sequence[str], evidence: Sequence[ResourceEvidence] = (), commentary: str | None = None) -> str:
    """Render every cited link with its review evidence or current access status."""
    sections = ["## Linked resources"]
    if commentary:
        sections.append(_inline(commentary))
    if not links:
        sections.append("No explicit public project or documentation links were found in the source.")
    reviewed = {item.url: item for item in evidence}
    for url in links:
        try:
            parsed, host = _destination(url)
        except _Blocked:
            continue
        item = reviewed.get(url)
        title = item.title if item and item.title else _publisher_page(url, parsed, host)[1]
        cited_url = f"<{url}>" if item and item.status == "reviewed" else f"`{url}`"
        lines = [f"### {_inline(title)[:180]}", f"- Cited link · {cited_url}"]
        if not item:
            lines.append("- Linked only")
        elif item.status == "reviewed":
            lines.append(f"- Publisher excerpt · {_inline(item.summary)}")
        else:
            lines.append(f"- {item.status.capitalize()} · {_inline(item.detail)}")
        if item and item.evidence_url and item.status == "reviewed":
            try:
                _destination(item.evidence_url)
            except _Blocked:
                pass
            else:
                lines.append(f"- Evidence · {_inline(item.detail)} · <{item.evidence_url}>")
        sections.append("\n".join(lines))
    return "\n\n".join(sections) + "\n"
