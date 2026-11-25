"""
验证RSS 地址校验、访问安全边界与失败日志的凭据遮盖。
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app.chain.torrents as torrents_module
from app.application import rss as rss_module
from app.application.rss import RssHelper, configure_rss_ports, reset_rss_ports
from app.chain.torrents import TorrentsChain


@pytest.fixture
def rss_ports():
    """
    装配可记录调用的 RSS 端口，确保用例不产生真实网络请求。
    """
    http_port = Mock()
    parser_port = Mock()
    parser_port.parse.return_value = None
    configure_rss_ports(http=http_port, browser=Mock(), parser=parser_port)
    yield http_port
    reset_rss_ports()


@pytest.mark.parametrize(
    "url",
    [None, "", "   ", 123, b"https://example.com/rss", "#", "#fragment",
     "/rss", "rss.xml", "ftp://example.com/rss", "https://bad host/rss",
     "https://example.com:abc/rss", "https://example.com:65536/rss"],
)
def test_rss_helper_rejects_invalid_urls_before_http_request(url, rss_ports):
    """
    无效 RSS 地址应返回普通错误，并在端口层之前被拒绝。
    """
    assert RssHelper().parse(url) is False
    rss_ports.get.assert_not_called()


def test_rss_helper_strips_whitespace_and_allows_http_fragment(rss_ports):
    """
    合法 HTTP(S) 地址即使带 fragment 也应去除外层空白后请求。
    """
    rss_ports.get.return_value = SimpleNamespace(
        status_code=200,
        content=b"<rss />",
        text="<rss />",
        reason="OK",
    )
    rss_ports.decode_xml.return_value = "<rss />"

    assert RssHelper().parse("  http://example.com:8080/rss#item\n") == []
    assert rss_ports.get.call_args.kwargs["url"] == "http://example.com:8080/rss#item"


@pytest.mark.parametrize("parse_result, renew_expected", [(False, False), (None, True)])
def test_torrents_rss_preserves_false_and_none_contract(
    monkeypatch, parse_result, renew_expected, rss_ports
):
    """
    RSS 普通错误与过期结果应继续分别对应不续期和自动续期。
    """
    site = {
        "id": 1,
        "name": "测试站点",
        "rss": "https://example.com/rss",
        "proxy": False,
        "timeout": 30,
        "ua": None,
    }
    sites_helper = Mock()
    sites_helper.get_indexer.return_value = site
    monkeypatch.setattr(torrents_module, "SitesHelper", lambda: sites_helper)
    monkeypatch.setattr(RssHelper, "parse", lambda *_args, **_kwargs: parse_result)

    chain = TorrentsChain()
    renew = Mock()
    monkeypatch.setattr(chain, "_TorrentsChain__renew_rss_url", renew)

    assert chain.rss("example.com") == []
    assert renew.called is renew_expected


def test_torrents_rss_warns_once_and_skips_invalid_site_url(monkeypatch, rss_ports):
    """
    站点 RSS 配置无效时只记录简短告警，不请求或自动续期。
    """
    site = {
        "id": 1,
        "name": "测试站点",
        "rss": "not-a-url?passkey=secret",
        "proxy": False,
        "timeout": 30,
        "ua": None,
    }
    sites_helper = Mock()
    sites_helper.get_indexer.return_value = site
    warnings = []
    errors = []
    monkeypatch.setattr(torrents_module, "SitesHelper", lambda: sites_helper)
    monkeypatch.setattr(torrents_module.logger, "warning", warnings.append)
    monkeypatch.setattr(torrents_module.logger, "error", errors.append)

    chain = TorrentsChain()
    renew = Mock()
    monkeypatch.setattr(chain, "_TorrentsChain__renew_rss_url", renew)

    assert chain.rss("example.com") == []
    assert rss_ports.get.call_count == 0
    assert errors == []
    assert warnings == ["站点 example.com RSS地址无效，跳过获取"]
    assert "passkey=secret" not in warnings[0]
    renew.assert_not_called()


@pytest.fixture
def rss_error_logs(monkeypatch):
    """
    收集 RSS 失败日志，避免用例输出真实错误日志。

    :return: 收集到的 error 日志文本列表
    """
    errors: list[str] = []
    monkeypatch.setattr(rss_module.logger, "error", errors.append)
    return errors


@pytest.mark.parametrize(
    ("response", "expected_fragment"),
    [
        (None, "获取RSS失败：请求返回空值"),
        (SimpleNamespace(status_code=503, content=b"", text="", reason="Service Unavailable"),
         "RSS请求失败，状态码: 503"),
    ],
)
def test_rss_helper_error_logs_mask_query_passkey(rss_ports, rss_error_logs, response, expected_fragment):
    """
    RSS 请求空响应与非 200 响应时，错误日志保留站点定位信息并遮盖 passkey。
    """
    rss_ports.get.return_value = response

    assert RssHelper().parse(
        "https://example.com/torrentrss.php?id=1&linktype=1&passkey=SUPERSECRET"
    ) is False
    assert len(rss_error_logs) == 1
    message = rss_error_logs[0]
    assert expected_fragment in message
    assert "SUPERSECRET" not in message
    assert "passkey=<REDACTED>" in message
    assert "id=1" in message


def test_rss_helper_exception_log_masks_url_credentials(rss_ports, rss_error_logs):
    """
    请求异常文本自带 RSS 相对地址时，错误日志同样不输出 passkey 原文。
    """
    rss_ports.get.side_effect = RuntimeError(
        "Max retries exceeded with url: /torrentrss.php?passkey=SUPERSECRET"
    )

    assert RssHelper().parse("https://example.com/torrentrss.php?passkey=SUPERSECRET") is False
    assert len(rss_error_logs) == 1
    assert "SUPERSECRET" not in rss_error_logs[0]
    assert "passkey=<REDACTED>" in rss_error_logs[0]
