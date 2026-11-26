"""Unit tests: identity, URL canonicalization, frontmatter, discovery,
quality gate, and the capture state machine."""
import pytest

from knowledge_forge import ids, urlnorm
from knowledge_forge import frontmatter as fm
from knowledge_forge.quality import gate


# ---- ids -----------------------------------------------------------------------
def test_source_id_deterministic():
    a = ids.source_id("https://example.com/x")
    b = ids.source_id("https://example.com/x")
    assert a == b and a.startswith("src_") and len(a) == 20


def test_source_id_requires_basis():
    with pytest.raises(ValueError):
        ids.source_id(None, None)


def test_tag_normalization_cjk():
    assert ids.normalize_tag("  Quant Risk ") == "quant-risk"
    assert ids.normalize_tag("术数") == "术数"
    assert ids.tag_id("术数").startswith("tag_")


# ---- urlnorm --------------------------------------------------------------------
def test_canonicalize_strips_tracking():
    u = "https://www.Example.com/a/?utm_source=x&fbclid=1&id=9"
    c = urlnorm.canonicalize(u)
    assert "utm" not in c and "fbclid" not in c and "id=9" in c
    assert c.startswith("https://example.com/a")


def test_canonicalize_keeps_wechat_identity():
    u = ("https://mp.weixin.qq.com/s?__biz=MzA=&mid=22&idx=1&sn=abc"
         "&chksm=zz&scene=21")
    c = urlnorm.canonicalize(u)
    for k in ("__biz", "mid", "idx", "sn"):
        assert k + "=" in c
    assert "chksm" not in c and "scene" not in c


def test_same_share_same_id():
    a = ids.source_id(urlnorm.canonicalize("https://x.com/p?utm_source=a"))
    b = ids.source_id(urlnorm.canonicalize("https://x.com/p?utm_source=b"))
    assert a == b


def test_platform_detection():
    assert urlnorm.platform_of("https://mp.weixin.qq.com/s?x=1") == "wechat"
    assert urlnorm.platform_of("https://zhuanlan.zhihu.com/p/1") == "zhihu"
    assert urlnorm.platform_of("https://some.blog.org/post") == "web"


# ---- frontmatter ------------------------------------------------------------------
def test_frontmatter_roundtrip_unicode_and_nesting():
    meta = {"id": "src_abc", "title": 'He said: "跨语言: test #1"',
            "tags": ["a", "术数"], "nested": {"k": [1, 2]}}
    text = fm.dump(meta, "body 内容")
    meta2, body2 = fm.parse(text)
    assert meta2["title"] == meta["title"]
    assert meta2["nested"] == {"k": [1, 2]}
    assert body2.strip() == "body 内容"


def test_frontmatter_multiline_and_colon_values():
    # cases the v1.0 regex parser could not handle
    raw = '---\ntitle: "a: b: c"\ndesc: |\n  line1\n  line2\n---\n\nbody\n'
    meta, body = fm.parse(raw)
    assert meta["title"] == "a: b: c"
    assert "line1" in meta["desc"] and "line2" in meta["desc"]


def test_frontmatter_malformed_raises():
    with pytest.raises(fm.FrontmatterError):
        fm.parse("---\nbad: [unclosed\n---\n\nbody")


def test_no_frontmatter_tolerated():
    meta, body = fm.parse("just text")
    assert meta == {} and body == "just text"


# ---- quality gate + state machine ----------------------------------------------------
def _rec(text, method="direct", **kw):
    r = {"text": text, "capture_method": method, "state": "received",
         "title": kw.pop("title", "T"), "images": []}
    r.update(kw)
    r["state"] = "received"
    gate.transition(r, "capturing")
    gate.transition(r, "normalized")
    return r


def test_gate_complete():
    r = gate.assess(_rec("real content. " * 100))
    assert r["capture_status"] == "complete" and r["state"] == "usable"
    assert not r["needs_retry"]


def test_gate_login_wall_is_metadata_only():
    r = gate.assess(_rec("请登录后查看全文 " * 5))
    assert r["capture_status"] == "metadata_only"
    assert r["needs_retry"] and r["state"] == "metadata_only"
    assert any("login" in x for x in r["quality_reasons"])


def test_gate_truncation_is_partial():
    r = gate.assess(_rec("正文开始了这里有相当多内容 " * 40 + " 阅读全文"))
    assert r["capture_status"] == "usable_partial"
    assert "full body after truncation point" in r["missing_elements"]


def test_gate_adapter_error_is_failed():
    r = _rec("", method="none")
    r["adapter_error"] = "all methods failed"
    r = gate.assess(r)
    assert r["capture_status"] == "failed" and r["state"] == "failed"


def test_state_machine_rejects_illegal_jump():
    r = {"state": "received"}
    with pytest.raises(gate.StateError):
        gate.transition(r, "committed")
