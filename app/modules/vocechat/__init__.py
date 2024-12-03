import json
from typing import Optional, Union, List, Tuple, Any, Dict

from app.core.context import Context, MediaInfo
from app.log import logger
from app.modules import _ModuleBase, _MessageBase
from app.modules.vocechat.vocechat import VoceChat
from app.schemas import MessageChannel, CommingMessage, Notification
from app.schemas.types import ModuleType


class VoceChatModule(_ModuleBase, _MessageBase[VoceChat]):

    def init_module(self) -> None:
        """
        初始化模块
        """
        super().init_service(service_name=VoceChat.__name__.lower(),
                             service_type=VoceChat)
        self._channel = MessageChannel.VoceChat

    @staticmethod
    def get_name() -> str:
        return "VoceChat"

    @staticmethod
    def get_type() -> ModuleType:
        """
        获取模块类型
        """
        return ModuleType.Notification

    @staticmethod
    def get_subtype() -> MessageChannel:
        """
        获取模块子类型
        """
        return MessageChannel.VoceChat

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效
        """
        return 4

    def stop(self):
        pass

    def test(self) -> Optional[Tuple[bool, str]]:
        """
        测试模块连接性
        """
        if not self.get_instances():
            return None
        for name, client in self.get_instances().items():
            state = client.get_state()
            if not state:
                return False, f"VoceChat {name} 未就续"
        return True, ""

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    def message_parser(self, source: str, body: Any, form: Any,
                       args: Any) -> Optional[CommingMessage]:
        """
        解析消息内容，返回字典，注意以下约定值：
        userid: 用户ID
        username: 用户名
        text: 内容
        :param source: 消息来源
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
            # 获取服务配置
            client_config = self.get_config(source)
            if not client_config:
                return None
            # 报文体
            msg_body = json.loads(body)
            # 类型
            msg_type = msg_body.get("detail", {}).get("type")
            if msg_type != "normal":
                # 非新消息
                return None
            logger.debug(f"收到VoceChat请求：{msg_body}")
            # 文本内容
            content = msg_body.get("detail", {}).get("content")
            # 用户ID
            gid = msg_body.get("target", {}).get("gid")
            channel_id = client_config.config.get("channel_id")
            if gid and str(gid) == str(channel_id):
                # 来自监听频道的消息
                userid = f"GID#{gid}"
            else:
                # 来自个人的消息
                userid = f"UID#{msg_body.get('from_uid')}"

            # 处理消息内容
            if content and userid:
                logger.info(f"收到来自 {client_config.name} 的VoceChat消息：userid={userid}, text={content}")
                return CommingMessage(channel=MessageChannel.VoceChat, source=client_config.name,
                                      userid=userid, username=userid, text=content)
        except Exception as err:
            logger.error(f"VoceChat消息处理发生错误：{str(err)}")
        return None

    def post_message(self, message: Notification) -> None:
        """
        发送消息
        :param message: 消息内容
        :return: 成功或失败
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            targets = message.targets
            userid = message.userid
            if not message.userid and targets:
                userid = targets.get('telegram_userid')
            client: VoceChat = self.get_instance(conf.name)
            if client:
                client.send_msg(title=message.title, text=message.text,
                                userid=userid, link=message.link)

    def post_medias_message(self, message: Notification, medias: List[MediaInfo]) -> None:
        """
        发送媒体信息选择列表
        :param message: 消息内容
        :param medias: 媒体列表
        :return: 成功或失败
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            client: VoceChat = self.get_instance(conf.name)
            if client:
                client.send_msg(title=message.title, userid=message.userid)
                client.send_medias_msg(title=message.title, medias=medias,
                                       userid=message.userid, link=message.link)

    def post_torrents_message(self, message: Notification, torrents: List[Context]) -> None:
        """
        发送种子信息选择列表
        :param message: 消息内容
        :param torrents: 种子列表
        :return: 成功或失败
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            targets = message.targets
            userid = message.userid
            if not userid and targets is not None:
                userid = targets.get('vocechat_userid')
                if not userid:
                    logger.warn(f"用户没有指定 VoceChat用户ID，消息无法发送")
                    return
            client: VoceChat = self.get_instance(conf.name)
            if client:
                client.send_torrents_msg(title=message.title, torrents=torrents,
                                         userid=userid, link=message.link)

    def register_commands(self, commands: Dict[str, dict]):
        pass
