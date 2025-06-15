import re
import threading
import uuid
from pathlib import Path
from threading import Event
from typing import Optional, List, Dict, Callable
from urllib.parse import urljoin

import telebot
from telebot import apihelper
from telebot.types import InputFile, InlineKeyboardMarkup, InlineKeyboardButton

from app.core.config import settings
from app.core.context import MediaInfo, Context
from app.core.metainfo import MetaInfo
from app.log import logger
from app.utils.common import retry
from app.utils.http import RequestUtils
from app.utils.string import StringUtils


class Telegram:
    _ds_url = f"http://127.0.0.1:{settings.PORT}/api/v1/message?token={settings.API_TOKEN}"
    _event = Event()
    _bot: telebot.TeleBot = None
    _callback_handlers: Dict[str, Callable] = {}  # 存储回调处理器

    def __init__(self, TELEGRAM_TOKEN: Optional[str] = None, TELEGRAM_CHAT_ID: Optional[str] = None, **kwargs):
        """
        初始化参数
        """
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            logger.error("Telegram配置不完整！")
            return
        # Token
        self._telegram_token = TELEGRAM_TOKEN
        # Chat Id
        self._telegram_chat_id = TELEGRAM_CHAT_ID
        # 初始化机器人
        if self._telegram_token and self._telegram_chat_id:
            # telegram bot api 地址，格式：https://api.telegram.org
            if kwargs.get("API_URL"):
                apihelper.API_URL = urljoin(kwargs["API_URL"], '/bot{0}/{1}')
                apihelper.FILE_URL = urljoin(kwargs["API_URL"], '/file/bot{0}/{1}')
            else:
                apihelper.proxy = settings.PROXY
            # bot
            _bot = telebot.TeleBot(self._telegram_token, parse_mode="Markdown")
            # 记录句柄
            self._bot = _bot
            # 标记渠道来源
            if kwargs.get("name"):
                self._ds_url = f"{self._ds_url}&source={kwargs.get('name')}"

            @_bot.message_handler(commands=['start', 'help'])
            def send_welcome(message):
                _bot.reply_to(message, "温馨提示：直接发送名称或`订阅`+名称，搜索或订阅电影、电视剧")

            @_bot.message_handler(func=lambda message: True)
            def echo_all(message):
                RequestUtils(timeout=15).post_res(self._ds_url, json=message.json)

            @_bot.callback_query_handler(func=lambda call: True)
            def callback_query(call):
                """
                处理按钮点击回调
                """
                try:
                    # 解析回调数据
                    callback_data = call.data
                    user_id = str(call.from_user.id)

                    logger.info(f"收到按钮回调：{callback_data}，用户：{user_id}")

                    # 发送回调数据给主程序处理
                    callback_json = {
                        "callback_query": {
                            "id": call.id,
                            "from": call.from_user.to_dict(),
                            "message": call.message.to_dict(),
                            "data": callback_data
                        }
                    }

                    # 先确认回调，避免用户看到loading状态
                    _bot.answer_callback_query(call.id)

                    # 发送给主程序处理
                    RequestUtils(timeout=15).post_res(self._ds_url, json=callback_json)

                except Exception as e:
                    logger.error(f"处理按钮回调失败：{str(e)}")
                    _bot.answer_callback_query(call.id, "处理失败，请重试")

            def run_polling():
                """
                定义线程函数来运行 infinity_polling
                """
                try:
                    _bot.infinity_polling(long_polling_timeout=30, logger_level=None)
                except Exception as err:
                    logger.error(f"Telegram消息接收服务异常：{str(err)}")

            # 启动线程来运行 infinity_polling
            self._polling_thread = threading.Thread(target=run_polling, daemon=True)
            self._polling_thread.start()
            logger.info("Telegram消息接收服务启动")

    def get_state(self) -> bool:
        """
        获取状态
        """
        return self._bot is not None

    def send_msg(self, title: str, text: Optional[str] = None, image: Optional[str] = None,
                 userid: Optional[str] = None, link: Optional[str] = None,
                 buttons: Optional[List[List[dict]]] = None) -> Optional[bool]:
        """
        发送Telegram消息
        :param title: 消息标题
        :param text: 消息内容
        :param image: 消息图片地址
        :param userid: 用户ID，如有则只发消息给该用户
        :param link: 跳转链接
        :param buttons: 按钮列表，格式：[[{"text": "按钮文本", "callback_data": "回调数据"}]]
        :userid: 发送消息的目标用户ID，为空则发给管理员
        """
        if not self._telegram_token or not self._telegram_chat_id:
            return None

        if not title and not text:
            logger.warn("标题和内容不能同时为空")
            return False

        try:
            if text:
                # 对text进行Markdown特殊字符转义
                text = re.sub(r"([_`])", r"\\\1", text)
                caption = f"*{title}*\n{text}"
            else:
                caption = f"*{title}*"

            if link:
                caption = f"{caption}\n[查看详情]({link})"

            if userid:
                chat_id = userid
            else:
                chat_id = self._telegram_chat_id

            # 创建按钮键盘
            reply_markup = None
            if buttons:
                reply_markup = self._create_inline_keyboard(buttons)

            return self.__send_request(userid=chat_id, image=image, caption=caption, reply_markup=reply_markup)

        except Exception as msg_e:
            logger.error(f"发送消息失败：{msg_e}")
            return False

    def send_medias_msg(self, medias: List[MediaInfo], userid: Optional[str] = None,
                        title: Optional[str] = None, link: Optional[str] = None,
                        buttons: Optional[List[List[Dict]]] = None) -> Optional[bool]:
        """
        发送媒体列表消息
        :param medias: 媒体信息列表
        :param userid: 用户ID，如有则只发消息给该用户
        :param title: 消息标题
        :param link: 跳转链接
        :param buttons: 按钮列表，格式：[[{"text": "按钮文本", "callback_data": "回调数据"}]]
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

            # 创建按钮键盘
            reply_markup = None
            if buttons:
                reply_markup = self._create_inline_keyboard(buttons)

            return self.__send_request(userid=chat_id, image=image, caption=caption, reply_markup=reply_markup)

        except Exception as msg_e:
            logger.error(f"发送消息失败：{msg_e}")
            return False

    def send_torrents_msg(self, torrents: List[Context],
                          userid: Optional[str] = None, title: Optional[str] = None,
                          link: Optional[str] = None, buttons: Optional[List[List[Dict]]] = None) -> Optional[bool]:
        """
        发送列表消息
        :param torrents: Torrent信息列表
        :param userid: 用户ID，如有则只发消息给该用户
        :param title: 消息标题
        :param link: 跳转链接
        :param buttons: 按钮列表，格式：[[{"text": "按钮文本", "callback_data": "回调数据"}]]
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

            # 创建按钮键盘
            reply_markup = None
            if buttons:
                reply_markup = self._create_inline_keyboard(buttons)

            return self.__send_request(userid=chat_id, caption=caption,
                                       image=mediainfo.get_message_image(), reply_markup=reply_markup)

        except Exception as msg_e:
            logger.error(f"发送消息失败：{msg_e}")
            return False

    @staticmethod
    def _create_inline_keyboard(buttons: List[List[Dict]]) -> InlineKeyboardMarkup:
        """
        创建内联键盘
        :param buttons: 按钮配置，格式：[[{"text": "按钮文本", "callback_data": "回调数据", "url": "链接"}]]
        :return: InlineKeyboardMarkup对象
        """
        keyboard = []
        for row in buttons:
            button_row = []
            for button in row:
                if "url" in button:
                    # URL按钮
                    btn = InlineKeyboardButton(text=button["text"], url=button["url"])
                else:
                    # 回调按钮
                    btn = InlineKeyboardButton(text=button["text"], callback_data=button["callback_data"])
                button_row.append(btn)
            keyboard.append(button_row)
        return InlineKeyboardMarkup(keyboard)

    def answer_callback_query(self, callback_query_id: int, text: Optional[str] = None,
                              show_alert: bool = False) -> Optional[bool]:
        """
        回应回调查询
        :param callback_query_id: 回调查询ID
        :param text: 提示文本
        :param show_alert: 是否显示弹窗提示
        :return: 回应结果
        """
        try:
            self._bot.answer_callback_query(callback_query_id, text, show_alert)
            return True
        except Exception as e:
            logger.error(f"回应回调查询失败：{str(e)}")
            return False

    @retry(Exception, logger=logger)
    def __send_request(self, userid: Optional[str] = None, image="", caption="",
                       reply_markup: Optional[InlineKeyboardMarkup] = None) -> bool:
        """
        向Telegram发送报文
        :param reply_markup: 内联键盘
        """
        if image:
            res = RequestUtils(proxies=settings.PROXY).get_res(image)
            if res is None:
                raise Exception("获取图片失败")
            if res.content:
                # 使用随机标识构建图片文件的完整路径，并写入图片内容到文件
                image_file = Path(settings.TEMP_PATH) / "telegram" / str(uuid.uuid4())
                if not image_file.parent.exists():
                    image_file.parent.mkdir(parents=True, exist_ok=True)
                image_file.write_bytes(res.content)
                photo = InputFile(image_file)
                # 发送图片到Telegram
                ret = self._bot.send_photo(chat_id=userid or self._telegram_chat_id,
                                           photo=photo,
                                           caption=caption,
                                           parse_mode="Markdown",
                                           reply_markup=reply_markup)
                if ret is None:
                    raise Exception("发送图片消息失败")
                return True
        # 按4096分段循环发送消息
        ret = None
        if len(caption) > 4095:
            for i in range(0, len(caption), 4095):
                ret = self._bot.send_message(chat_id=userid or self._telegram_chat_id,
                                             text=caption[i:i + 4095],
                                             parse_mode="Markdown",
                                             reply_markup=reply_markup if i == 0 else None)
        else:
            ret = self._bot.send_message(chat_id=userid or self._telegram_chat_id,
                                         text=caption,
                                         parse_mode="Markdown",
                                         reply_markup=reply_markup)
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

    def delete_commands(self):
        """
        清理菜单命令
        """
        if not self._bot:
            return
        # 清理菜单命令
        self._bot.delete_my_commands()

    def stop(self):
        """
        停止Telegram消息接收服务
        """
        if self._bot:
            self._bot.stop_polling()
            self._polling_thread.join()
            logger.info("Telegram消息接收服务已停止")
