# -*- coding: utf-8 -*-
"""
解析 Discuz 站点的用户资料、流量和做种信息。
"""

import re
from typing import Optional

from lxml import etree

from app.foundation import size as size_tools
from app.foundation import temporal as time_tools
from app.foundation import text as text_tools
from app.foundation.dom import DomUtils
from app.modules.indexer.parser import SitePage, SiteParserBase, SiteSchema


class DiscuzUserInfo(SiteParserBase):
    """
    解析 Discuz 站点的用户资料、流量与消息。
    """
    schema = SiteSchema.DiscuzX

    def _parse_user_base_info(self, html_text: SitePage) -> None:
        """
        解析站点用户身份和页面提供的基础统计。
        """
        html_text = self._prepare_html_text(html_text)
        html = etree.HTML(html_text)
        try:
            user_info = html.xpath('//a[contains(@href, "&uid=")]')
            if user_info:
                user_id_match = re.search(r"&uid=(\d+)", user_info[0].attrib['href'])
                if user_id_match and user_id_match.group().strip():
                    self.userid = user_id_match.group(1)
                    self._torrent_seeding_page = "forum.php?&mod=torrents&cat_5up=on"
                    self._user_detail_page = user_info[0].attrib['href']
                    self.username = user_info[0].text.strip()
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
        解析用户等级、加入时间、积分和流量信息。

        :param html_text: 当前页面或 API 响应文本
        """
        html = etree.HTML(html_text)
        try:
            if not DomUtils.has_child_elements(html):
                return None

            # 用户等级
            user_levels_text = html.xpath('//a[contains(@href, "usergroup")]/text()')
            if user_levels_text:
                self.user_level = user_levels_text[-1].strip()

            # 加入日期
            join_at_text = html.xpath('//li[em[text()="注册时间"]]/text()')
            if join_at_text:
                self.join_at = time_tools.normalize_datetime(join_at_text[0].strip())

            # 分享率可能与其他 PT 指标同处一行，需合并子标签文本并按标签取值。
            ratio_items = html.xpath('//li[contains(., "分享率")]')
            for ratio_item in ratio_items:
                ratio_match = re.search(r"分享率\s*[:：]?\s*[（(]?\s*([\d,.]+)", ratio_item.xpath('string(.)'))
                if ratio_match:
                    self.ratio = text_tools.parse_float(ratio_match.group(1))
                    break

            # 积分
            bouns_text = html.xpath('//li[em[text()="积分"]]/text()')
            if bouns_text:
                self.bonus = text_tools.parse_float(bouns_text[0].strip())

            # 上传
            upload_text = html.xpath('//li[em[contains(text(),"上传量")]]/text()')
            if upload_text:
                self.upload = size_tools.parse_size(upload_text[0].strip().split('/')[-1])

            # 下载
            download_text = html.xpath('//li[em[contains(text(),"下载量")]]/text()')
            if download_text:
                self.download = size_tools.parse_size(download_text[0].strip().split('/')[-1])
        finally:
            if html is not None:
                del html

    def _parse_user_torrent_seeding_info(self, html_text: str, multi_page: bool = False) -> Optional[str]:
        """
        做种相关信息。

        :param html_text: 当前页面或 API 响应文本
        :param multi_page: 是否多页数据
        :return: 下页地址
        """
        html = etree.HTML(html_text)
        try:
            if not DomUtils.has_child_elements(html):
                return None

            size_col = 3
            seeders_col = 4
            # 搜索size列
            if html.xpath('//tr[position()=1]/td[.//img[@class="size"] and .//img[@alt="size"]]'):
                size_col = len(html.xpath('//tr[position()=1]/td[.//img[@class="size"] '
                                          'and .//img[@alt="size"]]/preceding-sibling::td')) + 1
            # 搜索seeders列
            if html.xpath('//tr[position()=1]/td[.//img[@class="seeders"] and .//img[@alt="seeders"]]'):
                seeders_col = len(html.xpath('//tr[position()=1]/td[.//img[@class="seeders"] '
                                             'and .//img[@alt="seeders"]]/preceding-sibling::td')) + 1

            page_seeding = 0
            page_seeding_size = 0
            page_seeding_info = []
            seeding_sizes = html.xpath(f'//tr[position()>1]/td[{size_col}]')
            seeding_seeders = html.xpath(f'//tr[position()>1]/td[{seeders_col}]//text()')
            if seeding_sizes and seeding_seeders:
                page_seeding = len(seeding_sizes)

                for i in range(0, len(seeding_sizes)):
                    size = size_tools.parse_size(seeding_sizes[i].xpath("string(.)").strip())
                    seeders = text_tools.parse_int(seeding_seeders[i])

                    page_seeding_size += size
                    page_seeding_info.append([seeders, size])

            self.seeding += page_seeding
            self.seeding_size += page_seeding_size
            self.seeding_info.extend(page_seeding_info)

            # 是否存在下页数据
            next_page = None
            next_page_text = html.xpath('//a[contains(.//text(), "下一页") or contains(.//text(), "下一頁")]/@href')
            if next_page_text:
                next_page = next_page_text[-1].strip()
        finally:
            if html is not None:
                del html

        return next_page

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
