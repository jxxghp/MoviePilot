"""
将现有 MoviePilot 通知经私有 Server 投递到 App 或主屏幕通知网页。
"""

import re
import secrets
import threading
from collections.abc import Callable
from typing import Any, Optional, Union

from app.adapters.external.server import MoviePilotServerHelper
from app.application.configuration import get_configured_system_config
from app.modules import _MessageBase, _ModuleBase
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.message import Message
from app.schemas.system import NotificationConf
from app.schemas.types import ModuleType, NotificationChannel, SystemConfigKey


class AppPushModule(_ModuleBase, _MessageBase[object]):
    """
    复用现有渠道配置、消息类型开关和用户路由，不持有 Apple 或 VAPID 私钥。

    :cvar _relay_lock: 同一进程内串行创建、恢复与轮换凭据，数据库写入使用配置服务原子操作
    """

    _notification_user_target_keys = ("apppush",)
    _relay_lock = threading.RLock()

    def init_module(self) -> None:
        """
        声明 AppPush 渠道，配置随现有通知配置生命周期更新。
        """
        super().init_service(service_name="apppush")
        self._channel = NotificationChannel.AppPush

    @staticmethod
    def get_name() -> str:
        """
        提供渠道目录与前端配置使用的规范名称。
        """
        return "AppPush"

    @staticmethod
    def get_type() -> ModuleType:
        """
        声明为通知模块，沿用消息队列和分发器。
        """
        return ModuleType.Notification

    @staticmethod
    def get_subtype() -> NotificationChannel:
        """
        返回 AppPush 的渠道枚举。
        """
        return NotificationChannel.AppPush

    @staticmethod
    def get_priority() -> int:
        """
        与其他单向通知渠道一起分发，不拦截原 Web Push。
        """
        return 7

    def init_setting(self) -> Optional[tuple[str, Union[str, bool]]]:
        """
        由通知渠道配置启用，不新增全局环境开关。
        """
        return None

    def stop(self) -> None:
        """
        HTTP 使用共享请求入口，本模块没有长期连接需要关闭。
        """

    def test(self) -> tuple[bool, str]:
        """
        检查配置是否存在，实际投递由绑定用户的测试操作验证。
        """
        return (True, "") if self.get_configs() else (False, "请先启用 AppPush 渠道")

    def _request(self, conf: NotificationConf, method: str, path: str, data: dict[str, Any]) -> dict[str, Any]:
        """
        使用手工或自动接入凭据，不将内部凭据返回客户端或写入日志。
        """
        config = conf.config or {}
        server = self._server_url(conf)
        key = str(config.get("PUSH_RELAY_KEY") or "") or self._automatic_key(server)
        return MoviePilotServerHelper.relay_push(server, key, method, path, data)

    @staticmethod
    def _server_url(conf: NotificationConf) -> str:
        """
        空地址沿用既有中心服务配置，地址规范化同时决定本机凭据作用域。
        """
        return MoviePilotServerHelper.normalize_relay_url(str((conf.config or {}).get("PUSH_SERVER_URL")
                                                              or get_runtime_setting("MP_SERVER_HOST") or ""))

    @staticmethod
    def _save_registration(server: str, change: Callable[[dict[str, str]], dict[str, str]]) -> dict[str, str]:
        """
        在短事务内更新一个中心的凭据，不跨网络持有数据库事务或覆盖其他中心状态。

        :param server: 已校验且规范化的服务根地址
        :param change: 纯状态变更；输入、输出只含内部字符串字段
        :return: 已提交的该中心状态，不得交给客户端
        """
        def update(current: Any) -> tuple[dict[str, str], dict[str, Any]]:
            """
            合并最新持久化值，拒绝损坏状态而非静默生成另一份实例身份。
            """
            if current is not None and not isinstance(current, dict):
                raise ValueError("AppPush 接入状态损坏，请从备份恢复")
            values = dict(current or {})
            prior = values.get(server) or {}
            if not isinstance(prior, dict) or any(not isinstance(value, str) for value in prior.values()):
                raise ValueError("AppPush 接入状态损坏，请从备份恢复")
            registration = change(dict(prior))
            values[server] = registration
            return registration, values

        return get_configured_system_config().update_atomically(SystemConfigKey.AppPushCredentials, update)

    def _automatic_key(self, server: str, *, rotate: bool = False) -> str:
        """
        先持久化随机凭据，再幂等登记；候选轮换凭据先写入，重启后接续相同请求。

        :param server: 凭据所属的精确服务根地址
        :param rotate: 仅由容器管理员专用入口请求轮换；正常操作只恢复已有轮换
        :return: 当前已登记的内部发送凭据
        :raises ValueError: 身份或持久化失败、中心拒绝登记/轮换，不自动冒用 UID 恢复
        """
        with self._relay_lock:
            registration = self._save_registration(server, lambda old: old or {
                "key": "mpa_" + secrets.token_urlsafe(32), "instance": "", "pending": "",
            })
            if not re.fullmatch(r"mpa_[a-zA-Z0-9_-]{43}", registration.get("key", "")):
                raise ValueError("AppPush 接入凭据损坏，请从备份恢复")
            if not registration.get("instance"):
                uid = MoviePilotServerHelper.get_user_uid()
                if not uid:
                    raise ValueError("无法取得 MoviePilot 用户唯一 ID")
                result = MoviePilotServerHelper.relay_push(server, registration["key"], "POST", "instances", {"user_uid": uid})
                instance = result.get("instance")
                if not isinstance(instance, str) or not re.fullmatch(r"[a-f0-9]{32}", instance):
                    raise ValueError("中心服务接入响应无效")
                registration = self._save_registration(server, lambda old: {**old, "instance": instance})
            if rotate:
                registration = self._save_registration(server, lambda old: {
                    **old, "pending": old.get("pending") or "mpa_" + secrets.token_urlsafe(32),
                })
            if registration.get("pending"):
                pending = registration["pending"]
                result = MoviePilotServerHelper.relay_push(server, registration["key"], "PUT", "instances/credentials", {"key": pending})
                if result.get("instance") != registration["instance"] or result.get("rotated") is not True:
                    raise ValueError("中心服务轮换响应无效")

                def promote(old: dict[str, str]) -> dict[str, str]:
                    """
                    比较当前世代再提交，避免跨进程迟到的响应覆盖下一次轮换。
                    """
                    if old.get("key") == pending:
                        return old
                    if old.get("key") != registration["key"] or old.get("pending") != pending:
                        raise ValueError("AppPush 凭据已被其他操作更新，请重试")
                    return {**old, "key": pending, "pending": ""}

                registration = self._save_registration(server, promote)
            return registration["key"]

    def channel_manage(self, channel: str, action: str, **params: Any) -> Optional[dict[str, Any]]:
        """
        处理已鉴权的设备管理；owner 只由专用端点注入，不使用通用管理员动作的隐式广播。
        """
        if channel != "AppPush":
            return None
        configs = self.get_configs()
        source = params.get("source")
        conf = configs.get(source) if source else next(iter(configs.values()), None)
        owner = params.get("owner")
        if not conf or not isinstance(owner, str) or not owner:
            return {"success": False, "message": "请先配置并启用 AppPush 私有通知渠道"}
        installation = params.get("installation")
        origin = params.get("origin")
        try:
            if action == "status":
                data = self._request(conf, "GET", "devices", {"owner": owner})
                data.update(owner=owner, relay_url=self._server_url(conf) + "/push/app/")
            elif action == "pair":
                data = self._request(conf, "POST", "pairings", {"owner": owner, "origin": origin, "installation": installation})
                data["relay_url"] = self._server_url(conf) + "/push/app/"
            elif action == "rotate_instance" and params.get("administrative") is True:
                if (conf.config or {}).get("PUSH_RELAY_KEY"):
                    return {"success": False, "message": "手工凭据请由中心运营者轮换"}
                self._automatic_key(self._server_url(conf), rotate=True)
                data = {"rotated": True}
            elif action == "register":
                if not params.get("token") or not params.get("topic"):
                    return {"success": False, "message": "尚未取得系统推送令牌"}
                data = self._request(conf, "POST", "devices", {key: params[key] for key in (
                    "owner", "origin", "installation", "token", "topic", "environment",
                )})
            elif action == "unregister":
                data = self._request(conf, "DELETE", "devices", {"owner": owner, "installation": installation})
            elif action == "test":
                data = self._request(conf, "POST", "messages", {"owner": owner, "title": "MoviePilot 测试通知", "body": "通知服务已连接"})
            else:
                return {"success": False, "message": "不支持的 AppPush 操作"}
            return {"success": True, "data": data}
        except ValueError as error:
            return {"success": False, "message": str(error)}

    def post_message(self, message: Message, **kwargs: object) -> None:
        """
        仅向路由确定的不可变账号身份投递；缺少绑定绝不退回全设备广播。
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            owner = str((self.get_notification_targets(message) or {}).get("apppush") or "")
            if not owner:
                continue
            link = message.link or "/?shotcut=message"
            # 外部下载或媒体链接留在消息列表中，系统通知点击始终交给本实例。
            route = link if link.startswith("/") and not link.startswith("//") and "\\" not in link else "/?shotcut=message"
            try:
                result = self._request(conf, "POST", "messages", {
                    "owner": owner, "title": (message.title or "MoviePilot")[:256],
                    "body": (message.text or "")[:1000], "route": route,
                })
                if result.get("failed"):
                    logger.warning(f"AppPush 有 {len(result['failed'])} 个设备投递失败，请检查 Server 配置")
            except ValueError as error:
                logger.warning(f"AppPush 投递失败：{error}")
