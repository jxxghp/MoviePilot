# -*- coding: utf-8 -*-
import re

from lxml import etree

from app.modules.indexer.parser import SiteSchema
from app.modules.indexer.parser.nexus_php import NexusPhpSiteUserInfo
from app.utils.string import StringUtils


class NexusHhanclubSiteUserInfo(NexusPhpSiteUserInfo):
    schema = SiteSchema.NexusHhanclub

    def _parse_user_traffic_info(self, html_text):
        super()._parse_user_traffic_info(html_text)

        html_text = self._prepare_html_text(html_text)
        html = etree.HTML(html_text)

        # 上传、下载、分享率
        upload_match = re.search(r"[_<>/a-zA-Z-=\"'\s#;]+([\d,.\s]+[KMGTPI]*B)",
                                 html.xpath('//*[@id="user-info-panel"]/div[2]/div[2]/div[4]/text()')[0])
        download_match = re.search(r"[_<>/a-zA-Z-=\"'\s#;]+([\d,.\s]+[KMGTPI]*B)",
                                   html.xpath('//*[@id="user-info-panel"]/div[2]/div[2]/div[5]/text()')[0])
        ratio_match = re.search(r"分享率][:：_<>/a-zA-Z-=\"'\s#;]+([\d,.\s]+)",
                                html.xpath('//*[@id="user-info-panel"]/div[2]/div[1]/div[1]/div/text()')[0])

        # 计算分享率
        self.upload = StringUtils.num_filesize(upload_match.group(1).strip()) if upload_match else 0
        self.download = StringUtils.num_filesize(download_match.group(1).strip()) if download_match else 0
        # 优先使用页面上的分享率
        calc_ratio = 0.0 if self.download <= 0.0 else round(self.upload / self.download, 3)
        self.ratio = StringUtils.str_float(ratio_match.group(1)) if (
                ratio_match and ratio_match.group(1).strip()) else calc_ratio

    def _parse_user_detail_info(self, html_text: str):
        """
        解析用户额外信息，加入时间，等级
        :param html_text:
        :return:
        """
        super()._parse_user_detail_info(html_text)

        html = etree.HTML(html_text)
        if not StringUtils.is_valid_html_element(html):
            return
        # 加入时间
        join_at_text = html.xpath('//*[@id="mainContent"]/div/div[2]/div[4]/div[3]/span[2]/text()[1]')
        if join_at_text:
            self.join_at = StringUtils.unify_datetime_str(join_at_text[0].split(' (')[0].strip())

    def _get_user_level(self, html):
        super()._get_user_level(html)
        user_level_path = html.xpath('//*[@id="mainContent"]/div/div[2]/div[2]/div[4]/span[2]/img/@title')
        if user_level_path:
            self.user_level = user_level_path[0]
