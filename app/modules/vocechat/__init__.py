import json
from typing import Optional, Union, List, Tuple, Any, Dict

from app.core.config import settings
from app.core.context import Context, MediaInfo
from app.log import logger
from app.modules import _ModuleBase, checkMessage
from app.modules.vocechat.vocechat import VoceChat
from app.schemas import MessageChannel, CommingMessage, Notification


class VoceChatModule(_ModuleBase):
    vocechat: VoceChat = None

    def init_module(self) -> None:
        self.vocechat = VoceChat()

    @staticmethod
    def get_name() -> str:
        return "VoceChat"

    def stop(self):
        pass

    def test(self) -> Tuple[bool, str]:
        """
        测试模块连接性
        """
        state = self.vocechat.get_state()
        if state:
            return True, ""
        return False, "获取VoceChat频道失败"

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        return "MESSAGER", "vocechat"

    @staticmethod
    def message_parser(body: Any, form: Any,
                       args: Any) -> Optional[CommingMessage]:
        """
        解析消息内容，返回字典，注意以下约定值：
        userid: 用户ID
        username: 用户名
        text: 内容
        :param body: 请求体
        :param form: 表单
        :param args: 参数
        :return: 渠道、消息体
        """
        try:
            """
            {
              "created_at": 1672048481664, //消息创建的时间戳
              "detail": {
                "content": "hello this is my message to you", //消息内容
                "content_type": "text/plain", //消息类型，text/plain：纯文本消息，text/markdown：markdown消息，vocechat/file：文件类消息
                "expires_in": null, //消息过期时长，如果有大于0数字，说明该消息是个限时消息
                "properties": null, //一些有关消息的元数据，比如at信息，文件消息的具体类型信息，如果是个图片消息，还会有一些宽高，图片名称等元信息
                "type": "normal" //消息类型，normal代表是新消息
              },
              "from_uid": 7910, //来自于谁
              "mid": 2978, //消息ID
              "target": { "gid": 2 } //发送给谁，gid代表是发送给频道，uid代表是发送给个人，此时的数据结构举例：{"uid":1}
            }
            """
            # 报文体
            msg_body = json.loads(body)
            # 类型
            msg_type = msg_body.get("detail", {}).get("type")
            if msg_type != "normal":
                # 非新消息
                return None
            logger.debug(f"收到VoceChat请求：{msg_body}")
            # token校验
            token = args.get("token")
            if not token or token != settings.API_TOKEN:
                logger.warn(f"VoceChat请求token校验失败：{token}")
                return None
            # 文本内容
            content = msg_body.get("detail", {}).get("content")
            # 用户ID
            gid = msg_body.get("target", {}).get("gid")
            if gid and str(gid) == str(settings.VOCECHAT_CHANNEL_ID):
                # 来自监听频道的消息
                userid = f"GID#{gid}"
            else:
                # 来自个人的消息
                userid = f"UID#{msg_body.get('from_uid')}"

            # 处理消息内容
            if content and userid:
                logger.info(f"收到VoceChat消息：userid={userid}, text={content}")
                return CommingMessage(channel=MessageChannel.VoceChat,
                                      userid=userid, username=userid, text=content)
        except Exception as err:
            logger.error(f"VoceChat消息处理发生错误：{str(err)}")
        return None

    @checkMessage(MessageChannel.VoceChat)
    def post_message(self, message: Notification) -> None:
        """
        发送消息
        :param message: 消息内容
        :return: 成功或失败
        """
        self.vocechat.send_msg(title=message.title, text=message.text,
                               userid=message.userid, link=message.link)

    @checkMessage(MessageChannel.VoceChat)
    def post_medias_message(self, message: Notification, medias: List[MediaInfo]) -> Optional[bool]:
        """
        发送媒体信息选择列表
        :param message: 消息内容
        :param medias: 媒体列表
        :return: 成功或失败
        """
        # 先发送标题
        self.vocechat.send_msg(title=message.title, userid=message.userid)
        # 再发送内容
        return self.vocechat.send_medias_msg(title=message.title, medias=medias,
                                             userid=message.userid, link=message.link)

    @checkMessage(MessageChannel.VoceChat)
    def post_torrents_message(self, message: Notification, torrents: List[Context]) -> Optional[bool]:
        """
        发送种子信息选择列表
        :param message: 消息内容
        :param torrents: 种子列表
        :return: 成功或失败
        """
        return self.vocechat.send_torrents_msg(title=message.title, torrents=torrents,
                                               userid=message.userid, link=message.link)

    def register_commands(self, commands: Dict[str, dict]):
        pass
