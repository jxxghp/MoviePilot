import re
from threading import Lock
from typing import List, Optional

import requests
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient

from app.core.config import settings
from app.core.context import MediaInfo, Context
from app.core.metainfo import MetaInfo
from app.log import logger
from app.utils.string import StringUtils


lock = Lock()


class Slack:

    _client: WebClient = None
    _service: SocketModeHandler = None
    _ds_url = f"http://127.0.0.1:{settings.PORT}/api/v1/message?token={settings.API_TOKEN}"
    _channel = ""

    def __init__(self, SLACK_OAUTH_TOKEN: Optional[str] = None, SLACK_APP_TOKEN: Optional[str] = None, 
                 SLACK_CHANNEL: Optional[str] = None, **kwargs):

        if not SLACK_OAUTH_TOKEN or not SLACK_APP_TOKEN:
            logger.error("Slack 配置不完整！")
            return

        try:
            slack_app = App(token=SLACK_OAUTH_TOKEN,
                            ssl_check_enabled=False,
                            url_verification_enabled=False)
        except Exception as err:
            logger.error(f"Slack初始化失败: {str(err)}")
            return

        self._client = slack_app.client
        self._channel = SLACK_CHANNEL

        # 标记消息来源
        if kwargs.get("name"):
            self._ds_url = f"{self._ds_url}&source={kwargs.get('name')}"

        # 注册消息响应
        @slack_app.event("message")
        def slack_message(message):
            with requests.post(self._ds_url, json=message, timeout=10) as local_res:
                logger.debug("message: %s processed, response is: %s" % (message, local_res.text))

        @slack_app.action(re.compile(r"actionId-\d+"))
        def slack_action(ack, body):
            ack()
            with requests.post(self._ds_url, json=body, timeout=60) as local_res:
                logger.debug("message: %s processed, response is: %s" % (body, local_res.text))

        @slack_app.event("app_mention")
        def slack_mention(say, body):
            say(f"收到，请稍等... <@{body.get('event', {}).get('user')}>")
            with requests.post(self._ds_url, json=body, timeout=10) as local_res:
                logger.debug("message: %s processed, response is: %s" % (body, local_res.text))

        @slack_app.shortcut(re.compile(r"/*"))
        def slack_shortcut(ack, body):
            ack()
            with requests.post(self._ds_url, json=body, timeout=10) as local_res:
                logger.debug("message: %s processed, response is: %s" % (body, local_res.text))

        @slack_app.command(re.compile(r"/*"))
        def slack_command(ack, body):
            ack()
            with requests.post(self._ds_url, json=body, timeout=10) as local_res:
                logger.debug("message: %s processed, response is: %s" % (body, local_res.text))

        # 启动服务
        try:
            self._service = SocketModeHandler(
                slack_app,
                SLACK_APP_TOKEN
            )
            self._service.connect()
            logger.info("Slack消息接收服务启动")
        except Exception as err:
            logger.error("Slack消息接收服务启动失败: %s" % str(err))

    def stop(self):
        if self._service:
            try:
                self._service.close()
                logger.info("Slack消息接收服务已停止")
            except Exception as err:
                logger.error("Slack消息接收服务停止失败: %s" % str(err))

    def get_state(self) -> bool:
        """
        获取状态
        """
        return True if self._client else False

    def send_msg(self, title: str, text: Optional[str] = None,
                 image: Optional[str] = None, link: Optional[str] = None,
                 userid: Optional[str] = None, buttons: Optional[List[List[dict]]] = None,
                 original_message_id: Optional[str] = None,
                 original_chat_id: Optional[str] = None):
        """
        发送Slack消息
        :param title: 消息标题
        :param text: 消息内容
        :param image: 消息图片地址
        :param link: 点击消息转转的URL
        :param userid: 用户ID，如有则只发消息给该用户
        :param buttons: 消息按钮列表，格式为 [[{"text": "按钮文本", "callback_data": "回调数据", "url": "链接"}]]
        :param original_message_id: 原消息的时间戳，如果提供则编辑原消息
        :param original_chat_id: 原消息的频道ID，编辑消息时需要
        """
        if not self._client:
            return False, "消息客户端未就绪"
        if not title and not text:
            return False, "标题和内容不能同时为空"
        try:
            if userid:
                channel = userid
            else:
                # 消息广播
                channel = self.__find_public_channel()
            # 消息文本
            message_text = ""
            # 结构体
            blocks = []
            if not image:
                message_text = f"{title}\n{text or ''}"
            else:
                # 消息图片
                if image:
                    # 拼装消息内容
                    blocks.append({"type": "section", "text": {
                        "type": "mrkdwn",
                        "text": f"*{title}*\n{text or ''}"
                    }, 'accessory': {
                        "type": "image",
                        "image_url": f"{image}",
                        "alt_text": f"{title}"
                    }})
                # 自定义按钮
                if buttons:
                    for button_row in buttons:
                        elements = []
                        for button in button_row:
                            if "url" in button:
                                # URL按钮
                                elements.append({
                                    "type": "button",
                                    "text": {
                                        "type": "plain_text",
                                        "text": button["text"],
                                        "emoji": True
                                    },
                                    "url": button["url"],
                                    "action_id": f"actionId-url-{len(elements)}"
                                })
                            else:
                                # 回调按钮
                                elements.append({
                                    "type": "button",
                                    "text": {
                                        "type": "plain_text",
                                        "text": button["text"],
                                        "emoji": True
                                    },
                                    "value": button["callback_data"],
                                    "action_id": f"actionId-{button['callback_data']}"
                                })
                        if elements:
                            blocks.append({
                                "type": "actions",
                                "elements": elements
                            })
                elif link:
                    # 默认链接按钮
                    blocks.append({
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {
                                    "type": "plain_text",
                                    "text": "查看详情",
                                    "emoji": True
                                },
                                "value": "click_me_url",
                                "url": f"{link}",
                                "action_id": "actionId-url"
                            }
                        ]
                    })
            
            # 判断是编辑消息还是发送新消息
            if original_message_id and original_chat_id:
                # 编辑消息
                result = self._client.chat_update(
                    channel=original_chat_id,
                    ts=original_message_id,
                    text=message_text[:1000],
                    blocks=blocks or []
                )
            else:
                # 发送新消息
                result = self._client.chat_postMessage(
                    channel=channel,
                    text=message_text[:1000],
                    blocks=blocks,
                    mrkdwn=True
                )
            return True, result
        except Exception as msg_e:
            logger.error(f"Slack消息发送失败: {msg_e}")
            return False, str(msg_e)

    def send_medias_msg(self, medias: List[MediaInfo], userid: Optional[str] = None, title: Optional[str] = None,
                        buttons: Optional[List[List[dict]]] = None,
                        original_message_id: Optional[str] = None,
                        original_chat_id: Optional[str] = None) -> Optional[bool]:
        """
        发送媒体列表消息
        :param medias: 媒体信息列表
        :param userid: 用户ID，如有则只发消息给该用户
        :param title: 消息标题
        :param buttons: 按钮列表，格式：[[{"text": "按钮文本", "callback_data": "回调数据"}]]
        :param original_message_id: 原消息的时间戳，如果提供则编辑原消息
        :param original_chat_id: 原消息的频道ID，编辑消息时需要
        """
        if not self._client:
            return False
        if not medias:
            return False
        try:
            if userid:
                channel = userid
            else:
                # 消息广播
                channel = self.__find_public_channel()
            # 消息主体
            title_section = {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{title}*"
                }
            }
            blocks = [title_section]
            # 列表
            if medias:
                blocks.append({
                    "type": "divider"
                })
                index = 1
                for media in medias:
                    if media.get_poster_image():
                        if media.vote_star:
                            text = f"{index}. *<{media.detail_link}|{media.title_year}>*" \
                                   f"\n类型：{media.type.value}" \
                                   f"\n{media.vote_star}" \
                                   f"\n{media.get_overview_string(50)}"
                        else:
                            text = f"{index}. *<{media.detail_link}|{media.title_year}>*" \
                                   f"\n类型：{media.type.value}" \
                                   f"\n{media.get_overview_string(50)}"
                        blocks.append(
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": text
                                },
                                "accessory": {
                                    "type": "image",
                                    "image_url": f"{media.get_poster_image()}",
                                    "alt_text": f"{media.title_year}"
                                }
                            }
                        )
                        # 如果有自定义按钮，使用自定义按钮，否则使用默认选择按钮
                        if buttons:
                            # 使用自定义按钮（通常来自MessageChain的智能生成）
                            for button_row in buttons:
                                elements = []
                                for button in button_row:
                                    if "url" in button:
                                        elements.append({
                                            "type": "button",
                                            "text": {
                                                "type": "plain_text",
                                                "text": button["text"],
                                                "emoji": True
                                            },
                                            "url": button["url"],
                                            "action_id": f"actionId-url-{len(elements)}"
                                        })
                                    else:
                                        elements.append({
                                            "type": "button",
                                            "text": {
                                                "type": "plain_text",
                                                "text": button["text"],
                                                "emoji": True
                                            },
                                            "value": button["callback_data"],
                                            "action_id": f"actionId-{button['callback_data']}"
                                        })
                                if elements:
                                    blocks.append({
                                        "type": "actions",
                                        "elements": elements
                                    })
                            # 只为第一个媒体项添加按钮，避免重复
                            buttons = None
                        else:
                            # 使用默认选择按钮
                            blocks.append(
                                {
                                    "type": "actions",
                                    "elements": [
                                        {
                                            "type": "button",
                                            "text": {
                                                "type": "plain_text",
                                                "text": "选择",
                                                "emoji": True
                                            },
                                            "value": f"{index}",
                                            "action_id": f"actionId-{index}"
                                        }
                                    ]
                                }
                            )
                        index += 1
            
            # 判断是编辑消息还是发送新消息
            if original_message_id and original_chat_id:
                # 编辑消息
                result = self._client.chat_update(
                    channel=original_chat_id,
                    ts=original_message_id,
                    text=title,
                    blocks=blocks or []
                )
            else:
                # 发送新消息
                result = self._client.chat_postMessage(
                    channel=channel,
                    text=title,
                    blocks=blocks
                )
            return True if result else False
        except Exception as msg_e:
            logger.error(f"Slack消息发送失败: {msg_e}")
            return False

    def send_torrents_msg(self, torrents: List[Context], userid: Optional[str] = None, title: Optional[str] = None,
                          buttons: Optional[List[List[dict]]] = None,
                          original_message_id: Optional[str] = None,
                          original_chat_id: Optional[str] = None) -> Optional[bool]:
        """
        发送种子列表消息
        :param torrents: 种子信息列表
        :param userid: 用户ID，如有则只发消息给该用户
        :param title: 消息标题
        :param buttons: 按钮列表，格式：[[{"text": "按钮文本", "callback_data": "回调数据"}]]
        :param original_message_id: 原消息的时间戳，如果提供则编辑原消息
        :param original_chat_id: 原消息的频道ID，编辑消息时需要
        """
        if not self._client:
            return None

        try:
            if userid:
                channel = userid
            else:
                # 消息广播
                channel = self.__find_public_channel()
            # 消息主体
            title_section = {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{title}*"
                }
            }
            blocks = [title_section, {
                "type": "divider"
            }]
            # 列表
            index = 1
            
            # 如果有自定义按钮，先添加种子列表，然后添加统一的按钮
            if buttons:
                # 添加种子列表（不带单独的选择按钮）
                for context in torrents:
                    torrent = context.torrent_info
                    site_name = torrent.site_name
                    meta = MetaInfo(torrent.title, torrent.description)
                    link = torrent.page_url
                    title_text = f"{meta.season_episode} " \
                            f"{meta.resource_term} " \
                            f"{meta.video_term} " \
                            f"{meta.release_group}"
                    title_text = re.sub(r"\s+", " ", title_text).strip()
                    free = torrent.volume_factor
                    seeder = f"{torrent.seeders}↑"
                    description = torrent.description
                    text = f"{index}. 【{site_name}】<{link}|{title_text}> " \
                           f"{StringUtils.str_filesize(torrent.size)} {free} {seeder}\n" \
                           f"{description}"
                    blocks.append(
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": text
                            }
                        }
                    )
                    index += 1
                
                # 添加统一的自定义按钮
                for button_row in buttons:
                    elements = []
                    for button in button_row:
                        if "url" in button:
                            elements.append({
                                "type": "button",
                                "text": {
                                    "type": "plain_text",
                                    "text": button["text"],
                                    "emoji": True
                                },
                                "url": button["url"],
                                "action_id": f"actionId-url-{len(elements)}"
                            })
                        else:
                            elements.append({
                                "type": "button",
                                "text": {
                                    "type": "plain_text",
                                    "text": button["text"],
                                    "emoji": True
                                },
                                "value": button["callback_data"],
                                "action_id": f"actionId-{button['callback_data']}"
                            })
                    if elements:
                        blocks.append({
                            "type": "actions",
                            "elements": elements
                        })
            else:
                # 使用默认的每个种子单独按钮
                for context in torrents:
                    torrent = context.torrent_info
                    site_name = torrent.site_name
                    meta = MetaInfo(torrent.title, torrent.description)
                    link = torrent.page_url
                    title_text = f"{meta.season_episode} " \
                            f"{meta.resource_term} " \
                            f"{meta.video_term} " \
                            f"{meta.release_group}"
                    title_text = re.sub(r"\s+", " ", title_text).strip()
                    free = torrent.volume_factor
                    seeder = f"{torrent.seeders}↑"
                    description = torrent.description
                    text = f"{index}. 【{site_name}】<{link}|{title_text}> " \
                           f"{StringUtils.str_filesize(torrent.size)} {free} {seeder}\n" \
                           f"{description}"
                    blocks.append(
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": text
                            }
                        }
                    )
                    blocks.append(
                        {
                            "type": "actions",
                            "elements": [
                                {
                                    "type": "button",
                                    "text": {
                                        "type": "plain_text",
                                        "text": "选择",
                                        "emoji": True
                                    },
                                    "value": f"{index}",
                                    "action_id": f"actionId-{index}"
                                }
                            ]
                        }
                    )
                    index += 1
            
            # 判断是编辑消息还是发送新消息
            if original_message_id and original_chat_id:
                # 编辑消息
                result = self._client.chat_update(
                    channel=original_chat_id,
                    ts=original_message_id,
                    text=title,
                    blocks=blocks or []
                )
            else:
                # 发送新消息
                result = self._client.chat_postMessage(
                    channel=channel,
                    text=title,
                    blocks=blocks
                )
            return True if result else False
        except Exception as msg_e:
            logger.error(f"Slack消息发送失败: {msg_e}")
            return False

    def __find_public_channel(self):
        """
        查找公共频道
        """
        if not self._client:
            return ""
        conversation_id = ""
        try:
            for result in self._client.conversations_list(types="public_channel,private_channel"):
                if conversation_id:
                    break
                for channel in result["channels"]:
                    if channel.get("name") == (self._channel or "全体"):
                        conversation_id = channel.get("id")
                        break
        except Exception as e:
            logger.error(f"查找Slack公共频道失败: {str(e)}")
        return conversation_id
