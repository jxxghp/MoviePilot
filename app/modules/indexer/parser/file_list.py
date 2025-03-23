# -*- coding: utf-8 -*-
import re
from typing import Optional

from lxml import etree

from app.modules.indexer.parser import SiteParserBase, SiteSchema
from app.utils.string import StringUtils


class FileListSiteUserInfo(SiteParserBase):
    schema = SiteSchema.FileList

    def _parse_site_page(self, html_text: str):
        html_text = self._prepare_html_text(html_text)

        user_detail = re.search(r"userdetails.php\?id=(\d+)", html_text)
        if user_detail and user_detail.group().strip():
            self._user_detail_page = user_detail.group().strip().lstrip('/')
            self.userid = user_detail.group(1)

        self._torrent_seeding_page = f"snatchlist.php?id={self.userid}&action=torrents&type=seeding"

    def _parse_user_base_info(self, html_text: str):
        html_text = self._prepare_html_text(html_text)
        html = etree.HTML(html_text)

        ret = html.xpath(f'//a[contains(@href, "userdetails") and contains(@href, "{self.userid}")]//text()')
        if ret:
            self.username = str(ret[0])

    def _parse_user_traffic_info(self, html_text: str):
        """
        上传/下载/分享率 [做种数/魔力值]
        :param html_text:
        :return:
        """
        return

    def _parse_user_detail_info(self, html_text: str):
        html_text = self._prepare_html_text(html_text)
        html = etree.HTML(html_text)

        upload_html = html.xpath('//table//tr/td[text()="Uploaded"]/following-sibling::td//text()')
        if upload_html:
            self.upload = StringUtils.num_filesize(upload_html[0])
        download_html = html.xpath('//table//tr/td[text()="Downloaded"]/following-sibling::td//text()')
        if download_html:
            self.download = StringUtils.num_filesize(download_html[0])

        ratio_html = html.xpath('//table//tr/td[text()="Share ratio"]/following-sibling::td//text()')
        if ratio_html:
            share_ratio = StringUtils.str_float(ratio_html[0])
        else:
            share_ratio = 0
        self.ratio = 0 if self.download == 0 else share_ratio

        seed_html = html.xpath('//table//tr/td[text()="Seed bonus"]/following-sibling::td//text()')
        if seed_html:
            self.seeding = StringUtils.str_int(seed_html[1])
            self.seeding_size = StringUtils.num_filesize(seed_html[3])

        user_level_html = html.xpath('//table//tr/td[text()="Class"]/following-sibling::td//text()')
        if user_level_html:
            self.user_level = user_level_html[0].strip()

        join_at_html = html.xpath('//table//tr/td[contains(text(), "Join")]/following-sibling::td//text()')
        if join_at_html:
            join_at = (join_at_html[0].split("("))[0].strip()
            self.join_at = StringUtils.unify_datetime_str(join_at)

        bonus_html = html.xpath('//a[contains(@href, "shop.php")]')
        if bonus_html:
            self.bonus = StringUtils.str_float(bonus_html[0].xpath("string(.)").strip())
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
        seeders_col = 7

        page_seeding_size = 0
        page_seeding_info = []
        seeding_sizes = html.xpath(f'//table/tr[position()>1]/td[{size_col}]')
        seeding_seeders = html.xpath(f'//table/tr[position()>1]/td[{seeders_col}]')
        if seeding_sizes and seeding_seeders:
            for i in range(0, len(seeding_sizes)):
                size = StringUtils.num_filesize(seeding_sizes[i].xpath("string(.)").strip())
                seeders = StringUtils.str_int(seeding_seeders[i].xpath("string(.)").strip())

                page_seeding_size += size
                page_seeding_info.append([seeders, size])

        self.seeding_info.extend(page_seeding_info)

        # 是否存在下页数据
        next_page = None

        return next_page

    def _parse_message_unread_links(self, html_text: str, msg_links: list) -> Optional[str]:
        return None

    def _parse_message_content(self, html_text):
        return None, None, None
