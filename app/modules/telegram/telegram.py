from threading import Event, Thread
from typing import Optional, List
from urllib.parse import urlencode

from app.core import settings, MediaInfo, TorrentInfo, Context
from app.log import logger
from app.utils.http import RequestUtils
from app.utils.singleton import Singleton


class Telegram(metaclass=Singleton):

    _poll_timeout: int = 5
    _event = Event()

    def __init__(self):
        """
        初始化参数
        """
        # Token
        self._telegram_token = settings.TELEGRAM_TOKEN
        # Chat Id
        self._telegram_chat_id = settings.TELEGRAM_CHAT_ID
        # 用户Chat Id列表
        self._telegram_user_ids = settings.TELEGRAM_USERS.split(",")
        # 管理员Chat Id列表
        self._telegram_admin_ids = settings.TELEGRAM_ADMINS.split(",")
        # 消息轮循
        if self._telegram_token and self._telegram_chat_id:
            self._thread = Thread(target=self.__start_telegram_message_proxy)
            self._thread.start()

    def send_msg(self, title: str, text: str = "", image: str = "", userid: str = "") -> Optional[bool]:
        """
        发送Telegram消息
        :param title: 消息标题
        :param text: 消息内容
        :param image: 消息图片地址
        :param userid: 用户ID，如有则只发消息给该用户
        :userid: 发送消息的目标用户ID，为空则发给管理员
        """
        if not self._telegram_token or not self._telegram_chat_id:
            return None

        if not title and not text:
            logger.warn("标题和内容不能同时为空")
            return False

        try:
            if text:
                # text中的Markdown特殊字符转义
                text = text.replace("[", r"\[").replace("_", r"\_").replace("*", r"\*").replace("`", r"\`").replace("\n\n", "\n")
                caption = f"*{title}*\n{text}"
            else:
                caption = title

            if userid:
                chat_id = userid
            else:
                chat_id = self._telegram_chat_id

            return self.__send_request(chat_id=chat_id, image=image, caption=caption)

        except Exception as msg_e:
            logger.error(f"发送消息失败：{msg_e}")
            return False

    def send_meidas_msg(self, medias: List[MediaInfo], userid: str = "", title: str = "") -> Optional[bool]:
        """
        发送媒体列表消息
        """
        if not self._telegram_token or not self._telegram_chat_id:
            return None

        try:
            index, image, caption = 1, "", "*%s*" % title
            for media in medias:
                if not image:
                    image = media.get_message_image()
                if media.vote_average:
                    caption = "%s\n%s. [%s](%s)\n%s，%s" % (caption,
                                                           index,
                                                           media.get_title_string(),
                                                           media.get_detail_url(),
                                                           f"类型：{media.type.value}",
                                                           f"评分：{media.vote_average}")
                else:
                    caption = "%s\n%s. [%s](%s)\n%s" % (caption,
                                                        index,
                                                        media.get_title_string(),
                                                        media.get_detail_url(),
                                                        f"类型：{media.type.value}")
                index += 1

            if userid:
                chat_id = userid
            else:
                chat_id = self._telegram_chat_id

            return self.__send_request(chat_id=chat_id, image=image, caption=caption)

        except Exception as msg_e:
            logger.error(f"发送消息失败：{msg_e}")
            return False

    def send_torrents_msg(self, torrents: List[Context], userid: str = "", title: str = "") -> Optional[bool]:
        """
        发送列表消息
        """
        if not self._telegram_token or not self._telegram_chat_id:
            return None

        try:
            index, caption = 1, "*%s*" % title
            for context in torrents:
                torrent = context.torrent_info
                link = torrent.page_url
                title = torrent.title
                free = torrent.get_volume_factor_string()
                seeder = f"{torrent.seeders}↑"
                description = torrent.description
                caption = f"{caption}\n{index}. [{title}]({link}) {free} {seeder}\n{description}"
                index += 1

            if userid:
                chat_id = userid
            else:
                chat_id = self._telegram_chat_id

            return self.__send_request(chat_id=chat_id, caption=caption)

        except Exception as msg_e:
            logger.error(f"发送消息失败：{msg_e}")
            return False

    def __send_request(self, chat_id="", image="", caption="") -> bool:
        """
        向Telegram发送报文
        """

        def __res_parse(result):
            if result and result.status_code == 200:
                ret_json = result.json()
                status = ret_json.get("ok")
                if status:
                    return True
                else:
                    logger.error(
                        f"发送消息错误，错误码：{ret_json.get('error_code')}，错误原因：{ret_json.get('description')}")
                    return False
            elif result is not None:
                logger.error(f"发送消息错误，错误码：{result.status_code}，错误原因：{result.reason}")
                return False
            else:
                logger.error("发送消息错误，未知错误")
                return False

        # 请求
        request = RequestUtils(proxies=settings.PROXY)

        # 发送图文消息
        if image:
            photo_req = request.get_res(image)
            if photo_req and photo_req.content:
                res = request.post_res("https://api.telegram.org/bot%s/sendPhoto" % self._telegram_token,
                                       data={"chat_id": chat_id, "caption": caption, "parse_mode": "Markdown"},
                                       files={"photo": photo_req.content})
                if __res_parse(res):
                    return True
        # 发送文本消息
        res = request.get_res("https://api.telegram.org/bot%s/sendMessage?" % self._telegram_token + urlencode(
            {"chat_id": chat_id, "text": caption, "parse_mode": "Markdown"}))
        return __res_parse(res)

    def __start_telegram_message_proxy(self):
        logger.info("Telegram消息接收服务启动")

        def consume_messages(_offset: int, _sc_url: str, _ds_url: str) -> int:
            try:
                res = RequestUtils(proxies=settings.PROXY).get_res(
                    _sc_url + urlencode({"timeout": self._poll_timeout, "offset": _offset}))
                if res and res.json():
                    for msg in res.json().get("result", []):
                        # 无论本地是否成功，先更新offset，即消息最多成功消费一次
                        _offset = msg["update_id"] + 1
                        logger.debug("Telegram接收到消息: %s" % msg)
                        local_res = RequestUtils(timeout=10).post_res(_ds_url, json=msg)
                        logger.debug("Telegram message: %s processed, response is: %s" % (msg, local_res.text))
            except Exception as e:
                logger.error("Telegram 消息接收出现错误: %s" % e)
            return _offset

        offset = 0

        while True:
            if self._event.is_set():
                logger.info("Telegram消息接收服务已停止")
                break
            index = 0
            while index < 20 and not self._event.is_set():
                offset = consume_messages(_offset=offset,
                                          _sc_url="https://api.telegram.org/bot%s/getUpdates?" % self._telegram_token,
                                          _ds_url="http://127.0.0.1:%s/api/v1/messages?token=%s" % (
                                              settings.PORT, settings.API_TOKEN))
                index += 1

    def stop(self):
        """
        停止Telegram消息接收服务
        """
        self._event.set()
