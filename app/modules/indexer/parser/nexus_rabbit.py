# -*- coding: utf-8 -*-
import re
import json
from typing import Optional
from lxml import etree
from urllib.parse import urljoin
from app.log import logger
from app.modules.indexer.parser import SiteSchema
from app.modules.indexer.parser import SiteParserBase
from app.utils.string import StringUtils


class NexusRabbitSiteUserInfo(SiteParserBase):
    schema = SiteSchema.NexusRabbit

    def _parse_site_page(self, html_text: str):
        html_text = self._prepare_html_text(html_text)

        user_detail = re.search(r"user.php\?id=(\d+)", html_text)

        if not (user_detail and user_detail.group().strip()):
            return

        self.userid = user_detail.group(1)
        self._user_detail_page = f"user.php?id={self.userid}"

        self._user_traffic_page = None

        self._torrent_seeding_page = "api/general"
        self._torrent_seeding_params = {
            "page": 1,
            "limit": 5000000,
            "action": "userTorrentsList",
            "data": {"type": "seeding", "id": int(self.userid)},
        }
        self._torrent_seeding_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",  # 必须要加上这一条，不然返回的是空数据
        }

        self._user_mail_unread_page = None
        self._sys_mail_unread_page = "api/general"
        self._mail_unread_params = {
            "page": 1,
            "limit": 5000000,
            "action": "getMessageIn",
        }
        self._mail_unread_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
        }

    def _parse_user_torrent_seeding_info(
        self, html_text: str, multi_page: bool = False
    ) -> Optional[str]:
        """
        做种相关信息
        :param html_text:
        :param multi_page: 是否多页数据
        :return: 下页地址
        """

        try:
            torrents = json.loads(html_text).get("data", [])
        except Exception as e:
            logger.error(f"解析做种信息失败: {str(e)}")
            return None

        seeding_size = 0
        seeding_info = []

        for torrent in torrents:
            seeders = int(torrent.get("seeders", 0))
            size = StringUtils.num_filesize(torrent.get("size"))
            seeding_size += size
            seeding_info.append([seeders, size])

        self.seeding = len(torrents)
        self.seeding_size = seeding_size
        self.seeding_info = seeding_info

    def _parse_message_unread_links(
        self, html_text: str, msg_links: list
    ) -> str | None:
        unread_ids = []
        try:
            messages = json.loads(html_text).get("data", [])
        except Exception as e:
            logger.error(f"解析未读消息失败: {e}")
            return None
        for msg in messages:
            msg_id, msg_unread = msg.get("id"), msg.get("unread")
            if not (msg_id and msg_unread) or msg_unread == "no":
                continue
            unread_ids.append(msg_id)
            head, date, content = msg.get("subject"), msg.get("added"), msg.get("msg")
            if head and date and content:
                self.message_unread_contents.append((head, date, content))
        self.message_unread = len(unread_ids)
        if unread_ids:
            self._get_page_content(
                url=urljoin(self._base_url, "api/general?loading=true"),
                params={"action": "readMessage", "data": {"ids": unread_ids}},
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/plain, */*",
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
        return None

    def _parse_user_base_info(self, html_text: str):
        """只有奶糖余额才需要在 base 中获取，其它均可以在详情页拿到"""
        html = etree.HTML(html_text)
        if not StringUtils.is_valid_html_element(html):
            return
        bonus = html.xpath(
            '//div[contains(text(), "奶糖余额")]/following-sibling::div[1]/text()'
        )
        if bonus:
            self.bonus = StringUtils.str_float(bonus[0].strip())

    def _parse_user_detail_info(self, html_text: str):
        html = etree.HTML(html_text)
        if not StringUtils.is_valid_html_element(html):
            return
        # 缩小一下查找范围，所有的信息都在这个 div 里
        user_info = html.xpath('//div[contains(@class, "layui-hares-user-info-right")]')
        if not user_info:
            return
        user_info = user_info[0]
        # 用户名
        if username := user_info.xpath(
            './/span[contains(text(), "用户名")]/a/span/text()'
        ):
            self.username = username[0].strip()
        # 等级
        if user_level := user_info.xpath('.//span[contains(text(), "等级")]/b/text()'):
            self.user_level = user_level[0].strip()
        # 加入日期
        if join_date := user_info.xpath('.//span[contains(text(), "注册日期")]/text()'):
            join_date = join_date[0].strip().split("\r")[0].removeprefix("注册日期：")
            self.join_at = StringUtils.unify_datetime_str(join_date)
        # 上传量
        if upload := user_info.xpath('.//span[contains(text(), "上传量")]/text()'):
            self.upload = StringUtils.num_filesize(
                upload[0].strip().removeprefix("上传量：")
            )
        # 下载量
        if download := user_info.xpath('.//span[contains(text(), "下载量")]/text()'):
            self.download = StringUtils.num_filesize(
                download[0].strip().removeprefix("下载量：")
            )
        # 分享率
        if ratio := user_info.xpath('.//span[contains(text(), "分享率")]/em/text()'):
            self.ratio = StringUtils.str_float(ratio[0].strip())

    def _parse_message_content(self, html_text):
        """
        解析短消息内容，已经在 _parse_message_unread_links 内实现，重载防止 abstractmethod 报错
        :param html_text:
        :return:  head: message, date: time, content: message content
        """
        pass

    def _parse_user_traffic_info(self, html_text: str):
        """
        解析用户的上传，下载，分享率等信息，已经在 _parse_user_detail_info 内实现，重载防止 abstractmethod 报错
        :param html_text:
        :return:
        """
        pass
