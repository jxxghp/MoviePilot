# -*- coding: utf-8 -*-
import re
from typing import Optional

from lxml import etree

from app.modules.indexer.parser import SiteParserBase, SiteSchema
from app.utils.string import StringUtils


class SmallHorseSiteUserInfo(SiteParserBase):
    schema = SiteSchema.SmallHorse

    def _parse_site_page(self, html_text: str):
        html_text = self._prepare_html_text(html_text)

        user_detail = re.search(r"user.php\?id=(\d+)", html_text)
        if user_detail and user_detail.group().strip():
            self._user_detail_page = user_detail.group().strip().lstrip('/')
            self.userid = user_detail.group(1)
            self._torrent_seeding_page = f"torrents.php?type=seeding&userid={self.userid}"
        self._user_traffic_page = f"user.php?id={self.userid}"

    def _parse_user_base_info(self, html_text: str):
        html_text = self._prepare_html_text(html_text)
        html = etree.HTML(html_text)
        ret = html.xpath('//a[contains(@href, "user.php")]//text()')
        if ret:
            self.username = str(ret[0])

    def _parse_user_traffic_info(self, html_text: str):
        """
        上传/下载/分享率 [做种数/魔力值]
        :param html_text:
        :return:
        """
        html_text = self._prepare_html_text(html_text)
        html = etree.HTML(html_text)
        tmps = html.xpath('//ul[@class = "stats nobullet"]')
        if tmps:
            if tmps[1].xpath("li") and tmps[1].xpath("li")[0].xpath("span//text()"):
                self.join_at = StringUtils.unify_datetime_str(tmps[1].xpath("li")[0].xpath("span//text()")[0])
            self.upload = StringUtils.num_filesize(str(tmps[1].xpath("li")[2].xpath("text()")[0]).split(":")[1].strip())
            self.download = StringUtils.num_filesize(
                str(tmps[1].xpath("li")[3].xpath("text()")[0]).split(":")[1].strip())
            if tmps[1].xpath("li")[4].xpath("span//text()"):
                self.ratio = StringUtils.str_float(str(tmps[1].xpath("li")[4].xpath("span//text()")[0]).replace('∞', '0'))
            else:
                self.ratio = StringUtils.str_float(str(tmps[1].xpath("li")[5].xpath("text()")[0]).split(":")[1])
            self.bonus = StringUtils.str_float(str(tmps[1].xpath("li")[5].xpath("text()")[0]).split(":")[1])
            self.user_level = str(tmps[3].xpath("li")[0].xpath("text()")[0]).split(":")[1].strip()
            self.leeching = StringUtils.str_int(
                (tmps[4].xpath("li")[6].xpath("text()")[0]).split(":")[1].replace("[", ""))

    def _parse_user_detail_info(self, html_text: str):
        pass

    def _parse_user_torrent_seeding_info(self, html_text: str, multi_page: Optional[bool] = False) -> Optional[str]:
        """
         做种相关信息
         :param html_text:
         :param multi_page: 是否多页数据
         :return: 下页地址
         """
        html = etree.HTML(html_text)
        if not StringUtils.is_valid_html_element(html):
            return None

        size_col = 6
        seeders_col = 8

        page_seeding = 0
        page_seeding_size = 0
        page_seeding_info = []
        seeding_sizes = html.xpath(f'//table[@id="torrent_table"]//tr[position()>1]/td[{size_col}]')
        seeding_seeders = html.xpath(f'//table[@id="torrent_table"]//tr[position()>1]/td[{seeders_col}]')
        if seeding_sizes and seeding_seeders:
            page_seeding = len(seeding_sizes)

            for i in range(0, len(seeding_sizes)):
                size = StringUtils.num_filesize(seeding_sizes[i].xpath("string(.)").strip())
                seeders = StringUtils.str_int(seeding_seeders[i].xpath("string(.)").strip())

                page_seeding_size += size
                page_seeding_info.append([seeders, size])

        self.seeding += page_seeding
        self.seeding_size += page_seeding_size
        self.seeding_info.extend(page_seeding_info)

        # 是否存在下页数据
        next_page = None
        next_pages = html.xpath('//ul[@class="pagination"]/li[contains(@class,"active")]/following-sibling::li')
        if next_pages and len(next_pages) > 1:
            page_num = next_pages[0].xpath("string(.)").strip()
            if page_num.isdigit():
                next_page = f"{self._torrent_seeding_page}&page={page_num}"

        return next_page

    def _parse_message_unread_links(self, html_text: str, msg_links: list) -> Optional[str]:
        return None

    def _parse_message_content(self, html_text):
        return None, None, None
