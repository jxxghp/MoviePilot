#
# 极速之星 https://bitpt.cn/
# 作者： ThedoRap
# 日期： 2025-10-02
#
# -*- coding: utf-8 -*-
"""
解析极速之星站点的用户资料、流量和做种信息。
"""

import re
from typing import Optional, Tuple
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup

from app.foundation import size as size_tools
from app.foundation import temporal as time_tools
from app.modules.indexer.parser import SitePage, SiteParserBase, SiteSchema


class BitptSiteUserInfo(SiteParserBase):
    """
    解析 Bitpt 站点的用户资料、流量与消息。
    """
    schema = SiteSchema.Bitpt

    def _parse_site_page(self, html_text: SitePage) -> None:
        """
        识别并配置站点数据页面与请求参数。
        """
        self._user_basic_page = "userdetails.php?uid={uid}"
        self._user_detail_page = None
        self._user_basic_params = {}
        self._user_traffic_page = None
        self._sys_mail_unread_page = None
        self._user_mail_unread_page = None
        self._mail_unread_params = {}
        self._torrent_seeding_base = "browse.php"
        self._torrent_seeding_params = {"t": "myseed", "st": "2", "d": "desc"}
        self._torrent_seeding_headers = {}
        self._addition_headers = {}

    def _parse_logged_in(self, html_text: str) -> bool:
        """
        根据当前页面的用户入口判断登录状态。
        """
        soup = BeautifulSoup(html_text, 'html.parser')
        return bool(soup.find(id='userinfotop'))

    def _parse_user_base_info(self, html_text: SitePage) -> None:
        """
        解析站点用户身份和页面提供的基础统计。
        """
        if not html_text:
            return None
        soup = BeautifulSoup(html_text, 'html.parser')
        table = soup.find('table', class_='frmtable')
        if not table:
            return

        rows = table.find_all('tr')
        info_dict: dict[str, str] = {}
        for row in rows:
            cells = row.find_all('td')
            if len(cells) == 2:
                key = cells[0].text.strip()
                value = cells[1].text.strip()
                info_dict[key] = value

        self.userid = info_dict.get('UID')
        self.username = info_dict['用户名'].split('\xa0')[0] if '用户名' in info_dict else None
        self.user_level = info_dict['用户级别'] if '用户级别' in info_dict else None
        self.join_at = time_tools.normalize_datetime(info_dict['注册时间']) if '注册时间' in info_dict else None

        self.upload = size_tools.parse_size(info_dict['上传流量']) if '上传流量' in info_dict else 0
        self.download = size_tools.parse_size(info_dict['下载流量']) if '下载流量' in info_dict else 0
        self.ratio = float(info_dict['共享率']) if '共享率' in info_dict else 0
        bonus_str = info_dict.get('星辰', '')
        bonus_match = re.search(r'累计([\d\.]+)', bonus_str)
        self.bonus = float(bonus_match.group(1)) if bonus_match else 0
        self.message_unread = 0

        if hasattr(self, '_torrent_seeding_base') and self._torrent_seeding_base:
            self.seeding = 0
            self.seeding_size = 0
        else:
            seeding_info = soup.find('div', style="margin:0 auto;width:90%;font-size:14px;margin-top:10px;margin-bottom:10px;text-align:center;")
            if seeding_info:
                seeding_link = seeding_info.find_all('a')[1].text if len(seeding_info.find_all('a')) > 1 else ''
                match = re.search(r'当前上传的种子\((\d+)个, 共([\d\.]+ [KMGT]B)\)', seeding_link)
                if match:
                    self.seeding = int(match.group(1))
                    self.seeding_size = size_tools.parse_size(match.group(2))
                else:
                    self.seeding = 0
                    self.seeding_size = 0

    def _parse_user_traffic_info(self, html_text: str) -> None:
        """
        该站点在其他解析入口处理此数据，本入口不追加操作。
        """
        pass

    def _parse_user_detail_info(self, html_text: str) -> None:
        """
        该站点在其他解析入口处理此数据，本入口不追加操作。
        """
        pass

    def _parse_user_torrent_seeding_page_info(self, html_text: str) -> Tuple[int, int]:
        """
        统计当前页做种条目数及其真实容量。
        """
        if not html_text:
            return 0, 0
        soup = BeautifulSoup(html_text, 'html.parser')
        torrent_table = soup.find('table', class_='torrenttable')
        if not torrent_table:
            return 0, 0
        rows = torrent_table.find_all('tr')
        if len(rows) <= 1:
            return 0, 0
        torrents = [row for row in rows[1:] if 'btr' in row.get('class', [])]
        page_seeding = 0
        page_seeding_size = 0
        for torrent in torrents:
            size_td = torrent.find('td', class_='r')
            if size_td:
                size_a = size_td.find('a')
                size_text = size_a.text.strip() if size_a else size_td.text.strip()
                if size_text:
                    page_seeding += 1
                    page_seeding_size += size_tools.parse_size(size_text)
        return page_seeding, page_seeding_size

    def _parse_message_unread_links(self, html_text: str, msg_links: list[str]) -> Optional[str]:
        """
        该站点在其他解析入口处理此数据，本入口不追加操作。
        """
        pass

    def _parse_message_content(self, html_text: str) -> None:
        """
        该站点在其他解析入口处理此数据，本入口不追加操作。
        """
        pass

    def _parse_user_torrent_seeding_info(self, html_text: str, multi_page: bool = False) -> Optional[str]:
        """
        该站点在其他解析入口处理此数据，本入口不追加操作。
        """
        pass

    def parse(self) -> None:
        """
        执行通用解析后补齐本站页面数据，保留原有对外字段格式。
        """
        super().parse()
        if self._index_html:
            soup = BeautifulSoup(self._index_html, 'html.parser')
            user_link = soup.find('a', href=re.compile(r'userdetails\.php\?uid=\d+'))
            if user_link:
                uid_match = re.search(r'uid=(\d+)', user_link['href'])
                if uid_match:
                    self.userid = uid_match.group(1)

        if self.userid and self._user_basic_page:
            basic_url = self._user_basic_page.format(uid=self.userid)
            basic_html = self._get_page_content(url=urljoin(self._base_url, basic_url))
            self._parse_user_base_info(basic_html)

        if hasattr(self, '_torrent_seeding_base') and self._torrent_seeding_base:
            seeding_base_url = urljoin(self._base_url, self._torrent_seeding_base)
            if self._torrent_seeding_params is None:
                raise AttributeError("'NoneType' object has no attribute 'copy'")
            params = self._torrent_seeding_params.copy()
            page_num = 1
            while True:
                params['p'] = page_num
                query_string = urlencode(params)
                full_url = f"{seeding_base_url}?{query_string}"
                seeding_html = self._get_page_content(url=full_url)
                page_seeding, page_seeding_size = self._parse_user_torrent_seeding_page_info(seeding_html)
                self.seeding += page_seeding
                self.seeding_size += page_seeding_size
                if page_seeding == 0:
                    break
                page_num += 1

        # 文本字段归一为空字符串；流量、数量和比例保留基类约定的数值类型。
        self.userid = str(self.userid or "")
        self.username = str(self.username or "")
        self.user_level = str(self.user_level or "")
        self.join_at = str(self.join_at or "")
