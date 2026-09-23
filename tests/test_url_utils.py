import pytest

from app.foundation.url import UrlUtils, url_matches_trusted_hosts


@pytest.mark.parametrize(
    "url",
    [None, 123, b"https://example.com/rss", "", "   ", "#", "#fragment",
     "/rss", "example.com/rss", "//example.com/rss", "ftp://example.com/rss",
     "https:///rss", "https://", "https://bad host/rss", "https://example.com/a\nb",
     "https://example.com:abc/rss", "https://example.com:65536/rss",
     "https://example.com:-1/rss", "http://[::1/rss"],
)
def test_normalize_http_url_rejects_invalid_input(url):
    assert UrlUtils.normalize_http_url(url) is None


@pytest.mark.parametrize(
    "url",
    ["http://example.com", "https://example.com/rss", "HTTPS://Example.com/Rss",
     "http://localhost:0/rss", "https://example.com:65535/rss",
     "http://[::1]:8080/rss", "https://example.com/a%20b?key=a%2Fb&x=1&x=2#item"],
)
def test_normalize_http_url_preserves_address_except_outer_whitespace(url):
    assert UrlUtils.normalize_http_url(f" \t{url}\r\n") == url
    assert UrlUtils.normalize_http_url(url) == url


def test_standardize_base_url_keeps_permissive_contract():
    assert UrlUtils.standardize_base_url("example.com") == "http://example.com/"
    assert UrlUtils.normalize_http_url("example.com") is None


@pytest.mark.parametrize(
    ("url", "trusted_hosts", "expected"),
    [
        ("https://Media.Example:8096/image", {"media.example:8096"}, True),
        ("http://media.example:8096/image", {"MEDIA.EXAMPLE"}, True),
        ("http://media.example:8096/image", {"media.example:8097"}, False),
        ("ftp://media.example/image", {"media.example"}, False),
        ("http://[::1/image", {"::1"}, False),
    ],
)
def test_url_matches_trusted_hosts_requires_exact_http_host(url, trusted_hosts, expected):
    """受信主机匹配只放行 HTTP(S) 且端口或主机名精确命中的地址。"""
    assert url_matches_trusted_hosts(url, trusted_hosts) is expected
