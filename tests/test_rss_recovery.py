"""
站点 RSS 补全、续期验证、并发保护和失败退避的离线回归。
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event as ThreadEvent
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qsl, urlsplit

import pytest
from requests import Response

from app.application import rss as rss_module
from app.application.rss import RssHelper, ensure_site_rss
from app.application.site.contract import SiteRepository, SiteSnapshot, SiteWriteResult
from app.chain import site as site_module
from app.chain import torrents as torrents_module
from app.chain.site import SiteChain
from app.chain.torrents import TorrentsChain
from app.domain import site as site_rules
from app.modules.indexer.rss import get_rss_recipe, is_rss_expired

OLD_URL = "https://rss.example/rss?passkey=FAKEOLD&cat=1"
NEW_URL = "https://rss.example/rss?passkey=FAKENEW&cat=all"


def _site(**changes) -> SiteSnapshot:
    """
    构造已保存且启用的私有站点，所有凭据均为虚构文本。
    """
    values = dict(id=9001, name="离线RSS", domain="rss.example", url="https://rss.example/",
                  rss=OLD_URL, cookie="FAKECOOKIE", ua="RSS-Test", is_active=True, public=False)
    return SiteSnapshot(**(values | changes))


def _storage(site: SiteSnapshot) -> SimpleNamespace:
    """
    隔离仓储边界，保留配置快照比较和提交后读取的真实调用顺序。
    """
    current = [site]
    repository = Mock(spec=SiteRepository)
    repository.get_by_domain.side_effect = lambda _domain: current[0]

    def save(expected, url):
        """
        模拟单条带条件更新；生产 SQL 的原子行为另由真实数据库测试覆盖。
        """
        if current[0] != expected:
            return SiteWriteResult(False, "站点配置已变化")
        current[0] = replace(expected, rss=url)
        return SiteWriteResult(True)

    repository.update_rss_if_unchanged.side_effect = save
    helper = Mock(spec=RssHelper)
    helper.get_rss_link.return_value = (NEW_URL, "")
    helper.parse.return_value = [{"title": "Offline torrent", "enclosure": "https://rss.example/dl/1"}]
    return SimpleNamespace(current=current, repository=repository, helper=helper)


@pytest.fixture
def recovery(monkeypatch):
    """
    每个用例拥有独立退避状态和虚拟时钟，不污染宿主共享配置。
    """
    clock = SimpleNamespace(now=0.0)
    owner = rss_module._RssLinkRecovery(clock=lambda: clock.now)
    monkeypatch.setattr(rss_module, "_rss_link_recovery", owner)
    return SimpleNamespace(clock=clock, owner=owner)


@pytest.fixture
def rss_access(monkeypatch):
    """
    保留真实 RSS 解析和配方，替换 HTTP、浏览器及可选原生解析出口。
    """
    http = Mock(spec=rss_module.RssHttpPort)
    http.decode_xml.side_effect = lambda response, **_kwargs: response.text
    parser = Mock(spec=rss_module.RssParserPort)
    parser.parse.return_value = None
    monkeypatch.setattr(rss_module, "_rss_http_port", http)
    monkeypatch.setattr(rss_module, "_rss_browser_port", Mock(spec=rss_module.RssBrowserPort))
    monkeypatch.setattr(rss_module, "_rss_parser_port", parser)
    monkeypatch.setattr(rss_module, "_rss_site_port", SimpleNamespace(
        get_site_rss_recipe=get_rss_recipe, is_site_rss_expired=is_rss_expired,
    ))
    return http


def _response(status: int, text: str) -> Response:
    """
    使用真实 requests 响应的真假语义回放 HTTP 错误，不发起网络请求。
    """
    response = Response()
    response.status_code = status
    response.encoding = "utf-8"
    response._content = text.encode("utf-8")
    return response


@pytest.mark.parametrize("old_query,new_query,expected", [
    ("passkey=OLD&cat=1", "passkey=NEW", [("cat", "1"), ("passkey", "NEW")]),
    ("cat=1&passkey=OLD-TAIL", "passkey=NEW", [("cat", "1"), ("passkey", "NEW")]),
    ("cat=1&passkey=OLD", "passkey=NEW-TAIL_1%2B2%2F3%3D", [("cat", "1"), ("passkey", "NEW-TAIL_1+2/3=")]),
    ("cat=1&cat=2&search=&passkey=OLD", "cat=all&passkey=NEW&rows=50",
     [("cat", "1"), ("cat", "2"), ("search", ""), ("passkey", "NEW"), ("rows", "50")]),
    ("par=OLD&cat=1", "par=NEW-opaque", [("cat", "1"), ("par", "NEW-opaque")]),
    ("uid=1&passkey=OLD&cat=1", "uid=2&passkey=NEW", [("cat", "1"), ("uid", "2"), ("passkey", "NEW")]),
    ("passkey=OLD&signature=OLD&expires=1", "passkey=NEW&signature=NEW&expires=2",
     [("passkey", "NEW"), ("signature", "NEW"), ("expires", "2")]),
    ("passkey=OLD&t=movie&exp=90", "passkey=NEW", [("t", "movie"), ("exp", "90"), ("passkey", "NEW")]),
])
def test_renew_preserves_filters_and_whole_credentials(old_query, new_query, expected):
    """
    更新完整认证值，保留重复分类、空筛选与未被生成页更新的有效期配置。
    """
    result = site_rules.renew_rss_link(f"https://rss.example/rss?{old_query}", f"https://rss.example/rss?{new_query}")
    assert parse_qsl(urlsplit(result).query, keep_blank_values=True) == expected


def test_renew_accepts_new_domain_and_path_without_losing_filters():
    """
    站点迁移域名、协议或票据路径后采用新入口，同时保留可表达的筛选参数。
    """
    result = site_rules.renew_rss_link(OLD_URL, "https://new.example/feed/FAKENEW?passkey=NEW")
    assert result == "https://new.example/feed/FAKENEW?cat=1&passkey=NEW"


@pytest.mark.parametrize("status,body", [
    (200, "RSS Link has expired, You need to get a new one!"),
    (403, "RSS Link has expired, You need to get a new one!"),
    (401, "RSS 链接已过期，您需要获得一个新的！"),
    (200, "<html><head><title>Error</title></head><body><p>RSS Link has expired,<br>You need to get a new one!</p></body></html>"),
    (200, "RSS Link has expired,\nYou need to get a new one!"),
    (200, "  rss link has expired, you need to get new!  "),
])
def test_explicit_expiration_survives_http_and_html_forms(rss_access, status, body):
    """
    完整过期提示的状态码、HTML 包装、换行和标点变化不会漏掉续期。
    """
    rss_access.get.return_value = _response(status, body)
    assert RssHelper().parse(OLD_URL) is None


@pytest.mark.parametrize("status,body", [
    (429, "Too many requests"), (503, "RSS is temporarily unavailable"),
    (200, "<html><body>Please log in</body></html>"),
    (200, "<error>Invalid passkey</error>"),
])
def test_transient_errors_and_login_pages_do_not_trigger_renewal(rss_access, status, body):
    """
    普通错误和登录页面不是凭据过期，也不能伪装成一个合法空订阅。
    """
    rss_access.get.return_value = _response(status, body)
    assert RssHelper().parse(OLD_URL) is False


@pytest.mark.parametrize("body", [
    "<rss><channel /></rss>",
    '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" />',
    '<!-- 前置说明 --><rss><channel><title>RSS Link has expired, You need to get a new one!</title></channel></rss>',
])
def test_real_empty_feed_is_success_even_with_expiration_text(rss_access, body):
    """
    合法空订阅正常返回空列表，标题或内容提到过期也不会误触发续期。
    """
    rss_access.get.return_value = _response(200, body)
    assert RssHelper().parse(OLD_URL) == []


def test_oversized_rss_is_rejected_before_document_parsing(rss_access, monkeypatch):
    """
    超过大小限制的响应在 XML 和原生解析前失败，避免新增校验绕过原有内存保护。
    """
    monkeypatch.setattr(RssHelper, "MAX_RSS_SIZE", 8)
    rss_access.get.return_value = _response(200, "<rss><channel /></rss>")
    assert RssHelper().parse(OLD_URL) is False
    rss_access.decode_xml.assert_not_called()


@pytest.mark.parametrize("href,expected", [
    ("https://rss.example/rss?passkey=FAKE", "https://rss.example/rss?passkey=FAKE"),
    ("/rss?passkey=FAKE", "https://rss.example/rss?passkey=FAKE"),
    ("javascript:void(0)", ""), ("#", ""), ("ftp://rss.example/feed", ""), ("http://[bad/rss", ""),
])
def test_generated_links_are_normalized_and_validated(rss_access, href, expected):
    """
    相对链接会补全，脚本、空锚点和非 HTTP 地址不会作为成功结果保存。
    """
    rss_access.post.return_value = _response(200, f'<a class="faqlink" href="{href}">RSS</a>')
    link, message = RssHelper().get_rss_link("https://rss.example/", "FAKECOOKIE", "audit")
    assert link == expected
    assert bool(message) is not bool(expected)


def test_broad_link_selector_ignores_navigation_after_feed(rss_access):
    """
    专站宽选择器中 RSS 后面出现退出链接时仍选择订阅地址。
    """
    rss_access.post.return_value = _response(200, '<a href="/rss?token=FAKE">RSS</a><a href="/logout">退出</a>')
    assert RssHelper().get_rss_link("https://monikadesign.uk/", "FAKECOOKIE", "audit")[0] == "https://monikadesign.uk/rss?token=FAKE"


def test_generation_exception_redacts_cookie_and_query_credentials(rss_access):
    """
    生成页传输异常即使含原 Cookie 和链接凭据，对外提示也必须脱敏。
    """
    rss_access.post.side_effect = RuntimeError("FAKECOOKIE /rss?par=FAKESECRET&signature=FAKESIGN")
    link, message = RssHelper().get_rss_link("https://rss.example/", "FAKECOOKIE", "audit")
    assert not link
    assert all(secret not in message for secret in ("FAKECOOKIE", "FAKESECRET", "FAKESIGN"))


def test_generation_respects_browser_configuration_and_timeout(rss_access):
    """
    启用渲染时通过已注入的浏览器入口生成，并沿用站点 Cookie、UA 和超时。
    """
    browser = rss_module._rss_browser_port
    browser.render.return_value = '<a class="faqlink" href="/rss?passkey=FAKE">RSS</a>'
    link, _message = RssHelper().get_rss_link("https://rss.example/", "FAKECOOKIE", "audit", timeout=27, render=True)
    assert link == "https://rss.example/rss?passkey=FAKE"
    assert browser.render.call_args.kwargs == dict(url="https://rss.example/getrss.php", cookie="FAKECOOKIE",
                                                  ua="audit", proxies=None, timeout=27)
    rss_access.post.assert_not_called()


def test_recovery_validates_before_save_and_returns_same_round_items(recovery):
    """
    续期先验证实际订阅，保存后把同一份资源交给本轮，不再重复抓取。
    """
    storage = _storage(_site())
    sequence = []
    storage.helper.parse.side_effect = lambda *_args, **_kwargs: sequence.append("validate") or []
    save = storage.repository.update_rss_if_unchanged.side_effect

    def observed_save(*args):
        """
        记录验证与保存的顺序，并执行配置比较。
        """
        sequence.append("save")
        return save(*args)

    storage.repository.update_rss_if_unchanged.side_effect = observed_save
    result = ensure_site_rss("rss.example", storage.repository, expired_url=OLD_URL, helper=storage.helper)
    assert result.url == "https://rss.example/rss?cat=1&passkey=FAKENEW"
    assert result.items == []
    assert sequence == ["validate", "save"]
    assert storage.current[0].rss == result.url
    storage.helper.parse.assert_called_once()


@pytest.mark.parametrize("invalid_result", [None, False])
def test_invalid_new_feed_keeps_old_configuration(recovery, invalid_result):
    """
    新地址仍过期或抓取失败时不覆盖原地址，返回一次续期失败通知。
    """
    storage = _storage(_site())
    storage.helper.parse.return_value = invalid_result
    result = ensure_site_rss("rss.example", storage.repository, expired_url=OLD_URL, helper=storage.helper)
    assert result.url is None and result.notify
    assert storage.current[0].rss == OLD_URL
    storage.repository.update_rss_if_unchanged.assert_not_called()


def test_failed_write_is_reported_and_enters_backoff(recovery):
    """
    仓储拒绝保存时按失败处理，不能假装更新成功或在下一调用立即重发请求。
    """
    storage = _storage(_site())
    storage.repository.update_rss_if_unchanged.side_effect = None
    storage.repository.update_rss_if_unchanged.return_value = SiteWriteResult(False, "保存失败")
    first = ensure_site_rss("rss.example", storage.repository, expired_url=OLD_URL, helper=storage.helper)
    second = ensure_site_rss("rss.example", storage.repository, expired_url=OLD_URL, helper=storage.helper)
    assert first.url is None and first.notify and first.message == "保存失败"
    assert not second.notify
    storage.helper.get_rss_link.assert_called_once()


@pytest.mark.parametrize("stage", ["generate", "save"])
def test_recovery_exception_releases_admission_without_leaking_credentials(recovery, stage):
    """
    请求或提交异常只报告异常类型，保留旧地址、释放准入，并允许退避后重试。
    """
    storage = _storage(_site())
    boundary = storage.helper.get_rss_link if stage == "generate" else storage.repository.update_rss_if_unchanged
    successful = boundary.side_effect
    boundary.side_effect = RuntimeError("FAKECOOKIE https://rss.example/rss?passkey=FAKEOLD")
    failed = ensure_site_rss("rss.example", storage.repository, expired_url=OLD_URL, helper=storage.helper)
    assert failed.url is None and failed.notify
    assert "FAKECOOKIE" not in failed.message and "FAKEOLD" not in failed.message
    assert storage.current[0].rss == OLD_URL
    assert not recovery.owner._inflight

    boundary.side_effect = successful
    recovery.clock.now = 300
    assert ensure_site_rss("rss.example", storage.repository, expired_url=OLD_URL, helper=storage.helper).url


@pytest.mark.parametrize("snapshot", [None, RuntimeError("FAKECOOKIE")])
def test_missing_or_unreadable_site_does_not_generate(recovery, snapshot):
    """
    站点已删除或读取失败时不使用索引缓存里的旧凭据生成订阅。
    """
    storage = _storage(_site())
    if isinstance(snapshot, Exception):
        storage.repository.get_by_domain.side_effect = snapshot
    else:
        storage.current[0] = snapshot
    result = ensure_site_rss("rss.example", storage.repository, helper=storage.helper)
    assert result.url is None and "FAKECOOKIE" not in result.message
    storage.helper.get_rss_link.assert_not_called()


@pytest.mark.parametrize("changed", [None, _site(cookie="FAKENEWCOOKIE"), _site(rss=NEW_URL)])
def test_site_changed_before_generation_uses_current_configuration(recovery, changed):
    """
    获取准入前另一任务完成修改或删除时，不继续使用原快照发起生成请求。
    """
    storage = _storage(_site())
    storage.repository.get_by_domain.side_effect = [storage.current[0], changed]
    result = ensure_site_rss("rss.example", storage.repository, expired_url=OLD_URL, helper=storage.helper)
    assert result.url == (NEW_URL if changed is not None and changed.rss == NEW_URL else None)
    assert not recovery.owner._inflight
    storage.helper.get_rss_link.assert_not_called()


@pytest.mark.parametrize("changes", [dict(rss="invalid"), dict(rss=None, cookie=None)])
def test_invalid_manual_address_or_missing_cookie_keeps_configuration(recovery, changes):
    """
    手工无效地址和缺少 Cookie 都保留原配置，给出修正提示而不请求生成页。
    """
    storage = _storage(_site(**changes))
    result = ensure_site_rss("rss.example", storage.repository, helper=storage.helper)
    assert result.url is None and result.message
    storage.repository.update_rss_if_unchanged.assert_not_called()
    storage.helper.get_rss_link.assert_not_called()


def test_generator_returning_expired_address_is_not_retried_recursively(recovery):
    """
    生成页仍返回完全相同的过期地址时直接报告失败，不循环验证或写回旧地址。
    """
    unchanged = "https://rss.example/rss?passkey=FAKENEW"
    storage = _storage(_site(rss=unchanged))
    storage.helper.get_rss_link.return_value = (unchanged, "")
    result = ensure_site_rss("rss.example", storage.repository, expired_url=unchanged, helper=storage.helper)
    assert result.url is None and result.notify
    storage.helper.parse.assert_not_called()
    storage.repository.update_rss_if_unchanged.assert_not_called()


def test_retry_backoff_and_daily_notification_deduplication(recovery):
    """
    用虚拟时钟验证五分钟、十分钟退避及同配置每天最多一次过期通知。
    """
    storage = _storage(_site())
    storage.helper.get_rss_link.return_value = ("", "生成页不可用")

    def run():
        """
        通过公共入口触发同一配置的过期恢复。
        """
        return ensure_site_rss("rss.example", storage.repository, expired_url=OLD_URL, helper=storage.helper)

    assert run().notify
    recovery.clock.now = 299
    assert not run().notify
    assert storage.helper.get_rss_link.call_count == 1
    recovery.clock.now = 300
    assert not run().notify
    assert storage.helper.get_rss_link.call_count == 2
    recovery.clock.now = 899
    assert not run().notify
    assert storage.helper.get_rss_link.call_count == 2
    recovery.clock.now = 900
    assert not run().notify
    assert storage.helper.get_rss_link.call_count == 3
    recovery.clock.now = 24 * 3600
    assert run().notify
    assert storage.helper.get_rss_link.call_count == 4
    assert "FAKECOOKIE" not in repr(recovery.owner._states)
    assert "FAKEOLD" not in repr(recovery.owner._states)


def test_changed_credentials_bypass_old_failure_backoff(recovery):
    """
    修正 Cookie 后无需等待旧配置的退避，生成请求必须使用新凭据。
    """
    storage = _storage(_site())
    storage.helper.get_rss_link.return_value = ("", "旧凭据不可用")
    ensure_site_rss("rss.example", storage.repository, expired_url=OLD_URL, helper=storage.helper)
    storage.current[0] = replace(storage.current[0], cookie="FAKE-NEW-COOKIE")
    storage.helper.get_rss_link.return_value = (NEW_URL, "")
    assert ensure_site_rss("rss.example", storage.repository, expired_url=OLD_URL, helper=storage.helper).url
    assert storage.helper.get_rss_link.call_count == 2
    assert storage.helper.get_rss_link.call_args.kwargs["cookie"] == "FAKE-NEW-COOKIE"


def test_concurrent_recovery_is_coalesced_without_holding_io_lock(recovery):
    """
    同站点并发请求只生成一次；另一站点能够在首站网络阻塞时完成。
    """
    first = _storage(_site())
    other = _storage(_site(id=9002, domain="other.example", rss=None))
    entered, release = ThreadEvent(), ThreadEvent()

    def generate(**_kwargs):
        """
        暂停第一个站点的生成请求，让并发准入可确定性地被观察。
        """
        entered.set()
        assert release.wait(5)
        return NEW_URL, ""

    first.helper.get_rss_link.side_effect = generate
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(ensure_site_rss, "rss.example", first.repository,
                                  expired_url=OLD_URL, helper=first.helper)
        try:
            assert entered.wait(5)
            deferred = ensure_site_rss("rss.example", first.repository, expired_url=OLD_URL, helper=first.helper)
            assert deferred.url is None and not deferred.notify
            assert ensure_site_rss("other.example", other.repository, helper=other.helper).url
            first.helper.get_rss_link.assert_called_once()
        finally:
            release.set()
        assert pending.result(timeout=5).url
    assert not recovery.owner._inflight


def test_concurrent_manual_change_is_not_overwritten(recovery):
    """
    网络请求期间用户改写 RSS 时，旧生成结果不能覆盖新配置。
    """
    storage = _storage(_site())
    manual_url = "https://rss.example/rss?passkey=FAKEMANUAL&cat=3"

    def parse_and_edit(*_args, **_kwargs):
        """
        在验证响应到达时提交手工修改，模拟真实请求期间的配置竞争。
        """
        storage.current[0] = replace(storage.current[0], rss=manual_url)
        return []

    storage.helper.parse.side_effect = parse_and_edit
    result = ensure_site_rss("rss.example", storage.repository, expired_url=OLD_URL, helper=storage.helper)
    assert result.url is None
    assert storage.current[0].rss == manual_url


@pytest.mark.parametrize("changes", [dict(public=True, rss=None), dict(is_active=False, rss=None)])
def test_public_or_disabled_site_does_not_generate(recovery, changes):
    """
    公开或停用站点不发起需要私有 Cookie 的 RSS 自动生成。
    """
    storage = _storage(_site(**changes))
    assert ensure_site_rss("rss.example", storage.repository, helper=storage.helper).url is None
    storage.helper.get_rss_link.assert_not_called()


def _chain(storage, monkeypatch) -> TorrentsChain:
    """
    保留真实种子链与共享用例，只替换站点资源和 HTTP 解析边界。
    """
    chain = object.__new__(TorrentsChain)
    chain.site_repository = storage.repository
    chain.runtime_config = SimpleNamespace(user_agent="audit", site_url="http://localhost/")
    chain.post_message = Mock()
    snapshot = storage.current[0]
    indexer = dict(id=snapshot.id, domain=snapshot.domain, url=snapshot.url, rss=snapshot.rss,
                   name=snapshot.name, cookie=snapshot.cookie, ua=snapshot.ua)
    monkeypatch.setattr(torrents_module, "SitesHelper", lambda: SimpleNamespace(get_indexer=lambda _domain: indexer))
    monkeypatch.setattr(RssHelper, "get_rss_link", storage.helper.get_rss_link)
    monkeypatch.setattr(RssHelper, "parse", storage.helper.parse)
    return chain


@pytest.mark.parametrize("missing", [False, True])
def test_torrents_recovers_and_matches_in_current_round(recovery, monkeypatch, missing):
    """
    空地址或明确过期都在本轮返回资源，验证抓取不会被重复执行。
    """
    storage = _storage(_site(rss=None if missing else OLD_URL))
    items = [{"title": "Offline torrent", "enclosure": "https://rss.example/dl/1"}]
    storage.helper.parse.side_effect = [items] if missing else [None, items]
    chain = _chain(storage, monkeypatch)
    result = chain.rss("rss.example")
    assert len(result) == 1 and result[0].title == "Offline torrent"
    assert storage.helper.parse.call_count == (1 if missing else 2)
    chain.post_message.assert_not_called()


def test_torrents_only_notifies_once_for_repeated_failure(recovery, monkeypatch):
    """
    订阅层多次遇到同一过期地址时只通知一次，不绕过用例退避反复生成。
    """
    storage = _storage(_site())
    storage.helper.parse.return_value = None
    storage.helper.get_rss_link.return_value = ("", "无法生成")
    chain = _chain(storage, monkeypatch)
    assert chain.rss("rss.example") == []
    assert chain.rss("rss.example") == []
    chain.post_message.assert_called_once()
    storage.helper.get_rss_link.assert_called_once()


@pytest.mark.parametrize("reachable", [False, True])
def test_cookiecloud_completes_missing_rss_in_same_sync(recovery, monkeypatch, reachable):
    """
    已有有效 Cookie 或本轮刚更新 Cookie，都在本轮补齐缺失 RSS。
    """
    storage = _storage(_site(rss=None))
    chain = object.__new__(SiteChain)
    chain.site_repository = storage.repository
    chain.runtime_config = SimpleNamespace(user_agent="audit")
    monkeypatch.setattr(chain, "test", Mock(return_value=(reachable, "")))

    def update_cookie(**kwargs):
        """
        模拟 Cookie 提交后才对 RSS 用例开放新凭据。
        """
        storage.current[0] = replace(storage.current[0], cookie=kwargs["cookies"])
        return SiteWriteResult(True)

    storage.repository.update_cookie.side_effect = update_cookie
    chain._sync_cookiecloud_domain(domain="rss.example", cookie="FAKE-NEW", indexer={},
                                   site_info=storage.current[0], repository=storage.repository, rsshelper=storage.helper)
    assert storage.current[0].rss
    assert storage.helper.get_rss_link.call_args.kwargs["cookie"] == ("FAKECOOKIE" if reachable else "FAKE-NEW")


@pytest.mark.parametrize("saved", [True, False])
def test_cookiecloud_new_site_generates_only_after_successful_save(recovery, monkeypatch, saved):
    """
    CookieCloud 新增站点先提交 Cookie 再生成 RSS，新增失败时不发起无归属的生成请求。
    """
    storage = _storage(_site(rss=None))
    storage.current[0] = None
    chain = object.__new__(SiteChain)
    chain.runtime_config = SimpleNamespace(cookiecloud_blacklist="", user_agent="audit")
    monkeypatch.setattr(chain, "_cookiecloud_connect", lambda *_args: (
        False, _response(200, '<html><body><a href="logout.php">退出</a></body></html>'),
    ))

    def create(mutation):
        """
        模拟站点创建事务，提交后才允许独立仓储读取快照。
        """
        assert mutation.values["rss"] is None
        storage.current[0] = _site(**mutation.to_payload()) if saved else None
        return SiteWriteResult(saved, "" if saved else "保存失败")

    storage.repository.add.side_effect = create
    result = chain._sync_cookiecloud_domain(
        domain="rss.example", cookie="FAKE-NEW", indexer={"domain": "https://rss.example/", "name": "离线RSS"},
        site_info=None, repository=storage.repository, rsshelper=storage.helper,
    )
    assert result == ((0, 1, 0, True) if saved else (0, 0, 1, False))
    if saved:
        assert storage.current[0].rss
        assert storage.helper.get_rss_link.call_args.kwargs["cookie"] == "FAKE-NEW"
    else:
        storage.helper.get_rss_link.assert_not_called()


def test_site_updated_completes_missing_but_preserves_manual_rss(recovery, monkeypatch):
    """
    手工维护提交后的事件补全空地址，重复事件不改写已经有效的手工地址。
    """
    storage = _storage(_site(rss=None))
    chain = object.__new__(SiteChain)
    chain.site_repository = storage.repository
    monkeypatch.setattr(RssHelper, "get_rss_link", storage.helper.get_rss_link)
    monkeypatch.setattr(RssHelper, "parse", storage.helper.parse)
    event = SimpleNamespace(event_data={"domain": "rss.example"})
    chain.complete_site_rss(event)
    generated_url = storage.current[0].rss
    assert generated_url
    storage.current[0] = replace(storage.current[0], rss=OLD_URL)
    chain.complete_site_rss(event)
    assert storage.current[0].rss == OLD_URL
    storage.helper.get_rss_link.assert_called_once()


@pytest.mark.parametrize("saved", [True, False])
def test_cookie_login_completes_rss_after_new_cookie_commit(recovery, monkeypatch, saved):
    """
    用户名密码登录拿到 Cookie 后，同一请求补全 RSS，并使用已提交的新 Cookie。
    """
    storage = _storage(_site(rss=None))
    chain = object.__new__(SiteChain)
    chain.site_repository = storage.repository
    chain.runtime_config = SimpleNamespace(proxy_server=None)
    login = Mock()
    login.get_site_cookie_ua.return_value = ("FAKE-LOGIN", "New UA", "登录成功")
    monkeypatch.setattr(site_module, "CookieHelper", lambda: login)
    monkeypatch.setattr(RssHelper, "get_rss_link", storage.helper.get_rss_link)
    monkeypatch.setattr(RssHelper, "parse", storage.helper.parse)

    def commit_cookie(_site_id, mutation):
        """
        先保存 Cookie，再返回提交后的快照。
        """
        if not saved:
            return None
        storage.current[0] = replace(storage.current[0], **mutation.to_payload())
        return storage.current[0]

    storage.repository.update.side_effect = commit_cookie
    assert chain.update_cookie(storage.current[0], "user", "FAKEPASSWORD") == (
        (True, "登录成功") if saved else (False, "站点已删除，Cookie未保存")
    )
    if saved:
        assert storage.current[0].rss
        assert storage.helper.get_rss_link.call_args.kwargs["cookie"] == "FAKE-LOGIN"
    else:
        storage.helper.get_rss_link.assert_not_called()
