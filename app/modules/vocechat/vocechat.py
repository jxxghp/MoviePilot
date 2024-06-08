import re
import threading
from typing import Optional, List

from app.core.config import settings
from app.core.context import MediaInfo, Context
from app.core.metainfo import MetaInfo
from app.log import logger
from app.utils.common import retry
from app.utils.http import RequestUtils
from app.utils.string import StringUtils

lock = threading.Lock()


class VoceChat:
    # host
    _host = None
    # apikey
    _apikey = None
    # 频道ID
    _channel_id = None
    # 请求对象
    _client = None

    def __init__(self):
        """
        初始化
        """
        self._host = settings.VOCECHAT_HOST
        if self._host:
            if not self._host.endswith("/"):
                self._host += "/"
            if not self._host.startswith("http"):
                self._playhost = "http://" + self._host
        self._apikey = settings.VOCECHAT_API_KEY
        self._channel_id = settings.VOCECHAT_CHANNEL_ID
        if self._apikey and self._host and self._channel_id:
            self._client = RequestUtils(headers={
                "content-type": "text/markdown",
                "x-api-key": self._apikey,
                "accept": "application/json; charset=utf-8"
            })

    def get_state(self):
        """
        获取状态
        """
        return True if self.get_groups() else False

    def get_groups(self):
        """
        获取频道列表
        """
        if not self._client:
            return None
        result = self._client.get_res(f"{self._host}api/bot")
        if result and result.status_code == 200:
            return result.json()

    def send_msg(self, title: str, text: str = "",
                 userid: str = None, link: str = None) -> Optional[bool]:
        """
        微信消息发送入口，支持文本、图片、链接跳转、指定发送对象
        :param title: 消息标题
        :param text: 消息内容
        :param userid: 消息发送对象的ID，为空则发给所有人
        :param link: 消息链接
        :return: 发送状态，错误信息
        """
        if not self._client:
            return None

        if not title and not text:
            logger.warn("标题和内容不能同时为空")
            return False

        try:
            if text:
                caption = f"**{title}**\n{text}"
            else:
                caption = f"**{title}**"

            if link:
                caption = f"{caption}\n[查看详情]({link})"

            if userid:
                chat_id = userid
            else:
                chat_id = f"GID#{self._channel_id}"

            return self.__send_request(userid=chat_id, caption=caption)

        except Exception as msg_e:
            logger.error(f"发送消息失败：{msg_e}")
            return False

    def send_medias_msg(self, title: str, medias: List[MediaInfo],
                        userid: str = "", link: str = None) -> Optional[bool]:
        """
        发送列表类消息
        """
        if not self._client:
            return None

        try:
            index, caption = 1, "**%s**" % title
            for media in medias:
                if media.vote_average:
                    caption = "%s\n%s. [%s](%s)\n_%s，%s_" % (caption,
                                                             index,
                                                             media.title_year,
                                                             media.detail_link,
                                                             f"类型：{media.type.value}",
                                                             f"评分：{media.vote_average}")
                else:
                    caption = "%s\n%s. [%s](%s)\n_%s_" % (caption,
                                                          index,
                                                          media.title_year,
                                                          media.detail_link,
                                                          f"类型：{media.type.value}")
                index += 1

            if link:
                caption = f"{caption}\n[查看详情]({link})"

            if userid:
                chat_id = userid
            else:
                chat_id = f"GID#{self._channel_id}"

            return self.__send_request(userid=chat_id, caption=caption)

        except Exception as msg_e:
            logger.error(f"发送消息失败：{msg_e}")
            return False

    def send_torrents_msg(self, torrents: List[Context],
                          userid: str = "", title: str = "", link: str = None) -> Optional[bool]:
        """
        发送列表消息
        """
        if not self._client:
            return None

        if not torrents:
            return False

        try:
            index, caption = 1, "**%s**" % title
            for context in torrents:
                torrent = context.torrent_info
                site_name = torrent.site_name
                meta = MetaInfo(torrent.title, torrent.description)
                link = torrent.page_url
                title = f"{meta.season_episode} " \
                        f"{meta.resource_term} " \
                        f"{meta.video_term} " \
                        f"{meta.release_group}"
                title = re.sub(r"\s+", " ", title).strip()
                free = torrent.volume_factor
                seeder = f"{torrent.seeders}↑"
                caption = f"{caption}\n{index}.【{site_name}】[{title}]({link}) " \
                          f"{StringUtils.str_filesize(torrent.size)} {free} {seeder}"
                index += 1

            if link:
                caption = f"{caption}\n[查看详情]({link})"

            if userid:
                chat_id = userid
            else:
                chat_id = f"GID#{self._channel_id}"

            return self.__send_request(userid=chat_id, caption=caption)

        except Exception as msg_e:
            logger.error(f"发送消息失败：{msg_e}")
            return False

    @retry(Exception, logger=logger)
    def __send_request(self, userid: str, caption: str) -> bool:
        """
        向VoceChat发送报文
        userid格式：UID#xxx / GID#xxx
        """
        if not self._client:
            return False
        if userid.startswith("GID#"):
            action = "send_to_group"
        else:
            action = "send_to_user"
        idstr = userid[4:]

        with lock:
            result = self._client.post_res(f"{self._host}api/bot/{action}/{idstr}", data=caption.encode("utf-8"))
            if result and result.status_code == 200:
                return True
            elif result is not None:
                logger.error(f"VoceChat发送消息失败，错误码：{result.status_code}")
                return False
            else:
                raise Exception("VoceChat发送消息失败，连接失败")
