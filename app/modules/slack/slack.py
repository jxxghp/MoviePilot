"""
Slack Socket Mode 连接、命令清单管理与消息及文件发送客户端。
"""

import json
import re
from functools import partial
from hashlib import file_digest, sha256
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, List, Optional, Tuple, TypedDict, cast

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError, SlackRequestError
from slack_sdk.web.slack_response import SlackResponse

from app.adapters.network.http import RequestUtils
from app.adapters.system.attachment import image_upload_file, load_image
from app.application.messaging.ingress import submit_bound_payload as _submit_bound_payload
from app.domain.context import Context, MediaInfo
from app.domain.metainfo import MetaInfo
from app.foundation import size as size_tools
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.runtime.thread import ThreadHelper
from app.schemas.delivery import DeliveryPartOutcome, DeliveryPartPlanner, DeliveryPartSender, DeliveryReceipt
from app.schemas.message import Message
from app.schemas.types import NotificationChannel

lock = Lock()


class _SlackThread(TypedDict, total=False):
    """
    Slack SDK 发送时按需携带的线程参数。

    :ivar thread_ts: 明确回复线程时使用的父消息时间戳
    """

    thread_ts: str


class Slack:
    """
    Slack 通知与交互客户端。
    """

    _client: Optional[WebClient] = None
    _notification_client: Optional[WebClient] = None
    _service: Optional[SocketModeHandler] = None
    _channel = ""
    _oauth_token = ""
    # Slack 应用命令清单的最大命令数。
    _MAX_SLASH_COMMANDS = 50
    _SLASH_COMMAND_USAGE_HINT = "MoviePilot 可选参数"
    # Slack 单个正文块的字符上限，超过时回退为普通通知。
    _SECTION_TEXT_LIMIT = 3000
    # 从单消息 50 块上限中预留标题等结构所需空间。
    _FORMATTED_BLOCK_LIMIT = 48

    @staticmethod
    def probe_workspace_identity(token: str) -> str | None:
        """
        从 Slack 官方接口确认令牌所属的工作区及机器人。

        :param token: 待核验的机器人 OAuth 访问令牌
        :return: 官方明确返回的工作区与机器人标识
        """
        if not re.fullmatch(r"[A-Za-z0-9._-]+", token):
            return None
        response = RequestUtils(verify=True, timeout=8, headers={
            "Authorization": f"Bearer {token}",
        }).post_res("https://slack.com/api/auth.test", allow_redirects=False)
        if response is None:
            return None
        try:
            payload = response.json() if response.status_code == 200 else None
        except ValueError:
            return None
        finally:
            response.close()
        if not isinstance(payload, dict) or payload.get("ok") is not True or payload.get("is_enterprise_install") is True:
            return None
        workspace = payload.get("team_id")
        bot = payload.get("bot_id")
        if isinstance(workspace, str) and re.fullmatch(r"T[A-Z0-9]+", workspace) and (
            isinstance(bot, str) and re.fullmatch(r"B[A-Z0-9]+", bot)
        ):
            return f"{workspace}:{bot}"
        return None

    def __init__(self, SLACK_OAUTH_TOKEN: Optional[str] = None, SLACK_APP_TOKEN: Optional[str] = None,
                 SLACK_CHANNEL: Optional[str] = None,
                 SLACK_APP_ID: Optional[str] = None,
                 SLACK_APP_CONFIG_TOKEN: Optional[str] = None,
                 SLACK_COMMAND_REQUEST_URL: Optional[str] = None,
                 *,
                 start_socket: bool = True,
                 **kwargs: Any) -> None:
        """
        创建 Slack SDK 客户端，注册入站回调并连接 Socket Mode。

        缺少连接令牌时保留未就绪状态；SDK 初始化或连接异常记录日志，不继续抛出。

        :param SLACK_OAUTH_TOKEN: 机器人 OAuth 访问令牌
        :param SLACK_APP_TOKEN: Socket Mode 应用连接令牌
        :param SLACK_CHANNEL: 默认频道名称；未指定时查找名为“全体”的频道
        :param SLACK_APP_ID: 可选应用标识，与配置令牌一起启用命令清单管理
        :param SLACK_APP_CONFIG_TOKEN: 可选应用配置令牌，用于修改应用命令清单
        :param SLACK_COMMAND_REQUEST_URL: 斜杠命令请求地址；Socket Mode 下可为空
        :param start_socket: 是否创建入站 Socket Mode 服务
        :param kwargs: name 指定通知配置名称，其他配置字段当前忽略
        """

        if not SLACK_OAUTH_TOKEN or (start_socket and not SLACK_APP_TOKEN):
            logger.error("Slack 配置不完整！")
            return

        self._notification_client = WebClient(token=SLACK_OAUTH_TOKEN, retry_handlers=[])
        self._client = self._notification_client
        self._channel = SLACK_CHANNEL or ""
        self._oauth_token = SLACK_OAUTH_TOKEN
        self._config_name = kwargs.get("name")
        self._message_ingress = kwargs.get("message_ingress")
        self._app_id = (SLACK_APP_ID or "").strip()
        self._command_request_url = (SLACK_COMMAND_REQUEST_URL or "").strip()
        self._manifest_client = (
            WebClient(token=SLACK_APP_CONFIG_TOKEN)
            if SLACK_APP_CONFIG_TOKEN and self._app_id
            else None
        )
        self._registered_command_names: set[str] = set()
        if not start_socket:
            return
        try:
            slack_app = App(token=SLACK_OAUTH_TOKEN,
                            ssl_check_enabled=False,
                            url_verification_enabled=False)
            self._client = slack_app.client
        except Exception as err:
            logger.error(f"Slack初始化失败: {str(err)}")
            return

        @slack_app.event("message")
        def slack_message(message: dict[str, Any]) -> None:
            """
            将普通 Slack 消息转交消息链。
            """
            self._forward_to_message_chain(message, timeout=10)

        @slack_app.action(re.compile(r"actionId-.*"))
        def slack_action(ack: Callable[[], Any], body: dict[str, Any]) -> None:
            """
            确认交互按钮事件，并转交消息链处理。
            """
            ack()
            self._forward_to_message_chain(body, timeout=60)

        @slack_app.event("app_mention")
        def slack_mention(say: Callable[[str], Any], body: dict[str, Any]) -> None:
            """
            回应应用提及事件，并转交消息链处理。
            """
            say(f"收到，请稍等... <@{body.get('event', {}).get('user')}>")
            self._forward_to_message_chain(body, timeout=10)

        @slack_app.shortcut(re.compile(r"/*"))
        def slack_shortcut(ack: Callable[[], Any], body: dict[str, Any]) -> None:
            """
            确认快捷操作事件，并转交消息链处理。
            """
            ack()
            self._forward_to_message_chain(body, timeout=10)

        @slack_app.command(re.compile(r"/*"))
        def slack_command(ack: Callable[[], Any], body: dict[str, Any]) -> None:
            """
            确认斜杠命令事件，并转交消息链处理。
            """
            ack()
            self._forward_to_message_chain(body, timeout=10)

        try:
            self._service = SocketModeHandler(
                slack_app,
                SLACK_APP_TOKEN
            )
            cast(Callable[[], None], self._service.connect)()
            logger.info("Slack消息接收服务启动")
        except Exception as err:
            logger.error("Slack消息接收服务启动失败: %s" % str(err))

    def _forward_to_message_chain(self, payload: dict[str, Any], *, timeout: float) -> bool:
        """
        将已认证事件交给当前连接的绑定入口；提交失败在当前路径结束。
        """
        return _submit_bound_payload(
            payload, getattr(self, "_message_ingress", None), self._config_name, submit=ThreadHelper().submit,
        )

    def stop(self) -> bool:
        """
        关闭 Socket Mode 服务，并返回资源是否成功释放。
        """
        self._message_ingress = None
        if self._service:
            try:
                cast(Callable[[], None], self._service.close)()
                logger.info("Slack消息接收服务已停止")
            except Exception as err:
                logger.error("Slack消息接收服务停止失败: %s" % str(err))
                return False
        return True

    def get_state(self) -> bool:
        """
        检查 SDK 消息客户端是否已创建。

        :return: 客户端已创建时返回 True；不保证 Socket Mode 连接在线
        """
        return True if self._client else False

    def register_commands(self, commands: Dict[str, dict[str, Any]]) -> bool:
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

    def _update_manifest_commands(self, commands: Dict[str, dict[str, Any]]) -> bool:
        """
        更新 Slack Manifest 中的 Slash Commands，保留非本实例管理的命令。
        """
        if self._manifest_client is None:
            return False
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
        """
        导出 Slack App Manifest。
        """
        if self._manifest_client is None:
            return None
        result = self._manifest_client.apps_manifest_export(app_id=self._app_id)
        if result and result.get("ok") is False:
            logger.error(f"Slack Manifest 导出失败：{result.get('error')}")
            return None
        manifest = result.get("manifest") if result else None
        if isinstance(manifest, str):
            manifest = json.loads(manifest)
        return manifest if isinstance(manifest, dict) else None

    def _build_slash_commands(self, commands: Dict[str, dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        构建 Slack Manifest Slash Commands 配置。
        """
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
        """
        转换为 Slack Slash Command 名称。
        """
        command = f"/{str(command_text or '').strip().lstrip('/').lower()}"
        if not re.fullmatch(r"/[a-z0-9_-]{1,31}", command):
            return ""
        return command

    @staticmethod
    def _normalize_slack_description(
        description: Optional[str],
        fallback: str,
    ) -> str:
        """
        整理 Slack Slash Command 描述。
        """
        normalized = str(description or fallback or "MoviePilot").strip()
        return normalized[:2000] or "MoviePilot"

    def download_file(self, file_url: str) -> Optional[Tuple[bytes, str]]:
        """
        携带机器人令牌下载 Slack 私有文件。

        响应未声明 MIME 类型时使用 image/jpeg。

        :param file_url: 需要机器人授权才能访问的文件下载地址
        :return: 文件字节与去掉参数的 MIME 类型；未就绪、空响应或失败时返回 None
        """
        if not self._client or not self._oauth_token or not file_url:
            return None
        try:
            headers = {
                "Authorization": f"Bearer {self._oauth_token}",
                "User-Agent": get_runtime_setting('USER_AGENT'),
                "Accept": "*/*",
            }
            resp = RequestUtils(headers=headers, timeout=30).get_res(file_url)
            if resp and resp.content:
                mime_type = resp.headers.get("Content-Type", "image/jpeg")
                return resp.content, mime_type.split(";")[0]
        except Exception as e:
            logger.error(f"下载Slack文件失败: {e}")
        return None

    def _outbound_address(
        self, userid: Optional[str], original_chat_id: Optional[str],
        thread_id: Optional[str], original_message_id: Optional[str] = None,
    ) -> tuple[str, _SlackThread]:
        """
        从显式线程或编辑上下文确定目标，普通通知沿用指定接收人。

        :param userid: 普通通知显式指定的用户或频道
        :param original_chat_id: 交互来源频道
        :param thread_id: 明确要回复的线程时间戳
        :param original_message_id: 编辑的原消息时间戳
        :return: 频道和仅在明确回复线程时发给平台的字段
        """
        if original_chat_id and thread_id:
            return original_chat_id, {"thread_ts": thread_id}
        if original_chat_id and original_message_id:
            return original_chat_id, {}
        return userid or self.__find_public_channel(), {}

    def send_msg(self, title: Optional[str], text: Optional[str] = None,
                 image: Optional[str] = None, link: Optional[str] = None,
                 userid: Optional[str] = None, buttons: Optional[List[List[dict[str, Any]]]] = None,
                 original_message_id: Optional[str] = None,
                 original_chat_id: Optional[str] = None,
                 formatted_text: Optional[str] = None,
                 thread_id: Optional[str] = None) -> Tuple[bool, Any]:
        """
        发送或编辑 Slack 通知，必要时分批提交格式化内容。

        仅首批编辑原消息，剩余批次作为新消息发送；后续失败不会撤销已发送的批次。

        :param title: 消息标题，可与普通正文择一提供；为空时只发送正文
        :param text: 普通正文，兼作通知摘要和格式化失败时的回退
        :param image: 可选图片地址
        :param link: 未提供按钮时显示的详情链接
        :param userid: 接收用户或频道标识；为空时按默认频道名称查找
        :param buttons: 按行组织的链接按钮或回调按钮
        :param original_message_id: 原消息时间戳；与原频道标识同时提供才执行编辑
        :param original_chat_id: 原消息所属频道标识
        :param formatted_text: 可选 Slack mrkdwn 正文，按段落构建内容块
        :param thread_id: 原会话内线程的父消息时间戳
        :return: 成功时返回 (True, 最后一批 SDK 响应)，失败返回 (False, 错误文本)
        """
        if not self._client:
            return False, "消息客户端未就绪"
        if not title and not text:
            return False, "标题和内容不能同时为空"
        try:
            channel, thread = self._outbound_address(userid, original_chat_id, thread_id, original_message_id)
            message_text = f"{title}\n{text or ''}" if title else (text or "")
            batches = self._message_batches(title, text, image, link, buttons, formatted_text, message_text)
            result = None
            for index, (plain_text, batch) in enumerate(batches):
                if index == 0 and original_message_id and original_chat_id:
                    result = self._client.chat_update(
                        channel=original_chat_id, ts=original_message_id,
                        text=plain_text, blocks=batch or [],
                    )
                else:
                    result = self._client.chat_postMessage(
                        channel=channel, text=plain_text, blocks=batch, mrkdwn=True,
                        **thread,
                    )
            return True, result
        except Exception as msg_e:
            logger.error(f"Slack消息发送失败: {msg_e}")
            return False, str(msg_e)

    def send_tracked_message(
        self, message: Message, userid: str | None, *,
        receipt: DeliveryReceipt, part_sender: DeliveryPartSender, plan: DeliveryPartPlanner,
    ) -> bool:
        """
        按明确的 Slack 频道及线程地址保存并发送通知批次。

        :param message: 通知正文及平台可用的消息元素
        :param userid: 显式用户或频道标识
        :param receipt: 记录逐批回执的回调
        :param part_sender: 每批提交前核对投递进度
        :param plan: 首次发送前冻结所有批次
        :return: 全部批次均得到平台确认
        """
        channel, thread = self._tracked_address(message, userid)
        if not channel:
            receipt(None, 0, "skipped", "no_target")
            return False
        address = self._tracked_address_key(channel, thread)
        plain = f"{message.title}\n{message.text or ''}" if message.title else (message.text or "")
        batches = self._message_batches(
            message.title, message.text, message.image, message.link, message.buttons,
            message.channel_text(NotificationChannel.Slack), plain,
        )
        payloads: list[dict[str, Any]] = [
            {"channel": channel, "text": text, "blocks": blocks, "mrkdwn": True, **thread}
            for text, blocks in batches
        ]
        addresses = [address] * len(payloads)
        if payloads and message.original_message_id and message.original_chat_id and not message.private_delivery:
            original = str(message.original_message_id)
            payloads[0] = {"channel": channel, "ts": original, "text": payloads[0]["text"],
                           "blocks": payloads[0]["blocks"] or []}
            addresses[0] = f"channel:{channel}:edit:{original}"
        contents = [json.dumps(payload, ensure_ascii=False, sort_keys=True) for payload in payloads]
        plan(tuple((target, index, content) for index, (target, content) in enumerate(zip(addresses, contents))))
        confirmed = True
        for index, (target, content, payload) in enumerate(zip(addresses, contents, payloads)):
            status, code = part_sender(target, index, content, confirmed, partial(self._post_tracked_batch, payload))
            receipt(target, index, status, code)
            confirmed = confirmed and status == "confirmed"
        if not payloads:
            receipt(address, 0, "skipped", "empty_content")
        return bool(payloads) and confirmed

    def _post_tracked_batch(self, payload: dict[str, Any]) -> DeliveryPartOutcome:
        """
        提交一次平台调用并区分消息确认、明确拒绝和未知结果。
        """
        if not self._notification_client:
            return "retryable_failure", "client_unavailable"
        submit = self._notification_client.chat_update if "ts" in payload else self._notification_client.chat_postMessage
        return self._confirm_tracked_request(partial(submit, **payload))

    @staticmethod
    def _confirm_tracked_request(submit: Callable[[], SlackResponse]) -> DeliveryPartOutcome:
        """
        只依据 Slack 的实际回复结算各批次及上传结果。
        """
        try:
            response = submit()
        except SlackApiError as error:
            if error.response.status_code == 429 or error.response.get("error") == "ratelimited":
                return "retryable_failure", "rate_limited"
            if error.response.get("error") in {"invalid_auth", "channel_not_found", "not_in_channel", "is_archived"}:
                return "permanent_failure", "platform_rejected"
            return "unknown", "platform_unconfirmed"
        except (OSError, SlackRequestError):
            return "unknown", "response_unconfirmed"
        timestamp = response.get("ts")
        if not timestamp:
            file = response.get("file")
            if isinstance(file, dict):
                timestamp = file.get("id")
        if response.get("ok") is True and isinstance(timestamp, str) and timestamp:
            return "confirmed", None, timestamp
        return "unknown", "response_unconfirmed"

    def _tracked_address(self, message: Message, userid: str | None) -> tuple[str, _SlackThread]:
        """
        只解析显式目标和连接内配置的默认频道。
        """
        explicit = message.original_chat_id if not message.private_delivery else None
        if not userid and not explicit and not self._channel:
            return "", {}
        original = str(message.original_message_id) if explicit and message.original_message_id else None
        return self._outbound_address(userid or explicit, explicit, message.thread_id, original)

    @staticmethod
    def _tracked_address_key(channel: str, thread: _SlackThread) -> str:
        """
        区分频道普通通知和线程投递。
        """
        thread_ts = thread.get("thread_ts")
        return f"channel:{channel}:thread:{thread_ts}" if thread_ts else f"channel:{channel}"

    def send_tracked_image(
        self, message: Message, userid: str | None, *, receipt: DeliveryReceipt,
        part_sender: DeliveryPartSender, plan: DeliveryPartPlanner,
    ) -> bool:
        """
        为 Slack 原图冻结实际频道、线程和素材摘要。
        """
        channel, thread = self._tracked_address(message, userid)
        if not channel or not message.image:
            receipt(None, 0, "skipped", "no_target" if not channel else "empty_content")
            return False
        address = self._tracked_address_key(channel, thread)
        loaded = load_image(message.image)
        caption = "\n".join(value for value in (message.title, message.text) if value)
        digest = sha256(loaded[0]).hexdigest() if loaded else None
        content = json.dumps(("image", caption, digest), ensure_ascii=False)
        plan(((address, 0, content),))
        if loaded is None:
            submit = self._missing_tracked_file
        else:
            payload = {"channel": channel, "filename": f"image{loaded[1]}", "title": message.title or "图片",
                       "initial_comment": caption or None, "file": loaded[0], **thread}
            submit = partial(self._post_tracked_file, payload)
        status, code = part_sender(address, 0, content, True, submit)
        receipt(address, 0, status, code)
        return status == "confirmed"

    def send_tracked_file(
        self, message: Message, userid: str | None, *,
        receipt: DeliveryReceipt, part_sender: DeliveryPartSender, plan: DeliveryPartPlanner,
    ) -> bool:
        """
        固定文件内容摘要和实际频道后上传，未确认时保留源文件。
        """
        channel, thread = self._tracked_address(message, userid)
        if not channel or not message.file_path:
            receipt(None, 0, "skipped", "no_target" if not channel else "empty_content")
            return False
        address = self._tracked_address_key(channel, thread)
        path = Path(message.file_path)
        name = message.file_name or path.name
        payload: dict[str, Any] = {
            "channel": channel, "filename": name, "title": message.title or name,
            "initial_comment": "\n".join(value for value in (message.title, message.text) if value) or None,
            **thread,
        }
        try:
            if not path.is_file():
                raise FileNotFoundError
            file = path.open("rb")
        except OSError:
            content = json.dumps((None, payload), ensure_ascii=False, sort_keys=True)
            plan(((address, 0, content),))
            status, code = part_sender(address, 0, content, True, self._missing_tracked_file)
        else:
            with file:
                digest = file_digest(file, "sha256").hexdigest()
                file.seek(0)
                content = json.dumps((digest, payload), ensure_ascii=False, sort_keys=True)
                plan(((address, 0, content),))
                status, code = part_sender(
                    address, 0, content, True, partial(self._post_tracked_file, {**payload, "file": file}),
                )
        receipt(address, 0, status, code)
        return status == "confirmed"

    @staticmethod
    def _missing_tracked_file() -> DeliveryPartOutcome:
        """
        报告发送前发现的源文件缺失。
        """
        return "permanent_failure", "attachment_unavailable"

    def _post_tracked_file(self, payload: dict[str, Any]) -> DeliveryPartOutcome:
        """
        获准后提交一次 Slack 文件上传。
        """
        if not self._notification_client:
            return "retryable_failure", "client_unavailable"
        return self._confirm_tracked_request(partial(self._notification_client.files_upload_v2, **payload))

    def _message_batches(
        self, title: str | None, text: str | None, image: str | None, link: str | None,
        buttons: list[list[dict[str, Any]]] | None, formatted_text: str | None, message_text: str,
    ) -> list[tuple[str, list[dict[str, Any]]]]:
        """
        将普通发送与持久发送共同使用的内容拆分为 Slack API 批次。
        """
        blocks = self._message_blocks(title, text, image, link, buttons, formatted_text)
        if buttons:
            for button_row in buttons:
                elements: list[dict[str, Any]] = []
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
        elif link:
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

        return [(message_text[:1000], batch) for batch in self._block_batches(blocks)] if blocks else [
            (message_text[offset:offset + 4000], []) for offset in range(0, len(message_text), 4000)
        ]

    @classmethod
    def _message_blocks(cls, title: Optional[str], text: Optional[str], image: Optional[str],
                        link: Optional[str], buttons: Optional[List[List[Dict[str, Any]]]],
                        formatted_text: Optional[str]) -> List[Dict[str, Any]]:
        """
        选择渠道正文或原有图文结构，按钮仍由发送入口统一添加。

        无标题时使用正文；图片说明使用正文时最多保留 2000 字符，避免超过 SDK 限制。
        """
        blocks: list[dict[str, Any]]
        blocks = cls._formatted_blocks(formatted_text) if formatted_text else []
        if blocks:
            blocks.insert(0, {"type": "header", "text": {
                "type": "plain_text", "text": (title or "MoviePilot")[:150]
            }})
        elif image:
            blocks.append({"type": "section", "text": {
                "type": "mrkdwn",
                "text": f"*{title}*\n{text or ''}" if title else (text or " ")
            }, 'accessory': {
                "type": "image",
                "image_url": f"{image}",
                "alt_text": title or (text or "image")[:2000]
            }})
        elif buttons or link:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{title}*\n{text or ''}" if title else (text or " "),
                },
            })
        return blocks

    @classmethod
    def _formatted_blocks(cls, text: str) -> List[Dict[str, Any]]:
        """
        按段落生成 Block Kit，站点数量不限；过长单段仍回退原通知。
        """
        paragraphs = [part for part in text.split("\n\n") if part]
        if any(len(part) > cls._SECTION_TEXT_LIMIT for part in paragraphs):
            return []
        return [{"type": "section", "text": {
            "type": "mrkdwn", "text": part, "verbatim": True
        }} for part in paragraphs]

    @classmethod
    def _block_batches(cls, blocks: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        """
        拆分长报告的 Block Kit，每条保留标题和操作入口。
        """
        headers = [block for block in blocks if block["type"] == "header"]
        actions = [block for block in blocks if block["type"] == "actions"]
        content = [block for block in blocks if block["type"] not in {"header", "actions"}]
        capacity = max(1, cls._FORMATTED_BLOCK_LIMIT - len(actions))
        batches = [blocks]
        if len(blocks) > 50 and headers:
            batches = [headers + content[index:index + capacity] + actions
                       for index in range(0, len(content), capacity)]
        return batches

    def send_image(self, image: str, title: Optional[str] = None, text: Optional[str] = None,
                   userid: Optional[str] = None, buttons: Optional[List[List[dict[str, Any]]]] = None) -> Tuple[bool, Any]:
        """
        上传完整原图，保留说明及可选的交互按钮，不依赖外部图片地址可公开访问。

        :param image: HTTP(S)、Base64 或本地 JPG/PNG
        :param title: 图片说明标题
        :param text: 图片说明正文
        :param userid: 接收用户或频道，沿用宿主解析的目标
        :param buttons: 可选的交互按钮，随说明发送一次
        :return: 沿用文件上传的成功标记和响应；原图或说明失败时返回 False 及原因
        """
        with image_upload_file(image) as path:
            if path is None:
                return False, "图片不可用"
            if buttons:
                sent, _ = self.send_msg(title=title or (None if text else "图片"), text=text,
                                        userid=userid, buttons=buttons)
                if not sent:
                    return False, "图片说明发送失败"
                title, text = None, None
            return self.send_file(str(path), title=title, text=text, userid=userid)

    def send_file(
        self,
        file_path: str,
        title: Optional[str] = None,
        text: Optional[str] = None,
        userid: Optional[str] = None,
        file_name: Optional[str] = None,
        original_chat_id: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> Tuple[bool, Any]:
        """
        上传本地文件，并附带标题和正文。

        :param file_path: 现有本地普通文件路径
        :param title: 文件标题；省略时使用上传文件名
        :param text: 可选附言，与标题合并为初始评论
        :param userid: 接收用户或频道标识；为空时查找默认频道
        :param file_name: 可选上传名称，省略时使用本地文件名
        :param original_chat_id: 上传文件所在的原会话
        :param thread_id: 原会话内线程的父消息时间戳
        :return: 成功返回 (True, SDK 响应)，失败返回 (False, 错误文本)
        """
        if not self._client:
            return False, "消息客户端未就绪"
        if not file_path:
            return False, "文件路径不能为空"

        local_file = Path(file_path)
        if not local_file.exists() or not local_file.is_file():
            return False, f"文件不存在: {local_file}"

        try:
            channel, thread = self._outbound_address(userid, original_chat_id, thread_id)

            comment_parts = [part for part in [title, text] if part]
            initial_comment = "\n".join(comment_parts) if comment_parts else None

            with local_file.open("rb") as fp:
                result = self._client.files_upload_v2(
                    channel=channel,
                    file=fp,
                    filename=file_name or local_file.name,
                    title=title or (file_name or local_file.name),
                    initial_comment=initial_comment,
                    **thread,
                )
            return True, result
        except Exception as err:
            logger.error(f"Slack文件发送失败: {err}")
            return False, str(err)

    def add_reaction(self, channel: str, timestamp: str, emoji: str) -> bool:
        """
        给消息添加表情反应。

        :param channel: 消息所在频道标识
        :param timestamp: 消息时间戳
        :param emoji: Slack 表情名称，不含冒号
        :return: 请求成功返回 True；缺少参数、客户端未就绪或请求失败返回 False
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
        移除消息上的表情反应。

        :param channel: 消息所在频道标识
        :param timestamp: 消息时间戳
        :param emoji: Slack 表情名称，不含冒号
        :return: 请求成功返回 True；缺少参数、客户端未就绪或请求失败返回 False
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
                        buttons: Optional[List[List[dict[str, Any]]]] = None,
                        original_message_id: Optional[str] = None,
                        original_chat_id: Optional[str] = None,
                        thread_id: Optional[str] = None) -> Optional[bool]:
        """
        发送或编辑带选择按钮的媒体列表。

        :param medias: 待展示媒体列表
        :param userid: 接收用户或频道标识；为空时查找默认频道
        :param title: 列表标题
        :param buttons: 自定义按钮行；提供时替代每个条目的默认选择按钮
        :param original_message_id: 原消息时间戳，与原频道标识同时提供才执行编辑
        :param original_chat_id: 原消息所属频道标识
        :param thread_id: 原会话内线程的父消息时间戳
        :return: 收到有效 SDK 响应时返回 True；发送失败返回 False；空媒体列表不发送
        """
        elements: list[dict[str, Any]]
        blocks: list[dict[str, Any]]
        title_section: dict[str, Any]
        if not self._client:
            return False
        if not medias:
            return False
        try:
            channel, thread = self._outbound_address(userid, original_chat_id, thread_id, original_message_id)
            title_section = {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{title}*"
                }
            }
            blocks = [title_section]
            if medias:
                blocks.append({
                    "type": "divider"
                })
                # 如果有自定义按钮，先添加所有媒体项，然后添加统一的按钮
                if buttons:
                    for index, media in enumerate(medias, start=1):
                        if media.get_poster_image():
                            if media.vote_star:
                                text = f"{index}. *<{media.detail_link}|{media.title_year}>*" \
                                    f"\n类型：{media.type.value if media.type else ""}" \
                                    f"\n{media.vote_star}" \
                                    f"\n{media.get_overview_string(50)}"
                            else:
                                text = f"{index}. *<{media.detail_link}|{media.title_year}>*" \
                                    f"\n类型：{media.type.value if media.type else ""}" \
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
                    for index, media in enumerate(medias, start=1):
                        if media.get_poster_image():
                            if media.vote_star:
                                text = f"{index}. *<{media.detail_link}|{media.title_year}>*" \
                                    f"\n类型：{media.type.value if media.type else ""}" \
                                    f"\n{media.vote_star}" \
                                    f"\n{media.get_overview_string(50)}"
                            else:
                                text = f"{index}. *<{media.detail_link}|{media.title_year}>*" \
                                    f"\n类型：{media.type.value if media.type else ""}" \
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

            if original_message_id and original_chat_id:
                result = self._client.chat_update(
                    channel=original_chat_id,
                    ts=original_message_id,
                    text=title,
                    blocks=blocks or []
                )
            else:
                result = self._client.chat_postMessage(
                    channel=channel,
                    text=title,
                    blocks=blocks,
                    **thread,
                )
            return True if result else False
        except Exception as msg_e:
            logger.error(f"Slack消息发送失败: {msg_e}")
            return False

    def send_torrents_msg(self, torrents: List[Context], userid: Optional[str] = None, title: Optional[str] = None,
                          buttons: Optional[List[List[dict[str, Any]]]] = None,
                          original_message_id: Optional[str] = None,
                          original_chat_id: Optional[str] = None,
                          thread_id: Optional[str] = None) -> Optional[bool]:
        """
        发送或编辑带选择按钮的种子列表。

        :param torrents: 待展示种子列表
        :param userid: 接收用户或频道标识；为空时查找默认频道
        :param title: 列表标题
        :param buttons: 自定义按钮行；提供时替代每个条目的默认选择按钮
        :param original_message_id: 原消息时间戳，与原频道标识同时提供才执行编辑
        :param original_chat_id: 原消息所属频道标识
        :param thread_id: 原会话内线程的父消息时间戳
        :return: 收到有效 SDK 响应时返回 True；发送失败返回 False；客户端未就绪返回 None
        """
        elements: list[dict[str, Any]]
        blocks: list[dict[str, Any]]
        title_section: dict[str, Any]
        if not self._client:
            return None

        try:
            channel, thread = self._outbound_address(userid, original_chat_id, thread_id, original_message_id)
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
            index = 1

            # 如果有自定义按钮，先添加种子列表，然后添加统一的按钮
            if buttons:
                for context in torrents:
                    torrent = context.torrent_info
                    if not torrent:
                        continue
                    site_name = torrent.site_name
                    meta = MetaInfo(torrent.title or "", torrent.description)
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
                for context in torrents:
                    torrent = context.torrent_info
                    if not torrent:
                        continue
                    site_name = torrent.site_name
                    meta = MetaInfo(torrent.title or "", torrent.description)
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

            if original_message_id and original_chat_id:
                result = self._client.chat_update(
                    channel=original_chat_id,
                    ts=original_message_id,
                    text=title,
                    blocks=blocks or []
                )
            else:
                result = self._client.chat_postMessage(
                    channel=channel,
                    text=title,
                    blocks=blocks,
                    **thread,
                )
            return True if result else False
        except Exception as msg_e:
            logger.error(f"Slack消息发送失败: {msg_e}")
            return False

    def delete_msg(self, message_id: str, chat_id: Optional[str] = None) -> Optional[bool]:
        """
        按频道和消息时间戳删除 Slack 消息。

        :param message_id: 待删除消息的 Slack 时间戳
        :param chat_id: 消息所在频道标识；为空时查找默认频道
        :return: 删除成功返回 True；客户端未就绪返回 None；其他失败返回 False
        """
        if not self._client:
            return None

        try:
            if chat_id:
                target_channel = chat_id
            else:
                target_channel = self.__find_public_channel()

            if not target_channel:
                logger.error("无法确定要删除消息的Slack频道")
                return False

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

    def __find_public_channel(self) -> str:
        """
        在可见公共及私有频道中按名称查找默认接收频道。

        :return: 首个同名频道标识；未配置名称时查找“全体”，不可用时返回空字符串
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
                        conversation_id = str(channel.get("id") or "")
                        break
        except Exception as e:
            logger.error(f"查找Slack公共频道失败: {str(e)}")
        return conversation_id
