import json
from typing import Optional, Union, List, Tuple, Any

from app.core.context import MediaInfo, Context
from app.core.config import settings
from app.log import logger
from app.modules import _ModuleBase
from app.modules.telegram.telegram import Telegram


class TelegramModule(_ModuleBase):
    telegram: Telegram = None

    def init_module(self) -> None:
        self.telegram = Telegram()

    def stop(self):
        self.telegram.stop()

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        return "MESSAGER", "telegram"

    def message_parser(self, body: Any, form: Any, args: Any) -> Optional[dict]:
        """
        解析消息内容，返回字典，注意以下约定值：
        userid: 用户ID
        username: 用户名
        text: 内容
        :param body: 请求体
        :param form: 表单
        :param args: 参数
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
        # 校验token
        token = args.get("token")
        if not token or token != settings.API_TOKEN:
            return None
        try:
            message: dict = json.loads(body)
        except Exception as err:
            logger.error(f"解析Telegram消息失败：{err}")
            return None
        if message:
            text = message.get("text")
            user_id = message.get("from", {}).get("id")
            # 获取用户名
            user_name = message.get("from", {}).get("username")
            if text:
                logger.info(f"收到Telegram消息：userid={user_id}, username={user_name}, text={text}")
                # 检查权限
                if text.startswith("/"):
                    if settings.TELEGRAM_ADMINS \
                            and str(user_id) not in settings.TELEGRAM_ADMINS.split(',') \
                            and str(user_id) != settings.TELEGRAM_CHAT_ID:
                        self.telegram.send_msg(title="只有管理员才有权限执行此命令", userid=user_id)
                        return {}
                else:
                    if settings.TELEGRAM_USERS \
                            and not str(user_id) in settings.TELEGRAM_USERS.split(','):
                        logger.info(f"用户{user_id}不在用户白名单中，无法使用此机器人")
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

    def post_torrents_message(self, title: str, items: List[Context],
                              mediainfo: MediaInfo = None,
                              userid: Union[str, int] = None) -> Optional[bool]:
        """
        发送种子信息选择列表
        :param title: 标题
        :param items:  消息列表
        :param mediainfo:  媒体信息
        :param userid:  用户ID
        :return: 成功或失败
        """
        return self.telegram.send_torrents_msg(title=title, torrents=items,
                                               mediainfo=mediainfo, userid=userid)

    def register_commands(self, commands: dict):
        """
        注册命令，实现这个函数接收系统可用的命令菜单
        :param commands: 命令字典
        """
        self.telegram.register_commands(commands)
