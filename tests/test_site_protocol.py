"""
公共用例通过启动注入的 Module 能力解释站点协议，保留旧配置、链接和插件入口。
"""

import base64
import hashlib
import hmac
import json
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

import app.application.rss as rss_module
import app.modules.indexer as indexer
import app.modules.indexer.download as download_protocol
from app.application.rss import RssHelper, _require_rss_site_port, configure_rss_ports
from app.application.search.session import torrent_snapshot
from app.application.site.protocol import (
    SiteConnectionResult,
    get_site_protocol_port,
    is_expiring_download_url,
    site_domain,
    torrent_resource_identity,
)
from app.domain import site as site_rules
from app.domain import torrent as torrent_rules
from app.domain.context import TorrentInfo
from app.sdk.security import nexusphp_encrypt
from app.sdk.string import StringUtils
from app.startup.composition import site as composition


@pytest.fixture
def protocol_boundary(monkeypatch):
    """
    用真实调度器执行索引模块，替换目录边界并禁止访问插件目录。
    """
    module = object.__new__(indexer.IndexerModule)
    calls = []

    def running(method):
        """
        记录内部能力分发并只提供当前索引模块。
        """
        calls.append(method)
        return [module]

    monkeypatch.setattr(composition._SiteModuleCatalog, "get_running_modules", staticmethod(running))
    plugins = Mock(side_effect=AssertionError("站点内部能力不能访问插件目录"))
    monkeypatch.setattr(composition._SiteModuleCatalog, "get_plugin_modules", plugins)
    composition.configure_site_protocol_composition()
    for name in ("_rss_http_port", "_rss_browser_port", "_rss_parser_port"):
        monkeypatch.setattr(rss_module, name, getattr(rss_module, name))
    return SimpleNamespace(module=module, calls=calls, plugins=plugins)


@pytest.mark.parametrize("url,expected", [
    (None, ""), ("", ""), ("https://tracker.example.com/rss", "example.com"),
    ("https://u2.dmhy.org/torrents.php", "u2.dmhy.org"),
    ("https://www.U2.dmhy.org:8443/", "u2.dmhy.org"), (".u2.dmhy.org", "u2.dmhy.org"),
    ("https://pt.ecust.pp.ua/", "pt.ecust.pp.ua"), (".pt.gtkpw.xyz", "pt.gtkpw.xyz"),
    ("https://pt.gtk.pw/", "pt.gtk.pw"),
])
def test_domain_exceptions_are_dispatched_only_by_module(protocol_boundary, url, expected):
    """
    多级域名例外和普通域名都通过 Module 运行，插件和宿主消费相同结果。
    """
    assert site_domain(url) == expected
    assert StringUtils.get_url_domain(url) == expected
    assert protocol_boundary.calls == ["get_site_domain", "get_site_domain"]
    protocol_boundary.plugins.assert_not_called()


def test_domain_matching_is_host_scoped_and_domain_stays_pure(protocol_boundary):
    """
    站点名出现在查询串或其他主机中不能命中例外，Domain 不加载模块规则。
    """
    assert site_rules.extract_domain("https://u2.dmhy.org/") == "dmhy.org"
    url = "https://other.example/?redirect=https://u2.dmhy.org/"
    assert site_domain(url) == "other.example"
    assert site_domain("https://u2.dmhy.org.evil.test/") != "u2.dmhy.org"


def test_legacy_site_and_cookiecloud_use_module_domain_rules(protocol_boundary):
    """
    精确 Compat 路由保留旧插件域名函数和无参 CookieCloud 的归组行为。
    """
    legacy_site = import_module("app.utils.site")
    legacy_cookiecloud = import_module("app.helper.cookiecloud")
    assert legacy_site.extract_domain("https://u2.dmhy.org/") == "u2.dmhy.org"
    helper = legacy_cookiecloud.CookieCloudHelper()
    assert helper._domain_resolver(".u2.dmhy.org") == "u2.dmhy.org"


@pytest.mark.parametrize("page_url,enclosure,expected", [
    ("https://totheglory.im/t/17/", "https://totheglory.im/dl/17/old", ("7", "id=17")),
    ("https://totheglory.im/t/17/", "https://totheglory.im/download?id=23", ("7", "id=17")),
    ("https://kp.m-team.cc/detail/17", "https://tracker.example/download?id=23", ("7", "id=17")),
    ("https://tracker.example/detail/17", "[old]https://tracker.example/api", ("7", "id=17")),
    ("https://tracker.example/torrents.php?id=12&torrentid=17", None, ("7", "id=17")),
    ("https://tracker.example/t/17/", "https://tracker.example/dl/17/old", None),
    (None, "https://[invalid", None),
])
def test_module_identity_preserves_url_precedence_and_legacy_paths(protocol_boundary, page_url, enclosure, expected):
    """
    详情页身份优先于下载地址，迁移不能把临时票据或同形的其他站点路径当成 ID。
    """
    torrent = TorrentInfo(site=7, page_url=page_url, enclosure=enclosure)
    assert torrent_resource_identity(torrent) == expected
    assert torrent.page_url == page_url and torrent.enclosure == enclosure


def test_old_checkpoint_restoration_does_not_persist_tickets(protocol_boundary):
    """
    旧 TTG 检查点按公开详情恢复身份，没有下载地址时继续交给原页重取。
    """
    torrent = TorrentInfo(site=7, page_url="https://totheglory.im/t/17/", enclosure="https://totheglory.im/dl/17/old")
    record = torrent_snapshot(torrent)
    assert record["enclosure"] is None
    restored = protocol_boundary.module.restore_search_torrent(
        {"id": 7, "cookie": "current-cookie"}, record, allow_missing=True,
    )
    assert restored is not None and restored.enclosure is None
    assert torrent_resource_identity(restored) == ("7", "id=17")
    assert restored.site_cookie == "current-cookie"
    assert "current-cookie" not in json.dumps(record)


def test_yema_checkpoint_keeps_existing_page_recovery(protocol_boundary):
    """
    旧 Yema fragment 和换票配方不进入检查点，迁移不假定可以按缺失的 ID 重建下载。
    """
    record = torrent_snapshot(TorrentInfo(site=7, page_url="https://www.yemapt.org/#/torrent/detail/17/",
                                         enclosure="[old-credentials]https://www.yemapt.org/openApi/download"))
    restored = protocol_boundary.module.restore_search_torrent({"parser": "Yema", "apikey": "current"}, record,
                                                               allow_missing=True)
    assert restored is not None and restored.enclosure is None
    assert torrent_resource_identity(restored) is None
    assert "old-credentials" not in json.dumps(record)


@pytest.mark.parametrize("url,expected", [
    ("https://totheglory.im/dl/17/old", True), ("https://www.totheglory.im/dl/17/old/", True),
    ("https://tracker.example/download?id=17&t=123&sign=abc", True),
    ("https://tracker.example/dl/17/old", False), ("https://totheglory.im/rssdd.php?par=encoded", False),
    ("https://totheglory.im/dl/17/", False), (None, False),
])
def test_module_signature_rules_preserve_plain_and_indirect_links(protocol_boundary, url, expected):
    """
    专站签名解释通过 Module 分发，Domain 只保留通用查询签名。
    """
    assert is_expiring_download_url(url) is expected
    if url == "https://totheglory.im/dl/17/old":
        assert torrent_rules.is_expiring_download_url(url) is False


@pytest.mark.parametrize("domain,path,xpath,params", [
    ("hares.top", "getrss.php", "//*[@id='layui-layer100001']/div[2]/div/p[4]/a/@href", {"showrows": 50}),
    ("et8.org", "getrss.php", "//*[@id='outer']/table/tbody/tr/td/table/tbody/tr/td/a[2]/@href", {"showrows": 50}),
    ("pttime.org", "getrss.php", "//*[@id='outer']/table/tbody/tr/td/table/tbody/tr/td/text()[5]", {"showrows": 10}),
    ("ourbits.club", "getrss.php", "//a[@class='gen_rsslink']/@href", {"search_mode": 1}),
    ("totheglory.im", "rsstools.php?c51=51", "//textarea/text()", {"showrows": 50}),
    ("monikadesign.uk", "rss", "//a/@href", {"showrows": 50}),
    ("zhuque.in", "user/rss", "//a/@href", {"showrows": 50}),
    ("hdwing.org", "getrss.php", None, {"rsscart": 0}),
    ("audiences.me", "getrss.php", None, {"torrent_type": 1, "exp": 180}),
    ("shadowflow.org", "getrss.php", None, {"paid": 0, "search_mode": 0, "showrows": 30}),
    ("hddolby.com", "getrss.php", None, {"exp": 180}),
    ("hdhome.org", "getrss.php", None, {"exp": 180}),
    ("pthome.net", "getrss.php", None, {"exp": 180}),
    ("ptsbao.club", "getrss.php", None, {"size": 0}),
    ("leaves.red", "getrss.php", None, {"search_mode": 0, "paid": 2}),
    ("hdtime.org", "getrss.php", None, {"search_mode": 0}),
    ("m-team.io", "getrss.php", None, {"https": 1}),
    ("u2.dmhy.org", "getrss.php", None, {"inclautochecked": 1, "trackerssl": 1}),
])
def test_all_rss_site_recipes_are_dispatched_and_isolated(protocol_boundary, domain, path, xpath, params):
    """
    原有十八个专站 RSS 配方迁移后保留页面、选择器、参数和浏览器选择。
    """
    port = _require_rss_site_port()
    recipe = port.get_site_rss_recipe(f"https://{domain}/")
    assert recipe.url.startswith(f"https://{domain}/{path}")
    assert recipe.xpath == (xpath or "//a[@class='faqlink']/@href")
    assert all(recipe.params[key] == value for key, value in params.items())
    assert recipe.render is (domain == "zhuque.in")
    recipe.params["local-change"] = "not-shared"
    assert "local-change" not in port.get_site_rss_recipe(f"https://{domain}/").params
    assert protocol_boundary.calls == ["get_site_rss_recipe", "get_site_rss_recipe"]


@pytest.mark.parametrize("text,expected", [
    ("RSS 链接已过期, 您需要获得一个新的!", None),
    ("RSS Link has expired, You need to get a new one!", None),
    ("RSS Link has expired, You need to get new!", None),
    ("ordinary error", False),
])
def test_rss_expiration_is_interpreted_by_module(protocol_boundary, text, expected):
    """
    Application 只消费 Module 的过期事实，普通内容错误不触发 RSS 续期。
    """
    http = Mock(get=Mock(return_value=SimpleNamespace(status_code=200, text=text, content=b"")))
    configure_rss_ports(http=http, browser=Mock(), parser=Mock())
    assert RssHelper().parse("https://tracker.example/rss") is expected
    assert "is_site_rss_expired" in protocol_boundary.calls


def test_encryption_sdk_keeps_original_site_payload(protocol_boundary, monkeypatch):
    """
    旧 SDK 加密入口经过 Module 后仍保留随机 IV、MAC、字段和多字节文本口径。
    """
    monkeypatch.setattr(download_protocol.os, "urandom", lambda _length: b"x" * 16)
    key = b"k" * 32
    payload = json.loads(base64.b64decode(nexusphp_encrypt("中文协议", key)))
    assert payload.keys() == {"iv", "value", "mac", "tag"}
    assert payload["tag"] == ""
    assert payload["mac"] == hmac.new(key, (payload["iv"] + payload["value"]).encode(), hashlib.sha256).hexdigest()
    cipher = AES.new(key, AES.MODE_CBC, base64.b64decode(payload["iv"]))
    assert unpad(cipher.decrypt(base64.b64decode(payload["value"])), AES.block_size).decode() == "中文协议"
    assert protocol_boundary.calls == ["encrypt_site_payload"]


def test_unavailable_module_is_not_a_false_protocol_fact(protocol_boundary, monkeypatch):
    """
    Module 缺失不能被布尔转换成普通链接或无过期响应，从而绕过下载安全策略。
    """
    monkeypatch.setattr(composition._SiteModuleCatalog, "get_running_modules", staticmethod(lambda _method: []))
    with pytest.raises(RuntimeError, match="Module 不可用"):
        site_domain("https://u2.dmhy.org/")
    with pytest.raises(RuntimeError, match="Module 不可用"):
        is_expiring_download_url("https://totheglory.im/dl/17/old")
    with pytest.raises(RuntimeError, match="Module 不可用"):
        get_site_protocol_port().is_torrent_download_confirmation(b"page")
    with pytest.raises(RuntimeError, match="Module 不可用"):
        get_site_protocol_port().has_site_subtitle_api({"parser": "mTorrent"})
    with pytest.raises(RuntimeError, match="Module 不可用"):
        _require_rss_site_port().is_site_rss_expired("expired")
    with pytest.raises(RuntimeError, match="Module 不可用"):
        _require_rss_site_port().get_site_rss_recipe("https://u2.dmhy.org/")


def test_connection_unsupported_is_an_explicit_receipt(protocol_boundary, monkeypatch):
    """
    不支持检测与已知适配器能力缺失有不同回执，公共层不需要复制站点路由。
    """
    assert protocol_boundary.module.test_site({"domain": "https://ordinary.example/"}) == SiteConnectionResult(supported=False)
    monkeypatch.delitem(indexer.SPIDER_PARSER_CLASSES, "Piggo")
    receipt = protocol_boundary.module.test_site({"domain": "https://piggo.me/", "schema": "NexusPhp"})
    assert receipt.supported and not receipt.success
    assert receipt.message == "站点适配器未提供连接检测能力"


@pytest.mark.parametrize("resource", [{"schema": "MTorrent"}, {"parser": "mTorrent"},
                                     {"schema": "TNode"}, {"parser": "TNodeSpider"}])
def test_missing_declared_protocol_is_not_generic_connectivity(protocol_boundary, monkeypatch, resource):
    """
    兼容站已经声明认证协议时，缺失该适配器也不能回退成网页成功。
    """
    for parser in ("mTorrent", "TNodeSpider"):
        monkeypatch.delitem(indexer.SPIDER_PARSER_CLASSES, parser)
    receipt = protocol_boundary.module.test_site({"domain": "https://mirror.example/", **resource})
    assert receipt.supported and not receipt.success
    assert receipt.message == "站点适配器未提供连接检测能力"
