import re
from typing import List, Optional, Tuple, Union
from urllib.parse import urljoin, urlparse

from lxml import etree

from app.runtime.config import settings
from app.domain.context import Context
from app.db.oper.site import SiteOper
from app.runtime.log import logger
from app.runtime.hostports.siteresource import site_resource_port
from app.modules import _ModuleBase
from app.adapters.network.http import RequestUtils


class SubtitleModule(_ModuleBase):
    """
    字幕下载模块
    """

    # 站点详情页字幕下载元素识别XPATH
    _SITE_SUBTITLE_XPATH = [
        '//td[@class="rowhead"][text()="字幕"]/following-sibling::td//a[not(@class)]',
        '//td[@class="rowhead"][text()="字幕"]/following-sibling::td//a',
        '//div[contains(@class, "font-bold")][text()="字幕"]/following-sibling::div[1]//a[not(@class)]', # 憨憨
    ]
    _SUBTITLE_URL_ATTRS = (
        "href",
        "data-url",
        "data-href",
        "data-link",
        "data-download",
        "data-download-url",
    )
    _SCRIPT_URL_RE = re.compile(
        r"""["'](?P<url>(?:https?:)?//[^"']+|/[^"']+|[^"']*(?:download|subtitle|subs?)[^"']*)["']""",
        re.IGNORECASE,
    )

    def init_module(self) -> None:
        pass

    @staticmethod
    def get_name() -> str:
        return "站点字幕"

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效
        """
        return 0

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    def stop(self) -> None:
        pass

    def test(self):
        pass

    @classmethod
    def __normalize_subtitle_link(cls, page_url: str, sublink: str) -> Optional[str]:
        """
        转换并过滤真实字幕下载链接
        """
        if not sublink:
            return None
        sublink = sublink.strip()
        if not sublink or sublink.startswith("#"):
            return None
        parsed = urlparse(sublink)
        if parsed.scheme and parsed.scheme not in ("http", "https"):
            return None
        if sublink.startswith("//"):
            page_scheme = urlparse(page_url).scheme or "https"
            sublink = f"{page_scheme}:{sublink}"
        else:
            sublink = urljoin(page_url, sublink)
        parsed = urlparse(sublink)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return None
        return sublink

    @classmethod
    def _parse_subtitle_links(cls, html, page_url: str) -> List[str]:
        """
        从站点详情页中解析字幕下载链接
        """
        sublink_list = []
        found_links = set()
        for xpath in cls._SITE_SUBTITLE_XPATH:
            sublink_count = len(sublink_list)
            sublink_nodes = html.xpath(xpath)
            if sublink_nodes:
                for sublink_node in sublink_nodes:
                    sublinks = [sublink_node.get(attr) for attr in cls._SUBTITLE_URL_ATTRS]
                    sublinks.extend(
                        match.group("url")
                        for match in cls._SCRIPT_URL_RE.finditer(sublink_node.get("onclick") or "")
                    )
                    for sublink in sublinks:
                        sublink = cls.__normalize_subtitle_link(page_url, sublink)
                        if not sublink or sublink in found_links:
                            continue
                        found_links.add(sublink)
                        sublink_list.append(sublink)
                # 已成功匹配字幕区域，后续xpath可以忽略
                if len(sublink_list) > sublink_count:
                    break
        return sublink_list

    def site_subtitle_links(self, context: Context) -> Optional[List[str]]:
        """
        解析普通站点详情页获取字幕下载链接
        :param context:  上下文，包括识别信息、媒体信息、种子信息
        :return: 字幕下载链接列表，无法访问页面时返回None
        """
        torrent = context.torrent_info
        if not torrent.page_url:
            return None
        # 采用API访问的站点由对应爬虫模块处理，详情页HTML不含字幕元素
        if torrent.site is not None:
            site = SiteOper().get(torrent.site)
            if site and (indexer := site_resource_port.resolve().get_indexer(site.domain)):
                if indexer.get("parser") == "mTorrent":
                    return None
        request = RequestUtils(
            cookies=torrent.site_cookie,
            ua=torrent.site_ua,
            proxies=settings.PROXY if torrent.site_proxy else None,
        )
        res = request.get_res(torrent.page_url)
        if res and res.status_code == 200:
            if not res.text:
                logger.warn(f"读取页面代码失败：{torrent.page_url}")
                return []
            html = etree.HTML(res.text)
            try:
                return self._parse_subtitle_links(html, torrent.page_url)
            finally:
                if html is not None:
                    del html
        elif res is not None:
            logger.warn(f"连接 {torrent.page_url} 失败，状态码：{res.status_code}")
        else:
            logger.warn(f"无法打开链接：{torrent.page_url}")
        return None
