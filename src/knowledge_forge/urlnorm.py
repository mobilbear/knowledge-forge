"""Canonical URL logic. Preserves the original shared URL; canonicalisation is
used only for identity (dedup / idempotency), never to rewrite provenance.
"""
from __future__ import annotations

import urllib.parse
import re

_TRACKING_PREFIXES = (
    "utm_", "ref", "share", "fbclid", "gclid", "spm", "from", "wechat", "chksm",
    "mpshare", "scene", "srcid", "clicktime", "enterid", "s_r", "igsh",
)
# platform-specific params that ARE identity and must be kept
_KEEP_ALWAYS = {"id", "v", "p", "aid", "bvid", "item_id", "group_id", "sn", "mid", "idx", "__biz"}

PLATFORM_HOSTS = {
    "github.com": "github", "raw.githubusercontent.com": "github", "gist.github.com": "github",
    "reddit.com": "reddit", "old.reddit.com": "reddit",
    "news.ycombinator.com": "hackernews",
    "youtube.com": "youtube", "youtu.be": "youtube", "m.youtube.com": "youtube",
    "zhihu.com": "zhihu", "zhuanlan.zhihu.com": "zhihu",
    "toutiao.com": "toutiao", "quora.com": "quora",
    "twitter.com": "x", "x.com": "x",
    "linkedin.com": "linkedin",
    "xiaohongshu.com": "xiaohongshu", "xhslink.com": "xiaohongshu", "xhslink.cn": "xiaohongshu", "rednote.com": "xiaohongshu",
    "weibo.com": "weibo", "bilibili.com": "bilibili", "b23.tv": "bilibili",
    "douyin.com": "douyin", "ixigua.com": "toutiao",
    "substack.com": "substack",
    "mp.weixin.qq.com": "wechat",
    "arxiv.org": "arxiv",
}


def platform_of(url: str) -> str:
    host = urllib.parse.urlparse(url).netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    for h, plat in PLATFORM_HOSTS.items():
        if host == h or host.endswith("." + h):
            return plat
    return "web"


def canonicalize(url: str) -> str:
    """Strip tracking params + fragment; lower-case scheme/host; keep identity params.
    WeChat article URLs keep __biz/mid/idx/sn which jointly identify the article."""
    u = urllib.parse.urlparse(url.strip())
    host = u.netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    keep = []
    for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True):
        kl = k.lower()
        if kl in _KEEP_ALWAYS:
            keep.append((k, v))
        elif not kl.startswith(_TRACKING_PREFIXES):
            keep.append((k, v))
    path = u.path.rstrip("/") or "/"
    platform = platform_of(url)
    params = dict(keep)
    if platform == "youtube":
        parts = path.strip("/").split("/")
        video = None
        if host == "youtu.be" and len(parts) == 1:
            video = parts[0]
        elif path == "/watch":
            video = params.get("v")
        elif len(parts) == 2 and parts[0] in ("shorts", "live", "embed"):
            video = parts[1]
        if video:
            return "https://youtube.com/watch?" + urllib.parse.urlencode({"v": video})
    if platform == "bilibili" and host != "b23.tv" and path.startswith("/video/"):
        page = params.get("p")
        return "https://bilibili.com" + path + (
            "?" + urllib.parse.urlencode({"p": page}) if page and page != "1" else "")
    if platform == "arxiv":
        match = re.fullmatch(r"/(?:abs|pdf|html)/(\d{4}\.\d{4,5})(?:v\d+)?(?:\.pdf)?", path)
        if match:
            return "https://arxiv.org/abs/" + match[1]
    if platform == "xiaohongshu" and host not in ("xhslink.com", "xhslink.cn"):
        parts = path.strip("/").split("/")
        if len(parts) >= 2 and "/".join(parts[:-1]) in ("explore", "discovery/item"):
            return "https://xiaohongshu.com/explore/" + parts[-1]
    return urllib.parse.urlunparse(
        (u.scheme.lower() or "https", host, path, "", urllib.parse.urlencode(keep), "")
    )
