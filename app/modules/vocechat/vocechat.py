"""
VoceChat 机器人消息发送与文件下载客户端。
"""

import base64
import re
import threading
from typing import List, Optional, Tuple
from urllib.parse import quote

from app.adapters.network.http import RequestUtils
from app.domain.context import Context, MediaInfo
from app.domain.metainfo import MetaInfo
from app.foundation import size as size_tools
from app.runtime.execution import retry
from app.runtime.log import logger

lock = threading.Lock()


class VoceChat:
    """
    管理 VoceChat 连接配置并向频道或用户发送消息。
    """

    # 服务根地址；连接配置不完整时保持为空。
    _host = None
    # 机器人 API 密钥。
    _apikey = None
    # 频道ID
    _channel_id = None
    _client = None
    _file_client = None

    def __init__(self, VOCECHAT_HOST: Optional[str] = None, VOCECHAT_API_KEY: Optional[str] = None, VOCECHAT_CHANNEL_ID: Optional[str] = None, **kwargs: object) -> None:
        """
        保存 VoceChat 连接配置并创建消息及文件请求客户端。

        任一必需配置缺失时保留未初始化状态。

        :param VOCECHAT_HOST: 服务根地址；省略协议时补充 http://
        :param VOCECHAT_API_KEY: 机器人 API 密钥
        :param VOCECHAT_CHANNEL_ID: 默认接收频道标识
        :param kwargs: 兼容通知配置中的其他字段，当前忽略
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

    def get_state(self) -> bool:
        """
        查询机器人信息，检查服务是否返回有效结果。

        :return: 机器人接口返回非空数据时为 True
        """
        return True if self.get_groups() else False

    def get_groups(self) -> object:
        """
        读取机器人接口返回的频道配置数据。

        :return: 接口原始 JSON 数据；客户端未初始化或请求失败时返回 None
        """
        if not self._client:
            return None
        result = self._client.get_res(f"{self._host}api/bot")
        if result and result.status_code == 200:
            groups: object = result.json()
            return groups
        return None

    def send_msg(self, title: Optional[str], text: Optional[str] = None,
                 image: Optional[str] = None,
                 userid: Optional[str] = None, link: Optional[str] = None) -> Optional[bool]:
        """
        向指定用户或默认频道发送 Markdown 消息。

        :param title: 消息标题，可与正文择一提供
        :param text: 消息正文
        :param image: 可选图片地址
        :param userid: 数字用户标识、UID#用户标识或 GID#频道标识；为空时使用默认频道
        :param link: 可选详情链接
        :return: 发送成功返回 True；客户端未初始化返回 None；内容或发送失败返回 False
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
        """
        按文件头识别支持的图片格式，无法识别时保留调用方默认类型。
        """
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
        使用机器人凭据下载服务内文件。

        优先保留响应的 MIME 类型；缺失时依据图片文件头推断，未知格式默认 image/jpeg。

        :param path: 服务返回的文件路径，按查询参数编码
        :return: 文件字节与 MIME 类型；路径、客户端或下载不可用时返回 None
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
        """
        下载文件并保留检测到的 MIME 类型生成 data URL。

        :param path: 服务内文件路径
        :return: 带 MIME 类型的 Base64 地址；下载失败返回 None
        """
        file_data = self.download_file(path)
        if not file_data:
            return None
        content, mime_type = file_data
        return f"data:{mime_type};base64,{base64.b64encode(content).decode()}"

    def send_medias_msg(self, title: Optional[str], medias: List[MediaInfo],
                        userid: Optional[str] = None, link: Optional[str] = None) -> Optional[bool]:
        """
        向指定对象发送带媒体评分与详情链接的列表。

        :param title: 列表标题
        :param medias: 待展示媒体列表
        :param userid: 数字用户标识、UID#用户标识或 GID#频道标识；为空时使用默认频道
        :param link: 列表末尾的可选详情链接
        :return: 发送成功返回 True；客户端未初始化返回 None；发送失败返回 False
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
                                                             f"类型：{media.type.value if media.type else ''}",
                                                             f"评分：{media.vote_average}")
                else:
                    caption = "%s\n%s. [%s](%s)\n_%s_" % (caption,
                                                          index,
                                                          media.title_year,
                                                          media.detail_link,
                                                          f"类型：{media.type.value if media.type else ''}")
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
        向指定对象发送带站点和资源属性的种子列表。

        :param torrents: 待展示种子上下文列表
        :param userid: 数字用户标识、UID#用户标识或 GID#频道标识；为空时使用默认频道
        :param title: 列表标题
        :param link: 兼容调用参数；当前循环会覆盖为最后一个种子的详情地址
        :return: 发送成功返回 True；客户端未初始化返回 None；空列表或发送失败返回 False
        """
        if not self._client:
            return None

        if not torrents:
            return False

        try:
            index, caption = 1, "**%s**" % title
            for context in torrents:
                torrent = context.torrent_info
                if torrent is None:
                    continue
                site_name = torrent.site_name or ""
                meta = MetaInfo(torrent.title or "", torrent.description)
                link = torrent.page_url
                title = f"{meta.season_episode} " \
                    f"{meta.resource_term} " \
                    f"{meta.video_term} " \
                    f"{meta.release_group}"
                title = re.sub(r"\s+", " ", title).strip()
                free = torrent.volume_factor
                seeder = f"{torrent.seeders}↑"
                caption = f"{caption}\n{index}.【{site_name}】[{title}]({link}) " \
                    f"{size_tools.format_compact_size(torrent.size)} {free} {seeder}"
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
        串行提交消息，连接失败时由重试装饰器重试。

        :param userid: 数字用户标识、UID#用户标识或 GID#频道标识
        :param caption: UTF-8 编码前的 Markdown 正文
        :return: HTTP 200 返回 True；接收人无效或收到失败响应时返回 False
        :raises Exception: 连接失败且重试次数耗尽
        """
        if not self._client:
            return False
        if userid.startswith("GID#"):
            action = "send_to_group"
            idstr = userid[4:]
        elif userid.startswith("UID#"):
            action = "send_to_user"
            idstr = userid[4:]
        elif "#" not in userid:
            action = "send_to_user"
            idstr = userid
        else:
            logger.error(f"VoceChat消息接收对象格式错误：{userid}")
            return False

        if not idstr.isdigit():
            logger.error(f"VoceChat消息接收对象ID无效：{userid}")
            return False

        with lock:
            result = self._client.post_res(f"{self._host}api/bot/{action}/{idstr}", data=caption.encode("utf-8"))
            if result and result.status_code == 200:
                return True
            elif result is not None:
                logger.error(f"VoceChat发送消息失败，错误码：{result.status_code}")
                return False
            else:
                raise Exception("VoceChat发送消息失败，连接失败")
