"""
验证URL 解析、拼接、规范化和凭据脱敏规则。
"""

import pytest

from app.foundation.url import UrlUtils, mask_url_credentials, url_matches_trusted_hosts


@pytest.mark.parametrize(
    "url",
    [None, 123, b"https://example.com/rss", "", "   ", "#", "#fragment",
     "/rss", "example.com/rss", "//example.com/rss", "ftp://example.com/rss",
     "https:///rss", "https://", "https://bad host/rss", "https://example.com/a\nb",
     "https://example.com:abc/rss", "https://example.com:65536/rss",
     "https://example.com:-1/rss", "http://[::1/rss"],
)
def test_normalize_http_url_rejects_invalid_input(url):
    """
    严格 HTTP 地址归一化拒绝缺少协议或其他不合法输入。
    """
    assert UrlUtils.normalize_http_url(url) is None


@pytest.mark.parametrize(
    "url",
    ["http://example.com", "https://example.com/rss", "HTTPS://Example.com/Rss",
     "http://localhost:0/rss", "https://example.com:65535/rss",
     "http://[::1]:8080/rss", "https://example.com/a%20b?key=a%2Fb&x=1&x=2#item"],
)
def test_normalize_http_url_preserves_address_except_outer_whitespace(url):
    """
    有效 HTTP 地址只移除首尾空白，保留地址本身的内容。
    """
    assert UrlUtils.normalize_http_url(f" \t{url}\r\n") == url
    assert UrlUtils.normalize_http_url(url) == url


def test_standardize_base_url_keeps_permissive_contract():
    """
    基础地址补全保留宽松契约，与严格 HTTP 地址校验区分。
    """
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
    """
    受信主机匹配只放行 HTTP(S) 且端口或主机名精确命中的地址。
    """
    assert url_matches_trusted_hosts(url, trusted_hosts) is expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/torrentrss.php?id=1&passkey=SECRET&isdownload=0",
         "https://example.com/torrentrss.php?id=1&passkey=<REDACTED>&isdownload=0"),
        ("https://example.com/rss?access_token=SECRET",
         "https://example.com/rss?access_token=<REDACTED>"),
        ("https://example.com/rss?X-Plex-Token=SECRET&sort=title",
         "https://example.com/rss?X-Plex-Token=<REDACTED>&sort=title"),
        ("https://example.com/rss?key=a%2Fb&uid=42&sign=deadbeef",
         "https://example.com/rss?key=<REDACTED>&uid=<REDACTED>&sign=<REDACTED>"),
        ("https://example.com/rss#item?passkey=SECRET",
         "https://example.com/rss#item?passkey=<REDACTED>"),
    ],
)
def test_mask_url_credentials_redacts_query_credentials(url, expected):
    """
    查询串凭据只遮盖参数值，保留主机、路径、参数名和非凭据参数。
    """
    assert mask_url_credentials(url) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://admin:MyPass@192.168.1.5:5000/RPC2",
         "http://admin:<REDACTED>@192.168.1.5:5000/RPC2"),
        ("redis://:p@ss@10.0.0.1:6379/0", "redis://:<REDACTED>@10.0.0.1:6379/0"),
        ("postgres://mp:TopSecret@127.0.0.1:5432/moviepilot",
         "postgres://mp:<REDACTED>@127.0.0.1:5432/moviepilot"),
    ],
)
def test_mask_url_credentials_redacts_userinfo_password(url, expected):
    """
    连接串 userinfo 口令被遮盖，用户名、主机和端口保持可读。
    """
    assert mask_url_credentials(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://192.168.1.5:32400/library",
        "http://user@host:8080/p",
        "https://example.com/a?b=1&monkey=2&turkey=3",
        "https://example.com/t/p/w500/x.jpg",
        "已遮盖 https://example.com/rss?passkey=<REDACTED>",
    ],
)
def test_mask_url_credentials_keeps_non_credential_text(url):
    """
    端口、无口令 userinfo 和非凭据参数都不被改写，重复遮盖保持幂等。
    """
    assert mask_url_credentials(url) == url
    assert mask_url_credentials(mask_url_credentials(url)) == mask_url_credentials(url)


def test_mask_url_credentials_applies_to_free_text_with_url():
    """
    异常文本中的相对路径查询串同样被遮盖，便于直接用于日志脱敏。
    """
    text = "Max retries exceeded with url: /torrentrss.php?passkey=SECRET (Caused by NewConnectionError)"
    masked = mask_url_credentials(text)
    assert "SECRET" not in masked
    assert "passkey=<REDACTED>" in masked
    assert masked.startswith("Max retries exceeded with url: /torrentrss.php?")
