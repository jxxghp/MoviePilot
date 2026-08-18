import json
import re
import xml.dom.minidom
from typing import Optional, Union, List, Tuple, Any, Dict
from urllib.parse import quote

from app.domain.context import Context, MediaInfo
from app.runtime.channels import (
    matches_channel_admin,
    register_channel_admin_resolver,
    resolve_config_principal_ids,
)
from app.runtime.log import logger
from app.modules._base import _MessageChannelModuleBase
from app.adapters.external.wechat_crypt import WXBizMsgCrypt
from app.modules.wechat.wechat import WeChat
from app.modules.wechat.wechatbot import WeChatBot
from app.schemas.notification import NotificationChannel
from app.schemas.message import IncomingMessage
from app.schemas.message import Message
from app.foundation.dom import DomUtils


def _resolve_wechat_admin_ids(config: Optional[dict]) -> set[str]:
    """解析企业微信管理员及机器人模式下的主用户 ID。"""
    config_keys = ["WECHAT_ADMINS"]
    if (config or {}).get("WECHAT_MODE", "app") == "bot":
        config_keys.append("WECHAT_BOT_CHAT_ID")
    return resolve_config_principal_ids(config, *config_keys)


register_channel_admin_resolver(NotificationChannel.Wechat, _resolve_wechat_admin_ids)


class WechatModule(_MessageChannelModuleBase[WeChat]):

    # 管理员配置键，与渠道 resolver 保持一致
    _admin_config_key = "WECHAT_ADMINS"
    # 命令注册事件源标识固定为 WeChat（get_name 为“企业微信”）
    _command_origin = "WeChat"

    def init_module(self) -> None:
        """
        初始化模块
        """
        super().init_service(service_name=WeChat.__name__.lower(),
                             service_type=self._create_client)
        self._channel = NotificationChannel.Wechat

    @staticmethod
    def get_name() -> str:
        return "企业微信"

    @staticmethod
    def get_subtype() -> NotificationChannel:
        """
        获取模块的子类型
        """
        return NotificationChannel.Wechat

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效
        """
        return 1

    def stop(self) -> None:
        """停止模块"""
        for client in self.get_instances().values():
            try:
                if hasattr(client, "stop"):
                    client.stop()
            except Exception as err:
                logger.error(f"停止微信模块实例失败：{err}")

    @staticmethod
    def _is_bot_mode(config: dict) -> bool:
        return (config or {}).get("WECHAT_MODE", "app") == "bot"

    @classmethod
    def _create_client(cls, conf):
        if cls._is_bot_mode(conf.config):
            return WeChatBot(name=conf.name, **conf.config)
        return WeChat(name=conf.name, **conf.config)

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    def message_parser(self, source: str, body: Any, form: Any,
                       args: Any) -> Optional[IncomingMessage]:
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
            # 获取服务配置
            client_config = self.get_config(source)
            if not client_config:
                return None
            if self._is_bot_mode(client_config.config):
                return self._parse_bot_message(source=source, body=body, client_config=client_config)
            client: WeChat = self.get_instance(client_config.name)
            # URL参数
            sVerifyMsgSig = args.get("msg_signature")
            sVerifyTimeStamp = args.get("timestamp")
            sVerifyNonce = args.get("nonce")
            if not sVerifyMsgSig or not sVerifyTimeStamp or not sVerifyNonce:
                logger.debug(f"微信请求参数错误：{args}")
                return None
            # 解密模块
            wxcpt = WXBizMsgCrypt(sToken=client_config.config.get('WECHAT_TOKEN'),
                                  sEncodingAESKey=client_config.config.get('WECHAT_ENCODING_AESKEY'),
                                  sReceiveId=client_config.config.get('WECHAT_CORPID'))
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
            content = None
            images = None
            audio_refs = None
            files = None
            if msg_type == "event" and event == "click":
                # 企业微信菜单最终会转成命令文本，需与斜杠命令使用一致的管理员校验。
                if self._should_reject_admin_command(client_config.config, user_id):
                    client.send_msg(title="只有管理员才有权限执行此命令", userid=user_id)
                    return None
                # 根据EventKey执行命令
                content = DomUtils.tag_value(root_node, "EventKey")
                logger.info(f"收到来自 {client_config.name} 的微信事件：userid={user_id}, event={content}")
            elif msg_type == "text":
                # 文本消息
                content = DomUtils.tag_value(root_node, "Content", default="")
                logger.info(f"收到来自 {client_config.name} 的微信消息：userid={user_id}, text={content}")
            elif msg_type == "image":
                media_id = DomUtils.tag_value(root_node, "MediaId")
                pic_url = DomUtils.tag_value(root_node, "PicUrl")
                if media_id:
                    images = [IncomingMessage.MessageImage(ref=f"wxwork://media_id/{media_id}")]
                elif pic_url:
                    images = [IncomingMessage.MessageImage(ref=pic_url)]
                logger.info(
                    f"收到来自 {client_config.name} 的微信图片消息：userid={user_id}, images={len(images) if images else 0}"
                )
            elif msg_type == "voice":
                media_id = DomUtils.tag_value(root_node, "MediaId")
                recognition = DomUtils.tag_value(root_node, "Recognition", default="")
                content = (recognition or "").strip()
                if media_id:
                    audio_refs = [f"wxwork://voice_media_id/{media_id}"]
                logger.info(
                    f"收到来自 {client_config.name} 的微信语音消息：userid={user_id}, "
                    f"text={content}, audios={len(audio_refs) if audio_refs else 0}"
                )
            elif msg_type == "file":
                media_id = DomUtils.tag_value(root_node, "MediaId")
                file_name = DomUtils.tag_value(root_node, "FileName")
                if media_id:
                    files = [
                        IncomingMessage.MessageAttachment(
                            ref=f"wxwork://file_media_id/{media_id}",
                            name=file_name,
                        )
                    ]
                logger.info(
                    f"收到来自 {client_config.name} 的微信文件消息：userid={user_id}, files={len(files) if files else 0}"
                )
            else:
                return None

            if content and content.startswith("/") and self._should_reject_admin_command(
                    client_config.config, user_id
            ):
                client.send_msg(title="只有管理员才有权限执行此命令", userid=user_id)
                return None

            if content or images or audio_refs or files:
                # 处理消息内容
                return IncomingMessage(channel=NotificationChannel.Wechat, source=client_config.name,
                                      userid=user_id, username=user_id,
                                      is_channel_admin=matches_channel_admin(
                                          NotificationChannel.Wechat,
                                          client_config.config,
                                          user_id,
                                      ), text=content or "",
                                      images=images, audio_refs=audio_refs, files=files)
        except Exception as err:
            logger.error(f"微信消息处理发生错误：{str(err)}")
        return None

    def _parse_bot_message(self, source: str, body: Any, client_config) -> Optional[IncomingMessage]:
        try:
            if isinstance(body, bytes):
                msg_json = json.loads(body)
            elif isinstance(body, dict):
                msg_json = body
            else:
                msg_json = json.loads(body)
            while isinstance(msg_json, str):
                msg_json = json.loads(msg_json)
        except Exception as err:
            logger.debug(f"解析企业微信智能机器人消息失败：{err}")
            return None

        if not isinstance(msg_json, dict):
            return None

        payload_body = msg_json.get("body") or {}
        sender = ((payload_body.get("from") or {}).get("userid") or "").strip()
        if not sender:
            return None
        if payload_body.get("chattype") == "group":
            return None

        text = WeChatBot._extract_text_from_body(payload_body)
        images = WeChatBot._extract_images_from_body(payload_body)
        audio_refs = ["wxbot://voice"] if payload_body.get("msgtype") == "voice" else None
        files = None
        if payload_body.get("msgtype") == "file":
            file_payload = payload_body.get("file") or {}
            download_url = file_payload.get("download_url")
            if download_url:
                files = [
                    IncomingMessage.MessageAttachment(
                        ref=f"wxbot://file/{quote(download_url, safe='')}",
                        name=file_payload.get("name") or file_payload.get("filename"),
                        mime_type=file_payload.get("content_type")
                        or file_payload.get("mime_type"),
                        size=file_payload.get("size"),
                    )
                ]
        if text:
            text = re.sub(r"@\S+", "", text).strip()

        if text and text.startswith("/") and self._should_reject_admin_command(
                client_config.config, sender
        ):
            client: WeChatBot = self.get_instance(client_config.name)
            if client:
                client.send_msg(title="只有管理员才有权限执行此命令", userid=sender)
            return None

        if not text and not images and not audio_refs and not files:
            return None

        logger.info(
            f"收到来自 {client_config.name} 的企业微信智能机器人消息："
            f"userid={sender}, text={text}, images={len(images) if images else 0}"
        )
        return IncomingMessage(
            channel=NotificationChannel.Wechat,
            source=client_config.name,
            userid=sender,
            username=sender,
            is_channel_admin=matches_channel_admin(
                NotificationChannel.Wechat,
                client_config.config,
                sender,
            ),
            text=text or "",
            images=images,
            audio_refs=audio_refs,
            files=files,
        )

    def post_message(self, message: Message, **kwargs) -> None:
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
            if not userid and targets is not None:
                userid = targets.get('wechat_userid')
                if not userid:
                    logger.warn(f"用户没有指定 微信用户ID，消息无法发送")
                    return
            client: WeChat = self.get_instance(conf.name)
            if client:
                if message.voice_path and hasattr(client, "send_voice"):
                    sent = client.send_voice(
                        voice_path=message.voice_path,
                        userid=userid,
                    )
                    if not sent:
                        client.send_msg(title=message.title, text=message.text,
                                        image=message.image, userid=userid, link=message.link)
                else:
                    client.send_msg(title=message.title, text=message.text,
                                    image=message.image, userid=userid, link=message.link)

    def download_wechat_image_to_data_url(self, image_ref: str, source: str) -> Optional[str]:
        """
        下载企业微信渠道图片并转换为 data URL
        """
        if not image_ref:
            return None
        client_config = self.get_config(source)
        if not client_config:
            return None
        client = self.get_instance(client_config.name)
        if not client:
            return None
        if image_ref.startswith("wxwork://media_id/") and hasattr(client, "download_media_to_data_url"):
            media_id = image_ref.replace("wxwork://media_id/", "", 1)
            return client.download_media_to_data_url(media_id)
        if image_ref.startswith("wxbot://image/") and hasattr(client, "download_image_to_data_url"):
            return client.download_image_to_data_url(image_ref)
        return None

    def download_wechat_media_bytes(self, media_ref: str, source: str) -> Optional[bytes]:
        """
        下载企业微信语音媒体并返回原始字节。
        """
        if not media_ref:
            return None
        client_config = self.get_config(source)
        if not client_config:
            return None
        client = self.get_instance(client_config.name)
        if not client or not hasattr(client, "download_media_bytes"):
            return None
        if media_ref.startswith("wxwork://voice_media_id/"):
            media_id = media_ref.replace("wxwork://voice_media_id/", "", 1)
            return client.download_media_bytes(media_id)
        if media_ref.startswith("wxwork://file_media_id/"):
            media_id = media_ref.replace("wxwork://file_media_id/", "", 1)
            return client.download_media_bytes(media_id)
        return None

    def post_medias_message(self, message: Message, medias: List[MediaInfo]) -> None:
        """
        发送媒体信息选择列表
        :param message: 消息内容
        :param medias: 媒体列表
        :return: 成功或失败
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            client: WeChat = self.get_instance(conf.name)
            if client:
                # 先发送标题
                client.send_msg(title=message.title, userid=message.userid, link=message.link)
                # 再发送内容
                client.send_medias_msg(medias=medias, userid=message.userid)

    def post_torrents_message(self, message: Message, torrents: List[Context]) -> None:
        """
        发送种子信息选择列表
        :param message: 消息内容
        :param torrents: 种子列表
        :return: 成功或失败
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            client: WeChat = self.get_instance(conf.name)
            if client:
                client.send_torrents_msg(title=message.title, torrents=torrents,
                                         userid=message.userid, link=message.link)

    def _commands_enabled(self, config: Optional[dict]) -> bool:
        """
        菜单注册前置条件：智能机器人模式无传统菜单，缺少解密参数时无法调用菜单 API。
        """
        if self._is_bot_mode(config):
            logger.debug("智能机器人模式，跳过传统菜单初始化")
            return False
        if not config.get("WECHAT_ENCODING_AESKEY") or not config.get("WECHAT_TOKEN"):
            logger.debug("缺少消息解密参数，跳过菜单初始化")
            return False
        return True

    def _delete_commands(self, client) -> None:
        """企业微信使用自定义菜单 API 清理命令。"""
        client.delete_menus()

    def _apply_commands(self, client, commands: Dict[str, dict]) -> None:
        """企业微信使用自定义菜单 API 注册命令。"""
        client.create_menus(commands)
