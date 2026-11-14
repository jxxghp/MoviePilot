"""
RSS 订阅获取、站点链接生成、浏览器回退与资源解析。
"""

import hashlib
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, List, Literal, Optional, Protocol, Tuple, Union

import dateutil.parser
from cachetools import TTLCache
from lxml import etree

from app.application.configuration import get_chain_runtime_config_snapshot
from app.application.site.contract import SiteRepository, SiteSnapshot
from app.domain import site as site_rules
from app.foundation.url import UrlUtils, mask_url_credentials
from app.runtime.log import logger


class RssResponsePort(Protocol):
    """
    声明 RSS 用例实际读取的 HTTP 响应字段。

    :ivar status_code: HTTP 响应状态码
    :ivar content: 响应原始字节，供受控的 XML 编码探测使用
    :ivar text: 客户端解码后的响应文本
    :ivar reason: HTTP 响应原因短语，用于失败诊断
    """

    status_code: int
    content: bytes
    text: str
    reason: str


class RssHttpPort(Protocol):
    """
    声明 RSS 用例所需的同步 HTTP 与 XML 解码能力。
    """

    def get(self, *, url: str, ua: Optional[str], headers: Optional[dict[str, Any]],
            proxies: Optional[dict[str, Any]], timeout: int) -> Optional[RssResponsePort]:
        """
        获取 RSS 响应。

        :param url: RSS 订阅地址或站点生成接口 URL
        :param ua: 请求使用的 User-Agent
        :param headers: 额外请求头，None 表示不提供额外头
        :param proxies: 协议到代理连接参数的映射，None 表示不指定代理
        :param timeout: HTTP 请求超时，单位为秒
        :return: HTTP 响应；未取得响应时为 None
        """

    def post(self, *, url: str, data: dict[str, Any], cookie: str, ua: str,
             proxies: Optional[dict[str, Any]], timeout: int) -> Optional[RssResponsePort]:
        """
        提交 RSS 链接生成表单。

        :param url: RSS 订阅地址或站点生成接口 URL
        :param data: 站点生成 RSS 链接所需的表单字段
        :param cookie: 站点认证 Cookie 文本
        :param ua: 请求使用的 User-Agent
        :param proxies: 协议到代理连接参数的映射，None 表示不指定代理
        :param timeout: HTTP 请求超时，单位为秒
        :return: HTTP 响应；未取得响应时为 None
        """

    def decode_xml(self, response: RssResponsePort, *, performance_mode: bool,
                   confidence_threshold: float) -> str:
        """
        按宿主编码策略解码 XML 响应。

        :param response: 待解码的原始 HTTP 响应
        :param performance_mode: 是否采用减少编码探测开销的性能模式
        :param confidence_threshold: 编码探测结果可接受的最低置信度
        :return: 按宿主编码策略解码后的 XML 文本
        """


class RssBrowserPort(Protocol):
    """
    声明 RSS 链接页面渲染能力。
    """

    def render(self, *, url: str, cookie: str, ua: str,
               proxies: Optional[dict[str, Any]], timeout: int) -> Optional[str]:
        """
        渲染页面并返回源码。

        :param url: 需要浏览器执行并渲染的站点页面 URL
        :param cookie: 站点认证 Cookie 文本
        :param ua: 浏览器请求使用的 User-Agent
        :param proxies: 浏览器代理连接参数，None 表示不指定代理
        :param timeout: 浏览器页面获取的超时时间，单位为秒
        :return: 渲染后的页面源码，获取失败时为 None
        """


class RssParserPort(Protocol):
    """
    声明可选原生 RSS 解析加速能力。
    """

    def parse(self, content: str, max_items: int) -> Optional[List[dict[str, Any]]]:
        """
        解析 RSS XML；不可用或失败时返回 None 触发 Python 兜底。

        :param content: 待解析的 RSS XML 文本
        :param max_items: 最多保留的资源条目数
        :return: 解析后的资源条目；能力不可用或解析失败时为 None，成功但无资源时为空列表
        """


@dataclass(frozen=True, slots=True)
class RssLinkRecipe:
    """
    站点 Module 提供的 RSS 链接获取配方，公共用例只负责执行。

    :param url: RSS 配置页面的完整地址
    :param xpath: 获取生成链接的页面选择器
    :param params: 获取链接时提交的表单字段
    :param render: 是否使用已注入的浏览器端口
    """

    url: str
    xpath: str
    params: dict[str, Any]
    render: bool = False


class RssSitePort(Protocol):
    """
    RSS 用例消费的站点协议端口，由启动组合根接到 Module 分发。
    """

    def get_site_rss_recipe(self, url: str) -> RssLinkRecipe:
        """
        获取适配器选择的 RSS 获取配方，不执行 I/O。

        :param url: 站点根地址
        :return: 专站或通用站点配方
        """
        ...

    def is_site_rss_expired(self, text: str) -> bool:
        """
        解释站点返回的 RSS 过期响应。

        :param text: RSS 响应正文
        :return: 适配器确认需要重建 RSS 地址时为 True
        """
        ...


_rss_http_port: Optional[RssHttpPort] = None
_rss_browser_port: Optional[RssBrowserPort] = None
_rss_parser_port: Optional[RssParserPort] = None
_rss_site_port: Optional[RssSitePort] = None


def configure_rss_site_port(port: RssSitePort) -> None:
    """
    注入由 Module 分发执行的 RSS 协议解释端口。

    :param port: 本生命周期的 RSS 站点协议端口
    """
    global _rss_site_port
    _rss_site_port = port


def reset_rss_site_port() -> None:
    """
    撤销 RSS 协议端口，不影响独立的传输端口。
    """
    global _rss_site_port
    _rss_site_port = None


def _require_rss_site_port() -> RssSitePort:
    """
    返回已装配的站点协议端口，未装配时明确失败。
    """
    if _rss_site_port is None:
        raise RuntimeError("RSS 站点协议端口尚未由启动组合根装配")
    return _rss_site_port


def configure_rss_ports(*, http: RssHttpPort, browser: RssBrowserPort,
                        parser: RssParserPort) -> None:
    """
    由组合根装配 RSS 用例依赖的技术端口。
    """
    global _rss_http_port, _rss_browser_port, _rss_parser_port
    _rss_http_port = http
    _rss_browser_port = browser
    _rss_parser_port = parser


def reset_rss_ports() -> None:
    """
    清除 RSS 端口，供生命周期退出与隔离测试使用。
    """
    global _rss_http_port, _rss_browser_port, _rss_parser_port
    _rss_http_port = None
    _rss_browser_port = None
    _rss_parser_port = None
    _rss_link_recovery.reset()


def _require_rss_ports() -> Tuple[RssHttpPort, RssBrowserPort, RssParserPort]:
    """
    返回已装配端口，缺失时明确拒绝隐式构造 Adapter。
    """
    if _rss_http_port is None or _rss_browser_port is None or _rss_parser_port is None:
        raise RuntimeError("RSS 访问端口尚未由启动组合根装配")
    return _rss_http_port, _rss_browser_port, _rss_parser_port


class RssHelper:
    """
    RSS帮助类，解析RSS报文、获取RSS地址等
    """

    # RSS解析限制配置
    # 50MB最大RSS文件大小
    MAX_RSS_SIZE = 50 * 1024 * 1024
    # 最大解析条目数
    MAX_RSS_ITEMS = 1000

    def _response_status(self, response: RssResponsePort, url: str) -> Literal["valid", "expired", "error"]:
        """
        在 XML 解析前判断大小、明确过期事实和 HTTP 错误，保留非 200 的响应正文。

        :param response: 实际收到的响应，不能以 requests 的真假值判断其是否存在
        :param url: 仅供脱敏诊断的请求地址
        :return: 可解析、需要续期或普通失败
        """
        if len(response.content) > self.MAX_RSS_SIZE:
            logger.warning(f"RSS文件过大: {len(response.content) / 1024 / 1024:.1f}MB，跳过解析")
            return "error"
        if response.status_code == 200 or "rss" in response.text.casefold():
            if _require_rss_site_port().is_site_rss_expired(response.text):
                return "expired"
        if response.status_code != 200:
            logger.error(f"RSS请求失败，状态码: {response.status_code}, URL: {mask_url_credentials(url)}")
            return "error"
        return "valid"

    @staticmethod
    def _parse_publish_time(value: str) -> Optional[datetime]:
        """
        将 RSS 常见日期表达解析为 datetime，无法解析时返回 None。
        """
        try:
            return dateutil.parser.parse(value)
        except dateutil.parser.ParserError:
            return None

    def __parse_with_rust(self, ret_xml: Optional[str]) -> Optional[list[dict[str, Any]]]:
        """
        调用 Rust RSS 解析器，并统一处理基础 XML 校验和最大条目限制。
        """
        if not ret_xml or not ret_xml.strip():
            return None
        ret_xml_stripped = ret_xml.strip()
        if not ret_xml_stripped.startswith('<'):
            return None
        _, _, parser_port = _require_rss_ports()
        rust_items = parser_port.parse(ret_xml, self.MAX_RSS_ITEMS + 1)
        if rust_items is None:
            return None
        if len(rust_items) > self.MAX_RSS_ITEMS:
            logger.warning(f"RSS条目过多: 超过{self.MAX_RSS_ITEMS}，仅处理前{self.MAX_RSS_ITEMS}个")
        return rust_items[:self.MAX_RSS_ITEMS]

    def parse(self, url: str, proxy: bool = False,
              timeout: Optional[int] = 15, headers: Optional[dict[str, str]] = None, ua: Optional[str] = None) -> Union[List[dict[str, Any]], None, bool]:
        """
        解析 RSS 地址并保留插件兼容的返回约定。
        """
        normalized_url = UrlUtils.normalize_http_url(url)
        if normalized_url is None:
            return False
        return self._parse_impl(normalized_url, proxy=proxy, timeout=timeout, headers=headers, ua=ua)

    def _parse_impl(self, url: str, proxy: bool = False,
                    timeout: Optional[int] = 15, headers: Optional[dict[str, str]] = None,
                    ua: Optional[str] = None) -> Union[List[dict[str, Any]], None, bool]:
        """
        解析RSS订阅URL，获取RSS中的种子信息
        :param url: RSS地址
        :param proxy: 是否使用代理
        :param timeout: 请求超时
        :param headers: 自定义请求头
        :param ua: 自定义User-Agent
        :return: 种子信息列表，如为None代表Rss过期，如果为False则为错误
        """
        ret_array: list[dict[str, Any]] = []
        http_port, _, _ = _require_rss_ports()

        try:
            ret = http_port.get(
                url=url,
                ua=ua,
                proxies=get_chain_runtime_config_snapshot().proxy if proxy else None,
                timeout=timeout or 30,
                headers=headers,
            )
            if ret is None:
                logger.error(f"获取RSS失败：请求返回空值，URL: {mask_url_credentials(url)}")
                return False
        except Exception as err:
            logger.error(mask_url_credentials(f"获取RSS失败：{str(err)} - {traceback.format_exc()}"))
            return False

        if ret is not None:
            ret_xml = None
            root = None
            try:
                status = self._response_status(ret, url)
                if status != "valid":
                    return None if status == "expired" else False

                if ret.content:
                    ret_xml = http_port.decode_xml(
                        ret,
                        performance_mode=get_chain_runtime_config_snapshot().encoding_detection_performance_mode,
                        confidence_threshold=get_chain_runtime_config_snapshot().encoding_detection_min_confidence
                    )
                if not ret_xml:
                    ret_xml = ret.text

                if not ret_xml or not ret_xml.strip():
                    logger.error("RSS内容为空")
                    return False

                if not site_rules.is_rss_document(ret_xml):
                    logger.error("RSS内容不是有效的订阅文档，可能是登录页或错误页")
                    return False

                rust_items = self.__parse_with_rust(ret_xml)
                if rust_items is not None:
                    return rust_items

                parser = None
                try:
                    # 创建解析器，禁用网络访问以提高安全性和性能
                    parser = etree.XMLParser(
                        # 容错模式
                        recover=True,
                        # 保留CDATA
                        strip_cdata=False,
                        # 禁用外部实体解析
                        resolve_entities=False,
                        # 禁用网络访问
                        no_network=True,
                        # 禁用大文档解析，避免内存问题
                        huge_tree=False
                    )
                    root = etree.fromstring(ret_xml.encode('utf-8'), parser=parser)
                except etree.XMLSyntaxError as xml_error:
                    logger.debug(f"XML解析失败：{str(xml_error)}，尝试HTML解析")
                    # 如果XML解析失败，尝试作为HTML解析
                    try:
                        root = etree.HTML(ret_xml)
                        if root is not None:
                            rss_root = root.xpath('//rss | //feed')
                            if rss_root:
                                root = rss_root[0]
                    except Exception as e:
                        logger.error(f"HTML解析也失败：{str(e)}")
                        return False
                except Exception as general_error:
                    logger.error(f"解析RSS时发生未预期错误：{str(general_error)}")
                    return False
                finally:
                    if parser is not None:
                        try:
                            parser.close()
                        except Exception as close_error:
                            logger.debug(f"关闭解析器时出错：{str(close_error)}")
                        del parser

                if root is None:
                    logger.error("无法解析RSS内容")
                    return False

                items = root.xpath('.//item | .//entry')

                items_count = min(len(items), self.MAX_RSS_ITEMS)
                if len(items) > self.MAX_RSS_ITEMS:
                    logger.warning(f"RSS条目过多: {len(items)}，仅处理前{self.MAX_RSS_ITEMS}个")
                try:
                    for item in items[:items_count]:
                        try:
                            title_nodes = item.xpath('.//title')
                            title = title_nodes[0].text if title_nodes and title_nodes[0].text else ""
                            if not title:
                                continue

                            # 描述
                            desc_nodes = item.xpath('.//description | .//summary')
                            description = desc_nodes[0].text if desc_nodes and desc_nodes[0].text else ""

                            # 种子页面
                            link_nodes = item.xpath('.//link')
                            if link_nodes:
                                link = link_nodes[0].text if hasattr(link_nodes[0], 'text') and link_nodes[0].text else link_nodes[0].get('href', '')
                            else:
                                link = ""

                            # 种子链接
                            enclosure_nodes = item.xpath('.//enclosure')
                            enclosure = enclosure_nodes[0].get('url', '') if enclosure_nodes else ""
                            if not enclosure and not link:
                                continue
                            # 部分RSS只有link没有enclosure
                            if not enclosure and link:
                                enclosure = link

                            # 大小
                            size = 0
                            if enclosure_nodes:
                                size_attr = enclosure_nodes[0].get('length', '0')
                                if size_attr and str(size_attr).isdigit():
                                    size = int(size_attr)

                            # 发布日期
                            pubdate_nodes = item.xpath('./pubDate | ./published | ./updated')
                            if not pubdate_nodes:
                                pubdate_nodes = item.xpath('.//*[local-name()="pubDate"] | .//*[local-name()="published"] | .//*[local-name()="updated"]')

                            pubdate: Union[str, datetime] = ""
                            if pubdate_nodes and pubdate_nodes[0].text:
                                parsed_pubdate = self._parse_publish_time(pubdate_nodes[0].text)
                                if parsed_pubdate is not None:
                                    # 转为本地时区
                                    pubdate = parsed_pubdate.astimezone(tz=None)

                            # 获取豆瓣昵称
                            nickname_nodes = item.xpath('.//*[local-name()="creator"]')
                            nickname = nickname_nodes[0].text if nickname_nodes and nickname_nodes[0].text else ""

                            tmp_dict = {
                                'title': title,
                                'enclosure': enclosure,
                                'size': size,
                                'description': description,
                                'link': link,
                                'pubdate': pubdate
                            }
                            # 如果豆瓣昵称不为空，返回数据增加豆瓣昵称，供doubansync插件获取
                            if nickname:
                                tmp_dict['nickname'] = nickname
                            ret_array.append(tmp_dict)

                        except Exception as e1:
                            logger.debug(f"解析RSS条目失败：{str(e1)} - {traceback.format_exc()}")
                            continue
                finally:
                    items.clear()
                    del items

            except Exception as e2:
                logger.error(f"解析RSS失败：{str(e2)} - {traceback.format_exc()}")
                return False
            finally:
                if root is not None:
                    del root
                if ret_xml is not None:
                    del ret_xml

        return ret_array

    @staticmethod
    def _extract_rss_link(html_text: str, recipe: RssLinkRecipe) -> Optional[str]:
        """
        提取可抓取的生成结果，宽选择器跳过导航链接，兼容相对地址。

        :param html_text: 生成页面正文
        :param recipe: Module 提供的生成页地址和选择器
        :return: 最后一个有效候选；没有候选时为 None
        """
        html = etree.HTML(html_text)
        if html is None:
            return None
        try:
            for value in reversed(html.xpath(recipe.xpath)):
                candidate = site_rules.normalize_rss_link(value, recipe.url, broad=recipe.xpath == "//a/@href")
                if candidate:
                    return candidate
            return None
        finally:
            del html

    def get_rss_link(self, url: str, cookie: str, ua: str, proxy: bool = False,
                     timeout: Optional[int] = None, render: bool = False) -> Tuple[str, str]:
        """
        获取站点rss地址
        :param url: 站点地址
        :param cookie: 站点cookie
        :param ua: 站点ua
        :param proxy: 是否使用代理
        :param timeout: 请求超时时间
        :param render: 是否按站点配置使用浏览器生成，专站配方也可强制启用
        :return: rss地址、错误信息
        """
        try:
            http_port, browser_port, _ = _require_rss_ports()
            recipe = _require_rss_site_port().get_site_rss_recipe(url)
            if recipe.render or render:
                html_text = browser_port.render(
                    url=recipe.url,
                    cookie=cookie,
                    ua=ua,
                    proxies=get_chain_runtime_config_snapshot().proxy_server if proxy else None,
                    timeout=timeout or 60
                )
            else:
                res = http_port.post(
                    url=recipe.url,
                    data=recipe.params,
                    cookie=cookie,
                    ua=ua,
                    proxies=get_chain_runtime_config_snapshot().proxy if proxy else None,
                    timeout=timeout or 30,
                )
                if res is not None and res.status_code == 200:
                    html_text = res.text
                elif res is not None:
                    return "", f"获取 {url} RSS链接失败，错误码：{res.status_code}，错误原因：{res.reason}"
                else:
                    return "", f"获取RSS链接失败：无法连接 {url} "

            if html_text:
                rss_link = self._extract_rss_link(html_text, recipe)
                if rss_link:
                    return rss_link, ""

            return "", f"获取RSS链接失败：{url}"
        except Exception as e:
            message = mask_url_credentials(f"获取 {url} RSS链接失败：{str(e)}")
            return "", message.replace(cookie, "<REDACTED>") if cookie else message


@dataclass(frozen=True, slots=True)
class RssLinkResult:
    """
    站点 RSS 获取、续期和验证的结果，不在进程缓存中保留地址或订阅内容。

    :param url: 当前可使用的地址，失败或正在退避时为 None
    :param items: 本次验证抓取到的资源，供同轮复用；未抓取时为 None
    :param message: 不含凭据的失败或延后原因
    :param notify: 是否需要发送本轮首次或间隔一天的续期失败通知
    """

    url: Optional[str] = None
    items: Optional[list[dict[str, Any]]] = None
    message: str = ""
    notify: bool = False


@dataclass(frozen=True, slots=True)
class _RssLinkState:
    """
    单份站点配置的失败退避与通知时间，不保存 Cookie、RSS 地址等原文。

    :param failures: 本配置连续获取失败次数，最多累计十二次
    :param retry_at: 下一次允许生成链接的单调时钟时间
    :param notified_at: 最近一次过期通知时间；None 表示尚未通知
    """

    failures: int = 0
    retry_at: float = 0.0
    notified_at: Optional[float] = None


class _RssLinkRecovery:
    """
    合并同站点的链接获取，使用有界短期状态退避失败并防止重复通知。
    """

    # 不支持自动生成的站点也会进入退避；最多保存一天、1024 份配置的摘要。
    _STATE_TTL = 24 * 3600
    _RETRY_INITIAL = 5 * 60
    _RETRY_MAX = 3600

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        """
        创建仅保护状态读写的锁，网络与数据库操作始终在锁外执行。

        :param clock: 单调时钟，测试可注入虚拟时间
        """
        self._clock = clock
        self._lock = threading.Lock()
        self._states: TTLCache[str, _RssLinkState] = TTLCache(maxsize=1024, ttl=self._STATE_TTL, timer=clock)
        self._inflight: set[int] = set()

    def reset(self) -> None:
        """
        技术端口退出时清理退避状态，仍在执行的准入由原调用负责释放。
        """
        with self._lock:
            self._states.clear()

    @staticmethod
    def _fingerprint(site: SiteSnapshot) -> str:
        """
        用配置摘要隔离退避，更新 Cookie 或手工修改 RSS 后立即获得新预算。
        """
        values = (site.id, site.url, site.rss, site.cookie, site.ua, site.apikey, site.token,
                  site.proxy, site.timeout, site.render)
        return hashlib.sha256(repr(values).encode("utf-8")).hexdigest()

    @staticmethod
    def _existing(site: SiteSnapshot, expired_url: Optional[str]) -> Optional[RssLinkResult]:
        """
        保留有效配置及并发手工修改，停用或公开站点不自动生成私有订阅。
        """
        current = UrlUtils.normalize_http_url(site.rss)
        if current and current != expired_url:
            return RssLinkResult(url=current)
        if site.is_active is False or site.public:
            return RssLinkResult(message="站点已停用或无需生成私有RSS")
        if site.rss and site.rss.strip() and not current:
            return RssLinkResult(message="RSS地址无效，请修正或清空后自动获取")
        return None

    def _failure(self, key: str, message: str, expired: bool) -> RssLinkResult:
        """
        失败从五分钟逐次退避到一小时，过期通知对同配置最多每天发送一次。
        """
        with self._lock:
            previous = self._states.get(key, _RssLinkState())
            now = self._clock()
            failures = min(previous.failures + 1, 12)
            notify = expired and (previous.notified_at is None or now - previous.notified_at >= self._STATE_TTL)
            self._states[key] = _RssLinkState(
                failures=failures,
                retry_at=now + min(self._RETRY_INITIAL * 2 ** (failures - 1), self._RETRY_MAX),
                notified_at=now if notify else previous.notified_at,
            )
        logger.warning(message)
        return RssLinkResult(message=message, notify=notify)

    @staticmethod
    def _attempt(site: SiteSnapshot, repository: SiteRepository, helper: RssHelper,
                 expired_url: Optional[str]) -> RssLinkResult:
        """
        生成并实际验证候选，只有验证成功且配置未变化时才保存新地址。
        """
        if not site.url or not site.cookie:
            return RssLinkResult(message="站点缺少地址或Cookie，无法自动获取RSS")
        ua = site.ua or get_chain_runtime_config_snapshot().user_agent
        generated, message = helper.get_rss_link(
            url=site.url, cookie=site.cookie, ua=ua, proxy=bool(site.proxy),
            timeout=site.timeout, render=bool(site.render),
        )
        normalized = site_rules.normalize_rss_link(generated, site.url)
        if normalized is None:
            safe_message = mask_url_credentials(message).replace(site.cookie, "<REDACTED>")
            return RssLinkResult(message=safe_message or "未获取到有效RSS地址")
        candidate = site_rules.renew_rss_link(site.rss, normalized)
        if candidate == expired_url:
            return RssLinkResult(message="站点仍返回已过期的RSS地址，请更新Cookie或手工填写")
        items = helper.parse(candidate, proxy=bool(site.proxy), timeout=site.timeout or 30, ua=ua)
        if not isinstance(items, list):
            return RssLinkResult(message="新RSS地址验证失败，保留原配置并稍后重试")
        saved = repository.update_rss_if_unchanged(site, candidate)
        if not saved.success:
            return RssLinkResult(message=saved.message or "保存新RSS地址失败")
        logger.info(f"站点 {site.domain} RSS地址已验证并更新")
        return RssLinkResult(url=candidate, items=items)

    def ensure(self, domain: str, repository: SiteRepository, *, expired_url: Optional[str],
               helper: RssHelper) -> RssLinkResult:
        """
        按最新已提交配置补全缺失地址或续期，仅让同站点一个调用执行生成请求。

        :param domain: 站点域名，用于从仓储读取最新配置
        :param repository: 独立事务的站点仓储
        :param expired_url: 本轮明确过期的地址；None 表示只补全缺失地址
        :param helper: RSS 生成及抓取边界
        :return: 可复用的地址、已验证资源或退避原因
        """
        try:
            site = repository.get_by_domain(domain)
        except Exception as error:
            message = f"站点 {domain} RSS配置读取失败（{type(error).__name__}）"
            logger.error(message)
            return RssLinkResult(message=message)
        if site is None:
            return RssLinkResult(message="站点不存在")
        existing = self._existing(site, expired_url)
        if existing is not None:
            return existing
        key = self._fingerprint(site)
        with self._lock:
            state = self._states.get(key, _RssLinkState())
            if site.id in self._inflight or state.retry_at > self._clock():
                return RssLinkResult(message="RSS自动获取正在进行或等待重试")
            self._inflight.add(site.id)
        try:
            # 准入前另一个生成任务可能刚结束，重新读取以复用其保存结果。
            current = repository.get_by_domain(domain)
            if current != site:
                existing = self._existing(current, expired_url) if current else None
                return existing or RssLinkResult(message="站点配置已变化或已删除，稍后重试")
            result = self._attempt(site, repository, helper, expired_url)
            if result.url:
                with self._lock:
                    self._states.pop(key, None)
                return result
            return self._failure(key, result.message, expired_url is not None)
        except Exception as error:
            return self._failure(key, f"站点 {domain} RSS自动获取失败（{type(error).__name__}）", expired_url is not None)
        finally:
            with self._lock:
                self._inflight.remove(site.id)


_rss_link_recovery = _RssLinkRecovery()


def ensure_site_rss(domain: str, repository: SiteRepository, *, expired_url: Optional[str] = None,
                    helper: Optional[RssHelper] = None) -> RssLinkResult:
    """
    供手工维护、CookieCloud 和订阅抓取共用的 RSS 补全与续期入口。

    :param domain: 已保存站点的域名
    :param repository: 负责短事务和原子比较更新的仓储
    :param expired_url: 已确认过期的旧地址；缺省只补全空配置
    :param helper: 可选的 RSS 边界，默认使用启动注入的真实端口
    :return: 地址、可供本轮复用的资源，以及脱敏的失败提示
    """
    return _rss_link_recovery.ensure(domain, repository, expired_url=expired_url, helper=helper or RssHelper())
