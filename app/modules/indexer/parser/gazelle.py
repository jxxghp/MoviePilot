# -*- coding: utf-8 -*-
import re
from typing import Optional

from lxml import etree

from app.foundation import size as size_tools
from app.foundation import temporal as time_tools
from app.foundation import text as text_tools
from app.foundation.dom import DomUtils
from app.modules.indexer.parser import SiteParserBase, SiteSchema


class GazelleSiteUserInfo(SiteParserBase):
    """Gazelle 站点用户数据解析器。"""

    schema = SiteSchema.Gazelle

    def _parse_logged_in(self, html_text: str) -> bool:
        """
        识别 Gazelle 登录态，兼容不展示退出入口的站点首页。

        :param html_text: 站点首页 HTML
        :return: 已登录返回 True，否则返回 False
        """
        html = etree.HTML(html_text)
        try:
            if DomUtils.has_child_elements(html) and html.xpath(
                '//a[contains(@href, "user.php?id=")]'
            ):
                return True
        finally:
            if html is not None:
                del html

        return super()._parse_logged_in(html_text)

    def _parse_user_base_info(self, html_text: str):
        html_text = self._prepare_html_text(html_text)
        html = etree.HTML(html_text)
        try:
            tmps = html.xpath('//a[contains(@href, "user.php?id=") or contains(@href, "user?id=")]')
            if tmps:
                user_id_match = re.search(r"user(?:\.php)?\?id=(\d+)", tmps[0].attrib['href'])
                if user_id_match and user_id_match.group().strip():
                    self.userid = user_id_match.group(1)
                    self._torrent_seeding_page = f"torrents.php?type=seeding&userid={self.userid}"
                    self._user_detail_page = f"user.php?id={self.userid}"
                    self.username = tmps[0].text.strip()

            tmps = html.xpath('//*[@id="header-uploaded-value"]/@data-value')
            if tmps:
                self.upload = size_tools.parse_size(tmps[0])
            else:
                tmps = html.xpath('//li[@id="stats_seeding"]/span/text()')
                if tmps:
                    self.upload = size_tools.parse_size(tmps[0])

            tmps = html.xpath('//*[@id="header-downloaded-value"]/@data-value')
            if tmps:
                self.download = size_tools.parse_size(tmps[0])
            else:
                tmps = html.xpath('//li[@id="stats_leeching"]/span/text()')
                if tmps:
                    self.download = size_tools.parse_size(tmps[0])

            self.ratio = 0.0 if self.download <= 0.0 else round(self.upload / self.download, 3)

            tmps = html.xpath('//a[contains(@href, "bonus")]/@data-tooltip')
            if tmps:
                bonus_match = re.search(r"([\d,.]+)", tmps[0])
                if bonus_match and bonus_match.group(1).strip():
                    self.bonus = text_tools.parse_float(bonus_match.group(1))
            else:
                tmps = html.xpath('//a[contains(@href, "bonus")]')
                if tmps:
                    bonus_text = tmps[0].xpath("string(.)")
                    bonus_match = re.search(r"([\d,.]+)", bonus_text)
                    if bonus_match and bonus_match.group(1).strip():
                        self.bonus = text_tools.parse_float(bonus_match.group(1))
        finally:
            if html is not None:
                del html

    def _parse_site_page(self, html_text: str):
        pass

    def _parse_user_detail_info(self, html_text: str):
        """
        解析用户额外信息，加入时间，等级
        :param html_text:
        :return:
        """
        html = etree.HTML(html_text)
        try:
            if not DomUtils.has_child_elements(html):
                return None

            # 用户等级
            user_levels_text = html.xpath('//*[@id="class-value"]/@data-value')
            if user_levels_text:
                self.user_level = user_levels_text[0].strip()
            else:
                user_levels_text = html.xpath('//li[contains(text(), "用户等级")]/text()')
                if user_levels_text:
                    self.user_level = user_levels_text[0].split(':')[1].strip()

            # 加入日期
            join_at_text = html.xpath('//*[@id="join-date-value"]/@data-value')
            if join_at_text:
                self.join_at = time_tools.normalize_datetime(join_at_text[0].strip())
            else:
                join_at_text = html.xpath(
                    '//div[contains(@class, "box_userinfo_stats")]//li[contains(text(), "加入时间")]/span/text()')
                if join_at_text:
                    self.join_at = time_tools.normalize_datetime(join_at_text[0].strip())

            # 兼容部分 Gazelle 站点(如 JPopsuki)以文本形式展示上传/下载:
            # <li>Uploaded: 77.44 GB</li> / <li>Downloaded: 8.51 GB</li>
            if not self.upload:
                upload_text = html.xpath(
                    '//li[starts-with(normalize-space(text()), "Uploaded:")]')
                if upload_text:
                    size_match = re.search(
                        r"([\d.,]+\s*[GMKT]?i?B)", upload_text[0].xpath("string(.)"), re.I)
                    if size_match:
                        self.upload = size_tools.parse_size(size_match.group(1))
            if not self.download:
                download_text = html.xpath(
                    '//li[starts-with(normalize-space(text()), "Downloaded:")]')
                if download_text:
                    size_match = re.search(
                        r"([\d.,]+\s*[GMKT]?i?B)", download_text[0].xpath("string(.)"), re.I)
                    if size_match:
                        self.download = size_tools.parse_size(size_match.group(1))
            if not self.ratio and self.upload and self.download:
                self.ratio = round(self.upload / self.download, 3)
        finally:
            if html is not None:
                del html

    def _parse_user_torrent_seeding_info(self, html_text: str, multi_page: Optional[bool] = False) -> Optional[str]:
        """
        做种相关信息
        :param html_text:
        :param multi_page: 是否多页数据
        :return: 下页地址
        """
        html = etree.HTML(html_text)
        try:
            if not DomUtils.has_child_elements(html):
                return None

            size_col = 3
            # 搜索size列
            if html.xpath('//table[contains(@id, "torrent")]//tr[1]/td'):
                size_col = len(html.xpath('//table[contains(@id, "torrent")]//tr[1]/td')) - 3
            # 搜索seeders列
            seeders_col = size_col + 2

            page_seeding = 0
            page_seeding_size = 0
            page_seeding_info = []
            semantic_rows = html.xpath(
                '//table[contains(@id, "torrent")]//tr['
                'contains(concat(" ", normalize-space(@class), " "), " TableTorrent-rowTitle ")]'
            )
            if semantic_rows:
                # GPW 等现代 Gazelle 页面以语义 class 标记列，且标题单元格带 colspan，
                # 固定列序号无法反映视觉列位置，必须逐行按 class 配对读取。
                for row in semantic_rows:
                    size_cells = row.xpath(
                        './td[contains(concat(" ", normalize-space(@class), " "), '
                        '" TableTorrent-cellStatSize ")]'
                    )
                    seeder_cells = row.xpath(
                        './td[contains(concat(" ", normalize-space(@class), " "), '
                        '" TableTorrent-cellStatSeeders ")]'
                    )
                    if not size_cells or not seeder_cells:
                        continue
                    size = size_tools.parse_size(size_cells[0].xpath("string(.)").strip())
                    seeders = text_tools.parse_int(seeder_cells[0].xpath("string(.)").strip())
                    page_seeding += 1
                    page_seeding_size += size
                    page_seeding_info.append([seeders, size])
            else:
                seeding_sizes = html.xpath(f'//table[contains(@id, "torrent")]//tr[position()>1]/td[{size_col}]')
                seeding_seeders = html.xpath(f'//table[contains(@id, "torrent")]//tr[position()>1]/td[{seeders_col}]/text()')
                if seeding_sizes and seeding_seeders:
                    page_seeding = len(seeding_sizes)

                    for i in range(0, len(seeding_sizes)):
                        size = size_tools.parse_size(seeding_sizes[i].xpath("string(.)").strip())
                        seeders = text_tools.parse_int(seeding_seeders[i])

                        page_seeding_size += size
                        page_seeding_info.append([seeders, size])

            if multi_page:
                self.seeding += page_seeding
                self.seeding_size += page_seeding_size
                self.seeding_info.extend(page_seeding_info)
            else:
                if not self.seeding:
                    self.seeding = page_seeding
                if not self.seeding_size:
                    self.seeding_size = page_seeding_size
                if not self.seeding_info:
                    self.seeding_info = page_seeding_info

            # 是否存在下页数据
            next_page = None
            next_page_text = html.xpath('//a[contains(.//text(), "Next") or contains(.//text(), "下一页") or contains(@title, "下一页") or contains(@title, "Next")]/@href')
            if next_page_text:
                next_page = next_page_text[-1].strip()
        finally:
            if html is not None:
                del html

        return next_page

    def _parse_user_traffic_info(self, html_text: str):
        pass

    def _parse_message_unread_links(self, html_text: str, msg_links: list) -> Optional[str]:
        return None

    def _parse_message_content(self, html_text):
        return None, None, None
