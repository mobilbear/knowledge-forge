from email.message import Message
from io import BytesIO
from socket import AF_INET, SOCK_STREAM
from types import SimpleNamespace

import pytest

from knowledge_forge.capture import references


def test_discover_keeps_explicit_links_in_order():
    source = """
    正文：[项目](https://github.com/example/tool#readme)，模型 https://huggingface.co/example/model。
    文档 <https://docs.example.org/guide?version=2>，另有 https://docs.example.org/help_(setup)。
    [english docs](https://docs.example.org/overview). ![icon][doc-badge]
    [doc-badge]: https://docs.example.org/badge
    [同一文档](https://docs.example.org/guide?version=2&utm_source=feed#next)
    ![preview](https://docs.example.org/preview) <img src="https://docs.example.org/logo">
    https://docs.example.org/banner.png [分享](https://twitter.com/share?url=page)
    https://t.co/short https://example.org/posts/main#top http://docs.example.org/insecure
    https://example.org/posts/main/?utm_medium=feed
    """
    assert references.discover(source, "https://example.org/posts/main") == [
        "https://github.com/example/tool#readme",
        "https://huggingface.co/example/model",
        "https://docs.example.org/guide?version=2",
        "https://docs.example.org/help_(setup)",
        "https://docs.example.org/overview",
    ]


def test_discover_rejects_credentials_private_hosts_and_downloads():
    source = """
    https://sam:password@docs.example.org/project
    https://localhost/docs https://internal.local/docs https://10.0.0.1/docs
    https://docs.example.org:8443/project
    https://docs.example.org/share?token=secret
    https://docs.example.org/share?X-Amz-Signature=signature
    https://docs.example.org/share?private_token=value https://docs.example.org/share?client_secret=value
    https://docs.example.org/share?session_token=value https://docs.example.org/share?access_key=value
    https://docs.example.org/share?jwt=value https://docs.example.org/article#access_token=value
    https://docs.example.org/article#/start?bearer_token=value
    https://docs.example.org/artifacts/tool%2Ezip
    https://github.com/example/tool/releases/download/v1/tool
    https://huggingface.co/example/model/resolve/main/weights
    """
    assert references.discover(source) == []


@pytest.mark.parametrize(
    ("link", "endpoint", "kind", "body", "description"),
    [
        (
            "https://github.com/example/tool#readme",
            "https://api.github.com/repos/example/tool/readme",
            "text/plain",
            "# Example Tool\n\n![badge](https://example.org/badge)\n\n"
            "Example Tool converts published documents into searchable notes.",
            "converts published documents",
        ),
        (
            "https://github.com/example/tool/tree/experimental",
            "https://api.github.com/repos/example/tool/readme?ref=experimental",
            "text/plain",
            "# Example Tool\n\nThe experimental branch adds a different export format.",
            "adds a different export format",
        ),
        (
            "https://github.com/example/tool/tree/experimental/examples",
            "https://github.com/example/tool/tree/experimental/examples",
            "text/plain",
            "The examples directory gives usage examples for an export format.",
            "gives usage examples",
        ),
        (
            "https://huggingface.co/example/model",
            "https://huggingface.co/example/model/raw/main/README.md",
            "text/plain",
            "---\nlicense: mit\n---\n# Example model\n\n"
            "This model matches document paragraphs by multilingual similarity.",
            "multilingual similarity",
        ),
        (
            "https://docs.example.org/guide",
            "https://docs.example.org/guide",
            "text/html",
            "<title>Project documentation</title><meta name='description' content='"
            "Configure a local export of your notes.'><main><p>Guide</p></main>",
            "Configure a local export",
        ),
    ],
)
def test_inspect_uses_publisher_evidence(monkeypatch, link, endpoint, kind, body, description):
    requests = []

    def fetch(url, **kwargs):
        requests.append(url)
        return body, kind, url

    monkeypatch.setattr(references, "_fetch_text", fetch)
    result = references.inspect_resource(link)

    assert requests == [endpoint]
    assert result.url == link
    assert result.evidence_url == endpoint
    assert result.status == "reviewed"
    assert description in result.summary
    assert "Publisher excerpt" in references.render_review([link], [result])


def test_inspect_limits_publisher_excerpt_and_rejects_challenge_page(monkeypatch):
    link = "https://docs.example.org/guide"
    paragraph = " ".join(["project"] * 40)
    monkeypatch.setattr(references, "_fetch_text", lambda url, **kw: (paragraph, "text/plain", url))
    result = references.inspect_resource(link)
    assert len(result.summary.split()) == 24

    paragraph = "此项目将公开的文章转换成可离线检索的笔记。" * 16
    localized = references.inspect_resource(link)
    assert 10 < len(localized.summary) <= 121

    challenge = "<title>Just a moment...</title><p>Enable JavaScript and cookies to continue.</p>"
    monkeypatch.setattr(references, "_fetch_text", lambda url, **kw: (challenge, "text/html", url))
    assert references.inspect_resource(link).status == "blocked"


def test_review_keeps_blocked_and_unread_links_with_exact_urls(monkeypatch):
    blocked = "https://docs.example.org/blocked"
    pending = "https://github.com/example/pending#docs"

    def no_access(url, **kwargs):
        raise references._Blocked("destination is outside the publisher")

    monkeypatch.setattr(references, "_fetch_text", no_access)
    result = references.inspect_resource(blocked)
    review = references.render_review([blocked, pending], [result])

    assert result.status == "blocked"
    assert f"`{blocked}`" in review and f"<{blocked}>" not in review
    assert "destination is outside the publisher" in review
    assert f"`{pending}`" in review and f"<{pending}>" not in review
    assert "Linked only" in review


def test_fetch_checks_redirect_before_contacting_other_host(monkeypatch):
    url = "https://docs.example.org/guide"
    headers = Message()
    headers["Location"] = "https://other.example.org/guide"
    requests = []

    def connect(host, timeout):
        requests.append(host)
        return SimpleNamespace(
            request=lambda *args, **kwargs: None,
            getresponse=lambda: SimpleNamespace(status=302, headers=headers),
            close=lambda: None,
        )

    monkeypatch.setattr(references, "_open_connection", connect)
    result = references.inspect_resource(url)

    assert result.status == "blocked"
    assert requests == ["docs.example.org"]


def test_fetch_pins_public_addresses_and_rejects_private_resolution(monkeypatch):
    calls = []

    def resolve(host, port, **kwargs):
        calls.append((host, port))
        return [
            (AF_INET, SOCK_STREAM, 6, "", ("93.184.215.14", 443)),
            (AF_INET, SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ]

    monkeypatch.setattr(references.socket, "getaddrinfo", resolve)
    result = references.inspect_resource("https://docs.example.org/guide")

    assert result.status == "blocked"
    assert calls == [("docs.example.org", 443)]


def test_fetch_rejects_attachment_without_reading_it(monkeypatch):
    headers = Message()
    headers["Content-Type"] = "text/plain"
    headers["Content-Disposition"] = "attachment; filename=archive.txt"

    def unexpected_read(*args):
        pytest.fail("attachment body was read")

    response = SimpleNamespace(status=200, headers=headers, read1=unexpected_read)
    monkeypatch.setattr(references, "_open_connection", lambda *args: SimpleNamespace(
        request=lambda *args, **kwargs: None,
        getresponse=lambda: response,
        close=lambda: None,
    ))

    result = references.inspect_resource("https://docs.example.org/manual")
    assert result.status == "blocked"


def test_fetch_limits_read_size_and_uses_final_public_url(monkeypatch):
    location = Message()
    location["Location"] = "/manual/v2"
    headers = Message()
    headers["Content-Type"] = "text/plain"
    body = BytesIO(b"Example Tool reads published documents into a local catalog.")
    responses = iter([
        SimpleNamespace(status=302, headers=location),
        SimpleNamespace(status=200, headers=headers, read1=body.read1),
    ])
    requests = []

    def request(method, path, *, headers):
        assert method == "GET"
        assert "Authorization" not in headers and "Cookie" not in headers
        requests.append(path)

    monkeypatch.setattr(references, "_open_connection", lambda *args: SimpleNamespace(
        request=request,
        getresponse=lambda: next(responses),
        close=lambda: None,
    ))
    result = references.inspect_resource("https://docs.example.org/manual")

    assert result.status == "reviewed"
    assert result.evidence_url == "https://docs.example.org/manual/v2"
    assert requests == ["/manual", "/manual/v2"]
