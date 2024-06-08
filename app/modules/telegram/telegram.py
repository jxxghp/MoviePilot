import re
import threading
from pathlib import Path
from threading import Event
from typing import Optional, List, Dict

import telebot
from telebot import apihelper
from telebot.types import InputFile

from app.core.config import settings
from app.core.context import MediaInfo, Context
from app.core.metainfo import MetaInfo
from app.log import logger
from app.utils.common import retry
from app.utils.http import RequestUtils
from app.utils.string import StringUtils

apihelper.proxy = settings.PROXY


class Telegram:
    _ds_url = f"http://127.0.0.1:{settings.PORT}/api/v1/message?token={settings.API_TOKEN}"
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
            _bot = telebot.TeleBot(self._telegram_token, parse_mode="Markdown")
            # 记录句柄
            self._bot = _bot

            @_bot.message_handler(commands=['start', 'help'])
            def send_welcome(message):
                _bot.reply_to(message, "温馨提示：直接发送名称或`订阅`+名称，搜索或订阅电影、电视剧")

            @_bot.message_handler(func=lambda message: True)
            def echo_all(message):
                RequestUtils(timeout=5).post_res(self._ds_url, json=message.json)

            def run_polling():
                """
                定义线程函数来运行 infinity_polling
                """
                try:
                    _bot.infinity_polling(long_polling_timeout=30, logger_level=None)
                except Exception as err:
                    logger.error(f"Telegram消息接收服务异常：{str(err)}")

            # 启动线程来运行 infinity_polling
            self._polling_thread = threading.Thread(target=run_polling)
            self._polling_thread.start()
            logger.info("Telegram消息接收服务启动")

    def get_state(self) -> bool:
        """
        获取状态
        """
        return self._bot is not None

    def send_msg(self, title: str, text: str = "", image: str = "",
                 userid: str = "", link: str = None) -> Optional[bool]:
        """
        发送Telegram消息
        :param title: 消息标题
        :param text: 消息内容
        :param image: 消息图片地址
        :param userid: 用户ID，如有则只发消息给该用户
        :param link: 跳转链接
        :userid: 发送消息的目标用户ID，为空则发给管理员
        """
        if not self._telegram_token or not self._telegram_chat_id:
            return None

        if not title and not text:
            logger.warn("标题和内容不能同时为空")
            return False

        try:
            if text:
                caption = f"*{title}*\n{text}"
            else:
                caption = f"*{title}*"

            if link:
                caption = f"{caption}\n[查看详情]({link})"

            if userid:
                chat_id = userid
            else:
                chat_id = self._telegram_chat_id

            return self.__send_request(userid=chat_id, image=image, caption=caption)

        except Exception as msg_e:
            logger.error(f"发送消息失败：{msg_e}")
            return False

    def send_meidas_msg(self, medias: List[MediaInfo], userid: str = "",
                        title: str = "", link: str = None) -> Optional[bool]:
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
                chat_id = self._telegram_chat_id

            return self.__send_request(userid=chat_id, image=image, caption=caption)

        except Exception as msg_e:
            logger.error(f"发送消息失败：{msg_e}")
            return False

    def send_torrents_msg(self, torrents: List[Context],
                          userid: str = "", title: str = "", link: str = None) -> Optional[bool]:
        """
        发送列表消息
        """
        if not self._telegram_token or not self._telegram_chat_id:
            return None

        if not torrents:
            return False

        try:
            index, caption = 1, "*%s*" % title
            mediainfo = torrents[0].media_info
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
                chat_id = self._telegram_chat_id

            return self.__send_request(userid=chat_id, caption=caption,
                                       image=mediainfo.get_message_image())

        except Exception as msg_e:
            logger.error(f"发送消息失败：{msg_e}")
            return False

    @retry(Exception, logger=logger)
    def __send_request(self, userid: str = None, image="", caption="") -> bool:
        """
        向Telegram发送报文
        """

        if image:
            req = RequestUtils(proxies=settings.PROXY).get_res(image)
            if req is None:
                raise Exception("获取图片失败")
            if req.content:
                image_file = Path(settings.TEMP_PATH) / Path(image).name
                image_file.write_bytes(req.content)
                photo = InputFile(image_file)
                ret = self._bot.send_photo(chat_id=userid or self._telegram_chat_id,
                                           photo=photo,
                                           caption=caption,
                                           parse_mode="Markdown")
                if ret is None:
                    raise Exception("发送图片消息失败")
                if ret:
                    return True
        # 按4096分段循环发送消息
        ret = None
        if len(caption) > 4095:
            for i in range(0, len(caption), 4095):
                ret = self._bot.send_message(chat_id=userid or self._telegram_chat_id,
                                             text=caption[i:i + 4095],
                                             parse_mode="Markdown")
        else:
            ret = self._bot.send_message(chat_id=userid or self._telegram_chat_id,
                                         text=caption,
                                         parse_mode="Markdown")
        if ret is None:
            raise Exception("发送文本消息失败")
        return True if ret else False

    def register_commands(self, commands: Dict[str, dict]):
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
        if self._bot:
            self._bot.stop_polling()
            self._polling_thread.join()
            logger.info("Telegram消息接收服务已停止")
