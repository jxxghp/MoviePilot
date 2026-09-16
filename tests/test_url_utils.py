import pytest

from app.foundation.url import UrlUtils


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
