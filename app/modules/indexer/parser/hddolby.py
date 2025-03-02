# -*- coding: utf-8 -*-
import json
from typing import Optional, Tuple

from app.modules.indexer.parser import SiteParserBase, SiteSchema
from app.utils.string import StringUtils


class HDDolbySiteUserInfo(SiteParserBase):
    schema = SiteSchema.HDDolby
    request_mode = "apikey"

    # 用户级别字典
    HDDolby_sysRoleList = {
        "0": "Peasant",
        "1": "User",
        "2": "Power User",
        "3": "Elite User",
        "4": "Crazy User",
        "5": "Insane User",
        "6": "Veteran User",
        "7": "Extreme User",
        "8": "Ultimate User",
        "9": "Nexus Master",
        "10": "VIP",
        "11": "Retiree",
        "12": "Helper",
        "13": "Seeder",
        "14": "Transferrer",
        "15": "Uploader",
        "16": "Torrent Manager",
        "17": "Forum Moderator",
        "18": "Coder",
        "19": "Moderator",
        "20": "Administrator",
        "21": "Sysop",
        "22": "Staff Leader",
    }

    def _parse_site_page(self, html_text: str):
        """
        获取站点页面地址
        """
        # 更换api地址
        self._base_url = f"https://api.{StringUtils.get_url_domain(self._base_url)}"
        self._user_traffic_page = None
        self._user_detail_page = None
        self._user_basic_page = "api/v1/user/data"
        self._user_basic_params = {}
        self._user_basic_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*"
        }
        self._sys_mail_unread_page = None
        self._user_mail_unread_page = None
        self._mail_unread_params = {}
        self._torrent_seeding_page = "api/v1/user/peers"
        self._torrent_seeding_params = {}
        self._torrent_seeding_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*"
        }
        self._addition_headers = {
            "x-api-key": self.apikey,
        }

    def _parse_logged_in(self, html_text):
        """
        判断是否登录成功, 通过判断是否存在用户信息
        暂时跳过检测，待后续优化
        :param html_text:
        :return:
        """
        return True

    def _parse_user_base_info(self, html_text: str):
        """
        解析用户基本信息，这里把_parse_user_traffic_info和_parse_user_detail_info合并到这里
        """
        if not html_text:
            return None
        detail = json.loads(html_text)
        if not detail or detail.get("status") != 0:
            return
        user_infos = detail.get("data")
        """
        {
            "id": "1",
            "added": "2019-03-03 15:30:36",
            "last_access": "2025-02-18 19:48:04",
            "class": "22",
            "uploaded": "852071699418375",
            "downloaded": "1885536536176",
            "seedbonus": "99774808.0",
            "sebonus": "3739023.7",
            "unread_messages": "0",
        }
        """
        if not user_infos:
            return
        user_info = user_infos[0]
        self.userid = user_info.get("id")
        self.username = user_info.get("username")
        self.user_level = self.HDDolby_sysRoleList.get(user_info.get("class") or "1")
        self.join_at = user_info.get("added")
        self.upload = int(user_info.get("uploaded") or '0')
        self.download = int(user_info.get("downloaded") or '0')
        self.ratio = round(self.upload / self.download, 2) if self.download else 0
        self.bonus = float(user_info.get("seedbonus") or "0")
        self.message_unread = int(user_info.get("unread_messages") or '0')

    def _parse_user_traffic_info(self, html_text: str):
        """
        解析用户流量信息
        """
        pass

    def _parse_user_detail_info(self, html_text: str):
        """
        解析用户详细信息
        """
        pass

    def _parse_user_torrent_seeding_info(self, html_text: str, multi_page: bool = False) -> Optional[str]:
        """
        解析用户做种信息
        """
        if not html_text:
            return None
        seeding_info = json.loads(html_text)
        if not seeding_info or seeding_info.get("status") != 0:
            return None
        torrents = seeding_info.get("data", [])
        page_seeding_size = 0
        page_seeding_info = []
        for info in torrents:
            size = info.get("size")
            seeder = info.get("seeders") or 1
            page_seeding_size += size
            page_seeding_info.append([seeder, size])
        self.seeding += len(torrents)
        self.seeding_size += page_seeding_size
        self.seeding_info.extend(page_seeding_info)

        return None

    def _parse_message_unread_links(self, html_text: str, msg_links: list) -> Optional[str]:
        """
        解析未读消息链接，这里直接读出详情
        """
        pass

    def _parse_message_content(self, html_text) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """
        解析消息内容
        """
        pass
