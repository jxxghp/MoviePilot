import xml.dom.minidom
from typing import Optional, Union, List, Tuple, Any

from app.core.context import MediaInfo, Context
from app.core.config import settings
from app.log import logger
from app.modules import _ModuleBase
from app.modules.wechat.WXBizMsgCrypt3 import WXBizMsgCrypt
from app.modules.wechat.wechat import WeChat
from app.utils.dom import DomUtils


class WechatModule(_ModuleBase):

    wechat: WeChat = None

    def init_module(self) -> None:
        self.wechat = WeChat()

    def stop(self):
        pass

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        return "MESSAGER", "wechat"

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
        try:
            # URL参数
            sVerifyMsgSig = args.get("msg_signature")
            sVerifyTimeStamp = args.get("timestamp")
            sVerifyNonce = args.get("nonce")
            if not sVerifyMsgSig or not sVerifyTimeStamp or not sVerifyNonce:
                logger.error(f"微信请求参数错误：{args}")
                return None
            # 解密模块
            wxcpt = WXBizMsgCrypt(sToken=settings.WECHAT_TOKEN,
                                  sEncodingAESKey=settings.WECHAT_ENCODING_AESKEY,
                                  sReceiveId=settings.WECHAT_CORPID)
            # 报文数据
            if not body:
                logger.error(f"微信请求数据为空")
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
                wechat_admins = settings.WECHAT_ADMINS.split(',')
                if wechat_admins and not any(
                        user_id == admin_user for admin_user in wechat_admins):
                    self.wechat.send_msg(title="用户无权限执行菜单命令", userid=user_id)
                    return {}
            elif msg_type == "text":
                # 文本消息
                content = DomUtils.tag_value(root_node, "Content", default="")
                if content:
                    logger.info(f"收到微信消息：userid={user_id}, text={content}")
                # 处理消息内容
                return {
                    "userid": user_id,
                    "username": user_id,
                    "text": content
                }
        except Exception as err:
            logger.error(f"微信消息处理发生错误：{err}")
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
        return self.wechat.send_msg(title=title, text=text, image=image, userid=userid)

    def post_medias_message(self, title: str, items: List[MediaInfo],
                            userid: Union[str, int] = None) -> Optional[bool]:
        """
        发送媒体信息选择列表
        :param title:  标题
        :param items:  消息列表
        :param userid:  用户ID
        :return: 成功或失败
        """
        # 先发送标题
        self.wechat.send_msg(title=title)
        # 再发送内容
        return self.wechat.send_medias_msg(medias=items, userid=userid)

    def post_torrents_message(self, title: str, items: List[Context],
                              mediainfo: MediaInfo,
                              userid: Union[str, int] = None) -> Optional[bool]:
        """
        发送种子信息选择列表
        :param title: 标题
        :param items:  消息列表
        :param mediainfo:  媒体信息
        :param userid:  用户ID
        :return: 成功或失败
        """
        return self.wechat.send_torrents_msg(title=title, torrents=items, mediainfo=mediainfo, userid=userid)
