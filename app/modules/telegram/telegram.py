import threading
from threading import Event
from typing import Optional, List

import telebot

from app.core import settings, MediaInfo, Context
from app.log import logger
from app.utils.http import RequestUtils
from app.utils.singleton import Singleton
from app.utils.string import StringUtils


class Telegram(metaclass=Singleton):
    _ds_url = f"http://127.0.0.1:{settings.PORT}/api/v1/messages?token={settings.API_TOKEN}"
    _event = Event()
    _bot: telebot.TeleBot = None

    def __init__(self):
        """
        初始化参数
        """
        # Token
        self._telegram_token = settings.TELEGRAM_TOKEN
        # Chat Id
        self._telegram_chat_id = settings.TELEGRAM_CHAT_ID
        # 初始化机器人
        if self._telegram_token and self._telegram_chat_id:
            # bot
            _bot = telebot.TeleBot(self._telegram_token, parse_mode="markdown")
            # 记录句柄
            self._bot = _bot

            @_bot.message_handler(func=lambda message: True)
            def echo_all(message):
                RequestUtils(timeout=10).post_res(self._ds_url, json=message.json)

        def run_polling():
            """
            定义线程函数来运行 infinity_polling
            """
            _bot.infinity_polling()

        # 启动线程来运行 infinity_polling
        self._polling_thread = threading.Thread(target=run_polling)
        self._polling_thread.start()

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
                text = StringUtils.escape_markdown(text)
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

        if image:
            ret = self._bot.send_photo(chat_id=self._telegram_chat_id,
                                       photo=image,
                                       caption=caption,
                                       parse_mode="markdown")
        else:
            ret = self._bot.send_message(chat_id=self._telegram_chat_id,
                                         text=caption,
                                         parse_mode="markdown")

        return True if ret else False

    def register_commands(self, commands: dict):
        """
        注册菜单命令
        """
        if not self._bot:
            return
        # 设置bot命令
        if commands:
            self._bot.delete_my_commands()
            self._bot.set_my_commands(
                commands=[
                    telebot.types.BotCommand(cmd[1:], str(desc.get("description"))) for cmd, desc in
                    commands.items()
                ]
            )

    def stop(self):
        """
        停止Telegram消息接收服务
        """
        self._bot.stop_polling()
        self._polling_thread.join()
