from typing import Optional, Union, List, Tuple

from fastapi import Request

from app.core import MediaInfo, TorrentInfo, settings
from app.log import logger
from app.modules import _ModuleBase
from app.modules.telegram.telegram import Telegram


class TelegramModule(_ModuleBase):

    telegram: Telegram = None

    def init_module(self) -> None:
        self.telegram = Telegram()

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        return "MESSAGER", "telegram"

    async def message_parser(self, request: Request) -> Optional[dict]:
        """
        解析消息内容，返回字典，注意以下约定值：
        userid: 用户ID
        username: 用户名
        text: 内容
        :param request:  请求体
        :return: 消息内容、用户ID
        """
        """
            {
                'update_id': ,
                'message': {
                    'message_id': ,
                    'from': {
                        'id': ,
                        'is_bot': False,
                        'first_name': '',
                        'username': '',
                        'language_code': 'zh-hans'
                    },
                    'chat': {
                        'id': ,
                        'first_name': '',
                        'username': '',
                        'type': 'private'
                    },
                    'date': ,
                    'text': ''
                }
            }
        """
        msg_json: dict = await request.json()
        if msg_json:
            message = msg_json.get("message", {})
            text = message.get("text")
            user_id = message.get("from", {}).get("id")
            # 获取用户名
            user_name = message.get("from", {}).get("username")
            if text:
                logger.info(f"收到Telegram消息：userid={user_id}, username={user_name}, text={text}")
                # 检查权限
                if text.startswith("/"):
                    if str(user_id) not in settings.TELEGRAM_ADMINS.split(',') \
                            and str(user_id) != settings.TELEGRAM_CHAT_ID:
                        self.telegram.send_msg(title="只有管理员才有权限执行此命令", userid=user_id)
                        return {}
                else:
                    if not str(user_id) in settings.TELEGRAM_USERS.split(','):
                        self.telegram.send_msg(title="你不在用户白名单中，无法使用此机器人", userid=user_id)
                        return {}
                return {
                    "userid": user_id,
                    "username": user_name,
                    "text": text
                }
        return None

    def post_message(self, title: str,
                     text: str = None, image: str = None, userid: Union[str, int] = None) -> Optional[bool]:
        """
        发送消息
        :param title:  标题
        :param text: 内容
        :param image: 图片
        :param userid:  用户ID
        :return: 成功或失败
        """
        return self.telegram.send_msg(title=title, text=text, image=image, userid=userid)

    def post_medias_message(self, title: str, items: List[MediaInfo],
                            userid: Union[str, int] = None) -> Optional[bool]:
        """
        发送媒体信息选择列表
        :param title:  标题
        :param items:  消息列表
        :param userid:  用户ID
        :return: 成功或失败
        """
        return self.telegram.send_meidas_msg(title=title, medias=items, userid=userid)

    def post_torrents_message(self, title: str, items: List[TorrentInfo],
                              userid: Union[str, int] = None) -> Optional[bool]:
        """
        发送种子信息选择列表
        :param title: 标题
        :param items:  消息列表
        :param userid:  用户ID
        :return: 成功或失败
        """
        return self.telegram.send_torrents_msg(title=title, torrents=items, userid=userid)
