import json
import re
from threading import Lock
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient

from app.runtime.config import settings
from app.domain.context import MediaInfo, Context
from app.domain.metainfo import MetaInfo
from app.runtime.log import logger
from app.adapters.network.http import RequestUtils
from app.foundation import size as size_tools

lock = Lock()


class Slack:
    """Slack 通知与交互客户端。"""

    _client: WebClient = None
    _service: SocketModeHandler = None
    _ds_url = f"http://127.0.0.1:{settings.PORT}/api/v1/message?token={settings.API_TOKEN}"
    _channel = ""
    _oauth_token = ""
    _MAX_SLASH_COMMANDS = 50
    _SLASH_COMMAND_USAGE_HINT = "MoviePilot 可选参数"

    def __init__(self, SLACK_OAUTH_TOKEN: Optional[str] = None, SLACK_APP_TOKEN: Optional[str] = None,
                 SLACK_CHANNEL: Optional[str] = None,
                 SLACK_APP_ID: Optional[str] = None,
                 SLACK_APP_CONFIG_TOKEN: Optional[str] = None,
                 SLACK_COMMAND_REQUEST_URL: Optional[str] = None,
                 **kwargs):
        """
        初始化 Slack 客户端。

        :param SLACK_OAUTH_TOKEN: Slack Bot User OAuth Token
        :param SLACK_APP_TOKEN: Slack Socket Mode App Token
        :param SLACK_CHANNEL: 默认发送频道
        :param SLACK_APP_ID: Slack App ID，用于可选的 Manifest 命令自动注册
        :param SLACK_APP_CONFIG_TOKEN: Slack App Configuration Token，用于可选的 Manifest 命令自动注册
        :param SLACK_COMMAND_REQUEST_URL: Slash Command 请求 URL，Socket Mode 下可为空
        """

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
        self._oauth_token = SLACK_OAUTH_TOKEN
        self._app_id = (SLACK_APP_ID or "").strip()
        self._command_request_url = (SLACK_COMMAND_REQUEST_URL or "").strip()
        self._manifest_client = (
            WebClient(token=SLACK_APP_CONFIG_TOKEN)
            if SLACK_APP_CONFIG_TOKEN and self._app_id
            else None
        )
        self._registered_command_names: set[str] = set()

        # 标记消息来源
        if kwargs.get("name"):
            # URL encode the source name to handle special characters
            encoded_name = quote(kwargs.get('name'), safe='')
            self._ds_url = f"{self._ds_url}&source={encoded_name}"

        # 注册消息响应
        @slack_app.event("message")
        def slack_message(message):
            with requests.post(self._ds_url, json=message, timeout=10) as local_res:
                logger.debug("message: %s processed, response is: %s" % (message, local_res.text))

        @slack_app.action(re.compile(r"actionId-.*"))
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

    def register_commands(self, commands: Dict[str, dict]) -> bool:
        """
        通过 Slack App Manifest 注册 Slash Commands。

        :param commands: 命令字典，键为斜杠命令，值包含描述和分类等元数据
        :return: 注册是否成功
        """
        if not self._manifest_client or not self._app_id:
            logger.debug("Slack 未配置 SLACK_APP_ID/SLACK_APP_CONFIG_TOKEN，跳过命令自动注册")
            return False
        return self._update_manifest_commands(commands or {})

    def delete_commands(self) -> bool:
        """
        清理本实例自动注册过的 Slack Slash Commands。

        :return: 清理是否成功
        """
        if not self._manifest_client or not self._app_id:
            logger.debug("Slack 未配置 SLACK_APP_ID/SLACK_APP_CONFIG_TOKEN，跳过命令清理")
            return False
        return self._update_manifest_commands({})

    def _update_manifest_commands(self, commands: Dict[str, dict]) -> bool:
        """更新 Slack Manifest 中的 Slash Commands，保留非本实例管理的命令。"""
        try:
            manifest = self._export_manifest()
            if not manifest:
                return False
            features = manifest.setdefault("features", {})
            existing_commands = features.get("slash_commands") or []
            generated_commands = self._build_slash_commands(commands)
            managed_names = self._registered_command_names | {
                item["command"] for item in generated_commands
            }
            preserved_commands = [
                item
                for item in existing_commands
                if (
                    isinstance(item, dict)
                    and item.get("command") not in managed_names
                    and item.get("usage_hint") != self._SLASH_COMMAND_USAGE_HINT
                )
            ]
            available = max(self._MAX_SLASH_COMMANDS - len(preserved_commands), 0)
            if len(generated_commands) > available:
                logger.warning(
                    f"Slack Slash Commands 超过平台上限，仅注册前 {available} 个"
                )
                generated_commands = generated_commands[:available]
            features["slash_commands"] = preserved_commands + generated_commands

            result = self._manifest_client.apps_manifest_update(
                app_id=self._app_id,
                manifest=manifest,
            )
            if result and result.get("ok") is False:
                logger.error(f"Slack Manifest 更新失败：{result.get('error')}")
                return False
            self._registered_command_names = {
                item["command"] for item in generated_commands
            }
            logger.info(f"Slack Slash Commands 已同步：{len(generated_commands)} 个")
            return True
        except Exception as err:
            logger.error(f"Slack Slash Commands 自动注册失败：{err}")
            return False

    def _export_manifest(self) -> Optional[Dict[str, Any]]:
        """导出 Slack App Manifest。"""
        result = self._manifest_client.apps_manifest_export(app_id=self._app_id)
        if result and result.get("ok") is False:
            logger.error(f"Slack Manifest 导出失败：{result.get('error')}")
            return None
        manifest = result.get("manifest") if result else None
        if isinstance(manifest, str):
            manifest = json.loads(manifest)
        return manifest if isinstance(manifest, dict) else None

    def _build_slash_commands(self, commands: Dict[str, dict]) -> List[Dict[str, Any]]:
        """构建 Slack Manifest Slash Commands 配置。"""
        slash_commands = []
        seen_commands = set()
        for command_text, command_data in commands.items():
            command = self._normalize_slack_command(command_text)
            if not command or command in seen_commands:
                logger.warning(f"跳过无效或重复的 Slack Slash Command：{command_text}")
                continue
            seen_commands.add(command)
            description = self._normalize_slack_description(
                command_data.get("description") if isinstance(command_data, dict) else None,
                command,
            )
            item = {
                "command": command,
                "description": description,
                "should_escape": False,
                "usage_hint": self._SLASH_COMMAND_USAGE_HINT,
            }
            if self._command_request_url:
                item["url"] = self._command_request_url
            slash_commands.append(item)
        return slash_commands

    @staticmethod
    def _normalize_slack_command(command_text: str) -> str:
        """转换为 Slack Slash Command 名称。"""
        command = f"/{str(command_text or '').strip().lstrip('/').lower()}"
        if not re.fullmatch(r"/[a-z0-9_-]{1,31}", command):
            return ""
        return command

    @staticmethod
    def _normalize_slack_description(
        description: Optional[str],
        fallback: str,
    ) -> str:
        """整理 Slack Slash Command 描述。"""
        normalized = str(description or fallback or "MoviePilot").strip()
        return normalized[:2000] or "MoviePilot"

    def download_file(self, file_url: str) -> Optional[Tuple[bytes, str]]:
        """
        下载Slack私有文件
        :param file_url: Slack文件URL
        :return: (文件内容, MIME类型)
        """
        if not self._client or not self._oauth_token or not file_url:
            return None
        try:
            headers = {
                "Authorization": f"Bearer {self._oauth_token}",
                "User-Agent": settings.USER_AGENT,
                "Accept": "*/*",
            }
            resp = RequestUtils(headers=headers, timeout=30).get_res(file_url)
            if resp and resp.content:
                mime_type = resp.headers.get("Content-Type", "image/jpeg")
                return resp.content, mime_type.split(";")[0]
        except Exception as e:
            logger.error(f"下载Slack文件失败: {e}")
        return None

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
                                    "action_id": f"actionId-url-{button.get('text', 'url')}-{len(elements)}"
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

    def send_file(
        self,
        file_path: str,
        title: Optional[str] = None,
        text: Optional[str] = None,
        userid: Optional[str] = None,
        file_name: Optional[str] = None,
    ):
        """
        发送本地文件到 Slack。
        """
        if not self._client:
            return False, "消息客户端未就绪"
        if not file_path:
            return False, "文件路径不能为空"

        local_file = Path(file_path)
        if not local_file.exists() or not local_file.is_file():
            return False, f"文件不存在: {local_file}"

        try:
            if userid:
                channel = userid
            else:
                channel = self.__find_public_channel()

            comment_parts = [part for part in [title, text] if part]
            initial_comment = "\n".join(comment_parts) if comment_parts else None

            with local_file.open("rb") as fp:
                result = self._client.files_upload_v2(
                    channel=channel,
                    file=fp,
                    filename=file_name or local_file.name,
                    title=title or (file_name or local_file.name),
                    initial_comment=initial_comment,
                )
            return True, result
        except Exception as err:
            logger.error(f"Slack文件发送失败: {err}")
            return False, str(err)

    def add_reaction(self, channel: str, timestamp: str, emoji: str) -> bool:
        """
        为 Slack 消息添加 reaction，用作正在处理状态。
        """
        if not self._client or not channel or not timestamp or not emoji:
            return False
        try:
            result = self._client.reactions_add(
                channel=channel,
                timestamp=timestamp,
                name=emoji,
            )
            return bool(result and result.get("ok", True))
        except Exception as err:
            logger.error(f"Slack添加reaction失败: {err}")
            return False

    def remove_reaction(self, channel: str, timestamp: str, emoji: str) -> bool:
        """
        移除 Slack 消息 reaction。
        """
        if not self._client or not channel or not timestamp or not emoji:
            return False
        try:
            result = self._client.reactions_remove(
                channel=channel,
                timestamp=timestamp,
                name=emoji,
            )
            return bool(result and result.get("ok", True))
        except Exception as err:
            logger.error(f"Slack移除reaction失败: {err}")
            return False

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

                # 如果有自定义按钮，先添加所有媒体项，然后添加统一的按钮
                if buttons:
                    # 添加媒体列表（不带单独的选择按钮）
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
                            index += 1

                    # 添加统一的自定义按钮（在所有媒体项之后）
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
                                    "action_id": f"actionId-url-{button.get('text', 'url')}-{len(elements)}"
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
                    # 使用默认的每个媒体项单独按钮
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
                           f"{size_tools.format_compact_size(torrent.size)} {free} {seeder}\n" \
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
                                "action_id": f"actionId-url-{button.get('text', 'url')}-{len(elements)}"
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
                           f"{size_tools.format_compact_size(torrent.size)} {free} {seeder}\n" \
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

    def delete_msg(self, message_id: str, chat_id: Optional[str] = None) -> Optional[bool]:
        """
        删除Slack消息
        :param message_id: 消息时间戳（Slack消息ID）
        :param chat_id: 频道ID
        :return: 删除是否成功
        """
        if not self._client:
            return None

        try:
            # 确定要删除消息的频道ID
            if chat_id:
                target_channel = chat_id
            else:
                target_channel = self.__find_public_channel()

            if not target_channel:
                logger.error("无法确定要删除消息的Slack频道")
                return False

            # 删除消息
            result = self._client.chat_delete(
                channel=target_channel,
                ts=message_id
            )

            if result.get("ok"):
                logger.info(f"成功删除Slack消息: channel={target_channel}, ts={message_id}")
                return True
            else:
                logger.error(f"删除Slack消息失败: {result.get('error', 'unknown error')}")
                return False
        except Exception as e:
            logger.error(f"删除Slack消息异常: {str(e)}")
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
