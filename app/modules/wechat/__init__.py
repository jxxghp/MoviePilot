import xml.dom.minidom
from typing import Optional, Union, List, Tuple, Any, Dict

from app.core.config import settings
from app.core.context import Context, MediaInfo
from app.log import logger
from app.modules import _ModuleBase, checkMessage
from app.modules.wechat.WXBizMsgCrypt3 import WXBizMsgCrypt
from app.modules.wechat.wechat import WeChat
from app.schemas import MessageChannel, CommingMessage, Notification
from app.utils.dom import DomUtils


class WechatModule(_ModuleBase):
    wechat: WeChat = None

    def init_module(self) -> None:
        self.wechat = WeChat()

    @staticmethod
    def get_name() -> str:
        return "微信"

    def stop(self):
        pass

    def test(self) -> Tuple[bool, str]:
        """
        测试模块连接性
        """
        state = self.wechat.get_state()
        if state:
            return True, ""
        return False, "获取微信token失败"

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        return "MESSAGER", "wechat"

    def message_parser(self, body: Any, form: Any,
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
            # URL参数
            sVerifyMsgSig = args.get("msg_signature")
            sVerifyTimeStamp = args.get("timestamp")
            sVerifyNonce = args.get("nonce")
            if not sVerifyMsgSig or not sVerifyTimeStamp or not sVerifyNonce:
                logger.debug(f"微信请求参数错误：{args}")
                return None
            # 解密模块
            wxcpt = WXBizMsgCrypt(sToken=settings.WECHAT_TOKEN,
                                  sEncodingAESKey=settings.WECHAT_ENCODING_AESKEY,
                                  sReceiveId=settings.WECHAT_CORPID)
            # 报文数据
            if not body:
                logger.debug(f"微信请求数据为空")
                return None
            logger.debug(f"收到微信请求：{body}")
            ret, sMsg = wxcpt.DecryptMsg(sPostData=body,
                                         sMsgSignature=sVerifyMsgSig,
                                         sTimeStamp=sVerifyTimeStamp,
                                         sNonce=sVerifyNonce)
            if ret != 0:
                logger.error(f"解密微信消息失败 DecryptMsg ret = {ret}")
                return None
            # 解析XML报文
            """
            1、消息格式：
            <xml>
               <ToUserName><![CDATA[toUser]]></ToUserName>
               <FromUserName><![CDATA[fromUser]]></FromUserName> 
               <CreateTime>1348831860</CreateTime>
               <MsgType><![CDATA[text]]></MsgType>
               <Content><![CDATA[this is a test]]></Content>
               <MsgId>1234567890123456</MsgId>
               <AgentID>1</AgentID>
            </xml>
            2、事件格式：
            <xml>
                <ToUserName><![CDATA[toUser]]></ToUserName>
                <FromUserName><![CDATA[UserID]]></FromUserName>
                <CreateTime>1348831860</CreateTime>
                <MsgType><![CDATA[event]]></MsgType>
                <Event><![CDATA[subscribe]]></Event>
                <AgentID>1</AgentID>
            </xml>            
            """
            dom_tree = xml.dom.minidom.parseString(sMsg.decode('UTF-8'))
            root_node = dom_tree.documentElement
            # 消息类型
            msg_type = DomUtils.tag_value(root_node, "MsgType")
            # Event event事件只有click才有效,enter_agent无效
            event = DomUtils.tag_value(root_node, "Event")
            # 用户ID
            user_id = DomUtils.tag_value(root_node, "FromUserName")
            # 没的消息类型和用户ID的消息不要
            if not msg_type or not user_id:
                logger.warn(f"解析不到消息类型和用户ID")
                return None
            # 解析消息内容
            if msg_type == "event" and event == "click":
                # 校验用户有权限执行交互命令
                if settings.WECHAT_ADMINS:
                    wechat_admins = settings.WECHAT_ADMINS.split(',')
                    if wechat_admins and not any(
                            user_id == admin_user for admin_user in wechat_admins):
                        self.wechat.send_msg(title="用户无权限执行菜单命令", userid=user_id)
                        return None
                # 根据EventKey执行命令
                content = DomUtils.tag_value(root_node, "EventKey")
                logger.info(f"收到微信事件：userid={user_id}, event={content}")
            elif msg_type == "text":
                # 文本消息
                content = DomUtils.tag_value(root_node, "Content", default="")
                logger.info(f"收到微信消息：userid={user_id}, text={content}")
            else:
                return None

            if content:
                # 处理消息内容
                return CommingMessage(channel=MessageChannel.Wechat,
                                      userid=user_id, username=user_id, text=content)
        except Exception as err:
            logger.error(f"微信消息处理发生错误：{str(err)}")
        return None

    @checkMessage(MessageChannel.Wechat)
    def post_message(self, message: Notification) -> None:
        """
        发送消息
        :param message: 消息内容
        :return: 成功或失败
        """
        self.wechat.send_msg(title=message.title, text=message.text,
                             image=message.image, userid=message.userid, link=message.link)

    @checkMessage(MessageChannel.Wechat)
    def post_medias_message(self, message: Notification, medias: List[MediaInfo]) -> Optional[bool]:
        """
        发送媒体信息选择列表
        :param message: 消息内容
        :param medias: 媒体列表
        :return: 成功或失败
        """
        # 先发送标题
        self.wechat.send_msg(title=message.title, userid=message.userid, link=message.link)
        # 再发送内容
        return self.wechat.send_medias_msg(medias=medias, userid=message.userid)

    @checkMessage(MessageChannel.Wechat)
    def post_torrents_message(self, message: Notification, torrents: List[Context]) -> Optional[bool]:
        """
        发送种子信息选择列表
        :param message: 消息内容
        :param torrents: 种子列表
        :return: 成功或失败
        """
        return self.wechat.send_torrents_msg(title=message.title, torrents=torrents,
                                             userid=message.userid, link=message.link)

    def register_commands(self, commands: Dict[str, dict]):
        """
        注册命令，实现这个函数接收系统可用的命令菜单
        :param commands: 命令字典
        """
        self.wechat.create_menus(commands)
