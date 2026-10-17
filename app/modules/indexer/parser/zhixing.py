#
# 知行 http://pt.zhixing.bjtu.edu.cn/
# 作者： ThedoRap
# 日期： 2025-10-02
#
# -*- coding: utf-8 -*-
"""
解析知行站点的用户资料、流量和做种信息。
"""

import re
from typing import Optional, Tuple
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.foundation import temporal as time_tools
from app.modules.indexer.parser import SitePage, SiteParserBase, SiteSchema


class ZhixingSiteUserInfo(SiteParserBase):
    """
    解析 Zhixing 站点的用户资料、流量与消息。
    """
    schema = SiteSchema.Zhixing

    def _parse_site_page(self, html_text: SitePage) -> None:
        """
        获取站点页面地址。
        """
        self._user_basic_page = "user/{uid}/"
        self._user_detail_page = None
        self._user_basic_params = {}
        self._user_traffic_page = None
        self._sys_mail_unread_page = None
        self._user_mail_unread_page = None
        self._mail_unread_params = {}
        self._torrent_seeding_base = "user/{uid}/seeding"
        self._torrent_seeding_params = {}
        self._torrent_seeding_headers = {}
        self._addition_headers = {}

    def _parse_logged_in(self, html_text: str) -> bool:
        """
        判断是否登录成功, 通过判断是否存在用户信息。
        """
        soup = BeautifulSoup(html_text, 'html.parser')
        return bool(soup.find(id='um'))

    def _parse_user_base_info(self, html_text: SitePage) -> None:
        """
        解析用户基本信息，这里把_parse_user_traffic_info和_parse_user_detail_info合并到这里。
        """
        if not html_text:
            return None
        soup = BeautifulSoup(html_text, 'html.parser')
        details_tabs = soup.find_all('div', class_='user-details-tabs')
        info_dict: dict[str, str] = {}
        for tab in details_tabs:
            for p in tab.find_all('p'):
                text = p.text.strip()
                if '：' in text:
                    parts = text.split('：', 1)
                elif ':' in text:
                    parts = text.split(':', 1)
                else:
                    continue
                if len(parts) == 2:
                    key = parts[0].strip()
                    value_text = parts[1].strip()
                    value = re.split(r'\s*\(', value_text)[0].strip().split('查看')[0].strip()
                    info_dict[key] = value

        # 保留基础统计，做种分页没有返回条目时用于兜底。
        self._basic_info = info_dict

        self.userid = info_dict.get('UID')
        self.username = info_dict.get('用户名')
        self.user_level = info_dict.get('用户组')
        self.join_at = time_tools.normalize_datetime(info_dict['注册时间']) if '注册时间' in info_dict else None

        def num_filesize_safe(s: Optional[str]) -> int:
            """
            为不带单位的数字补充字节单位，空值保持零容量。
            """
            if s:
                s = s.strip()
                if re.match(r'^\d+(\.\d+)?$', s):
                    s += ' B'
            return self.num_filesize(s) if s else 0

        self.upload = num_filesize_safe(info_dict['上传流量']) if '上传流量' in info_dict else 0
        self.download = num_filesize_safe(info_dict['下载流量']) if '下载流量' in info_dict else 0
        self.ratio = float(info_dict['共享率']) if '共享率' in info_dict else 0
        self.bonus = float(info_dict['保种积分']) if '保种积分' in info_dict else 0.0
        # 暂无消息解析
        self.message_unread = 0

        # 先采用基础资料中的做种统计，随后由分页结果覆盖或作为回退值。
        self.seeding = int(info_dict['当前保种数量']) if '当前保种数量' in info_dict else 0
        self.seeding_size = num_filesize_safe(info_dict['当前保种容量']) if '当前保种容量' in info_dict else 0

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
        解析用户做种信息单页，返回本页数量和大小。
        """
        if not html_text:
            return 0, 0
        soup = BeautifulSoup(html_text, 'html.parser')
        torrents = soup.find_all('tr', id=re.compile(r'^t\d+'))
        page_seeding = 0
        page_seeding_size = 0
        for torrent in torrents:
            size_td = torrent.find('td', class_='r')
            if size_td:
                size_text = size_td.find('a').text if size_td.find('a') else size_td.text.strip()
                page_seeding += 1
                page_seeding_size += self.num_filesize(size_text)
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

    def _parse_user_torrent_seeding_info(self, html_text: str, multi_page: bool = False) -> None:
        """
        做种统计由 parse 按本站页码规则逐页累积，此入口不重复处理。
        """
        pass

    def parse(self) -> None:
        """
        解析站点信息。
        """
        super().parse()
        # 先从首页解析userid
        if self._index_html:
            soup = BeautifulSoup(self._index_html, 'html.parser')
            user_link = soup.find('a', href=re.compile(r'/user/\d+/'))
            if user_link:
                uid_match = re.search(r'/user/(\d+)/', user_link['href'])
                if uid_match:
                    self.userid = uid_match.group(1)
        # 如果有userid，则格式化页面
        if self.userid:
            if self._user_basic_page:
                basic_url = self._user_basic_page.format(uid=self.userid)
                basic_html = self._get_page_content(url=urljoin(self._base_url, basic_url))
                self._parse_user_base_info(basic_html)
            if hasattr(self, '_torrent_seeding_base') and self._torrent_seeding_base:
                # 分页统计从零累积，避免重复计入基础资料中的汇总值。
                self.seeding = 0
                self.seeding_size = 0
                seeding_base = self._torrent_seeding_base.format(uid=self.userid)
                seeding_base_url = urljoin(self._base_url, seeding_base)
                page_num = 1
                while True:
                    seeding_url = f"{seeding_base_url}/p{page_num}"
                    seeding_html = self._get_page_content(url=seeding_url)
                    page_seeding, page_seeding_size = self._parse_user_torrent_seeding_page_info(seeding_html)
                    self.seeding += page_seeding
                    self.seeding_size += page_seeding_size
                    if page_seeding == 0:
                        break
                    page_num += 1
                # 做种分页没有解析到条目时，回退到基础资料中的汇总值。
                if self.seeding == 0 and hasattr(self, '_basic_info'):
                    def num_filesize_safe(s: Optional[str]) -> int:
                        """
                        为不带单位的数字补充字节单位，空值保持零容量。
                        """
                        if s:
                            s = s.strip()
                            if re.match(r'^\d+(\.\d+)?$', s):
                                s += ' B'
                        return self.num_filesize(s) if s else 0
                    self.seeding = int(self._basic_info.get('当前保种数量', 0))
                    self.seeding_size = num_filesize_safe(self._basic_info.get('当前保种容量', ''))

        # 文本字段归一为空字符串；流量、数量和比例保留基类约定的数值类型。
        self.userid = str(self.userid or "")
        self.username = str(self.username or "")
        self.user_level = str(self.user_level or "")
        self.join_at = str(self.join_at or "")
