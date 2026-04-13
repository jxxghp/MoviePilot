import re
import threading
import base64
from typing import Optional, List, Tuple
from urllib.parse import quote

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
    _file_client = None

    def __init__(self, VOCECHAT_HOST: Optional[str] = None, VOCECHAT_API_KEY: Optional[str] = None, VOCECHAT_CHANNEL_ID: Optional[str] = None, **kwargs):
        """
        初始化
        """
        if not VOCECHAT_HOST or not VOCECHAT_API_KEY or not VOCECHAT_CHANNEL_ID:
            logger.error("VoceChat配置不完整！")
            return
        self._host = VOCECHAT_HOST.strip()
        if self._host and not self._host.startswith("http"):
            self._host = f"http://{self._host}"
        if self._host and not self._host.endswith("/"):
            self._host += "/"
        self._apikey = VOCECHAT_API_KEY
        self._channel_id = VOCECHAT_CHANNEL_ID
        if self._apikey and self._host and self._channel_id:
            self._client = RequestUtils(headers={
                "content-type": "text/markdown",
                "x-api-key": self._apikey,
                "accept": "application/json; charset=utf-8"
            })
            self._file_client = RequestUtils(headers={
                "x-api-key": self._apikey,
                "accept": "*/*"
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

    def send_msg(self, title: str, text: Optional[str] = None,
                 image: Optional[str] = None,
                 userid: Optional[str] = None, link: Optional[str] = None) -> Optional[bool]:
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

            if image:
                caption = f"{caption}\n![]({image})"

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

    @staticmethod
    def _guess_mime_type(content: bytes, default: str = "image/jpeg") -> str:
        if not content:
            return default
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if content.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if content.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if content.startswith(b"BM"):
            return "image/bmp"
        if content.startswith(b"RIFF") and b"WEBP" in content[:16]:
            return "image/webp"
        return default

    def download_file(self, path: str) -> Optional[Tuple[bytes, str]]:
        """
        下载 VoceChat 文件资源
        """
        if not path or not self._file_client:
            return None
        req_url = f"{self._host}api/resource/file?path={quote(path, safe='')}"
        try:
            res = self._file_client.get_res(req_url)
        except Exception as err:
            logger.error(f"VoceChat 文件下载失败：{err}")
            return None
        if not res or not res.content:
            return None
        mime_type = (res.headers.get("Content-Type") or "").split(";")[0].strip()
        return res.content, mime_type or self._guess_mime_type(res.content)

    def download_file_to_data_url(self, path: str) -> Optional[str]:
        file_data = self.download_file(path)
        if not file_data:
            return None
        content, mime_type = file_data
        return f"data:{mime_type};base64,{base64.b64encode(content).decode()}"

    def send_medias_msg(self, title: str, medias: List[MediaInfo],
                        userid: Optional[str] = None, link: Optional[str] = None) -> Optional[bool]:
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
                          userid: Optional[str] = None,
                          title: Optional[str] = None,
                          link: Optional[str] = None) -> Optional[bool]:
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
