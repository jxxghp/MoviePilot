# -*- coding: utf-8 -*-
"""
解析 IPTorrents 站点的用户资料、流量和做种信息。
"""

import re
from typing import Optional

from lxml import etree

from app.foundation import size as size_tools
from app.foundation import temporal as time_tools
from app.foundation import text as text_tools
from app.foundation.dom import DomUtils
from app.modules.indexer.parser import SitePage, SiteParserBase, SiteSchema


class IptSiteUserInfo(SiteParserBase):
    """
    解析 Ipt 站点的用户资料、流量与消息。
    """
    schema = SiteSchema.Ipt

    def _parse_user_base_info(self, html_text: SitePage) -> None:
        """
        解析站点用户身份和页面提供的基础统计。
        """
        html_text = self._prepare_html_text(html_text)
        html = etree.HTML(html_text)
        try:
            tmps = html.xpath('//a[contains(@href, "/u/")]//text()')
            tmps_id = html.xpath('//a[contains(@href, "/u/")]/@href')
            if tmps:
                self.username = str(tmps[-1])
            if tmps_id:
                user_id_match = re.search(r"/u/(\d+)", tmps_id[0])
                if user_id_match and user_id_match.group().strip():
                    self.userid = user_id_match.group(1)
                    self._user_detail_page = f"user.php?u={self.userid}"
                    self._torrent_seeding_page = f"peers?u={self.userid}"

            tmps = html.xpath('//div[@class = "stats"]/div/div')
            if tmps:
                self.upload = size_tools.parse_size(str(tmps[0].xpath('span/text()')[1]).strip())
                self.download = size_tools.parse_size(str(tmps[0].xpath('span/text()')[2]).strip())
                self.seeding = text_tools.parse_int(tmps[0].xpath('a')[2].xpath('text()')[0])
                self.leeching = text_tools.parse_int(tmps[0].xpath('a')[2].xpath('text()')[1])
                self.ratio = text_tools.parse_float(str(tmps[0].xpath('span/text()')[0]).strip().replace('-', '0'))
                self.bonus = text_tools.parse_float(tmps[0].xpath('a')[3].xpath('text()')[0])
        finally:
            if html is not None:
                del html

    def _parse_site_page(self, html_text: SitePage) -> None:
        """
        该站点在其他解析入口处理此数据，本入口不追加操作。
        """
        pass

    def _parse_user_detail_info(self, html_text: str) -> None:
        """
        解析用户等级、注册时间及其他资料。
        """
        html = etree.HTML(html_text)
        try:
            if not DomUtils.has_child_elements(html):
                return

            user_levels_text = html.xpath('//tr/th[text()="Class"]/following-sibling::td[1]/text()')
            if user_levels_text:
                self.user_level = user_levels_text[0].strip()

            # 加入日期
            join_at_text = html.xpath('//tr/th[text()="Join date"]/following-sibling::td[1]/text()')
            if join_at_text:
                self.join_at = time_tools.normalize_datetime(join_at_text[0].split(' (')[0])
        finally:
            if html is not None:
                del html

    def _parse_user_torrent_seeding_info(self, html_text: str, multi_page: bool = False) -> None:
        """
        读取做种页面数据并返回后续分页入口。
        """
        html = etree.HTML(html_text)
        try:
            if not DomUtils.has_child_elements(html):
                return None
            # 做种列表从表格内容中读取，保留每条种子的体积和做种人数。
            seeding_end_pos = 3
            if html.xpath('//tr/td[text() = "Leechers"]'):
                seeding_end_pos = len(html.xpath('//tr/td[text() = "Leechers"]/../preceding-sibling::tr')) + 1
                seeding_end_pos = seeding_end_pos - 3

            page_seeding = 0
            page_seeding_size = 0
            seeding_torrents = html.xpath('//tr/td[text() = "Seeders"]/../following-sibling::tr/td[position()=6]/text()')
            if seeding_torrents:
                page_seeding = seeding_end_pos
                for per_size in seeding_torrents[:seeding_end_pos]:
                    if '(' in per_size and ')' in per_size:
                        per_size = per_size.split('(')[-1]
                        per_size = per_size.split(')')[0]

                    page_seeding_size += size_tools.parse_size(per_size)

            self.seeding = page_seeding
            self.seeding_size = page_seeding_size
        finally:
            if html is not None:
                del html

    def _parse_user_traffic_info(self, html_text: str) -> None:
        """
        该站点在其他解析入口处理此数据，本入口不追加操作。
        """
        pass

    def _parse_message_unread_links(self, html_text: str, msg_links: list[str]) -> Optional[str]:
        """
        收集未读消息链接，返回仍需读取的下一页。
        """
        return None

    def _parse_message_content(self, html_text: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """
        解析消息标题、日期和正文；无独立消息页时返回空结果。
        """
        return None, None, None
