"""
将现有 MoviePilot 通知经私有 Server 投递到 App 或主屏幕通知网页。
"""

from typing import Any, Optional, Union

from app.adapters.external.server import MoviePilotServerHelper
from app.modules import _MessageBase, _ModuleBase
from app.runtime.log import logger
from app.schemas.message import Message
from app.schemas.system import NotificationConf
from app.schemas.types import ModuleType, NotificationChannel


class AppPushModule(_ModuleBase, _MessageBase[object]):
    """
    复用现有渠道配置、消息类型开关和用户路由，不持有 Apple 或 VAPID 私钥。
    """

    _notification_user_target_keys = ("apppush",)

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
        从渠道配置读取唯一中转地址和实例密钥，不将密钥返回客户端或写入日志。
        """
        config = conf.config or {}
        return MoviePilotServerHelper.relay_push(str(config.get("PUSH_SERVER_URL") or ""),
                                                str(config.get("PUSH_RELAY_KEY") or ""), method, path, data)

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
                data.update(owner=owner, relay_url=str((conf.config or {}).get("PUSH_SERVER_URL") or "").rstrip("/") + "/push/app/")
            elif action == "pair":
                data = self._request(conf, "POST", "pairings", {"owner": owner, "origin": origin, "installation": installation})
                data["relay_url"] = str((conf.config or {}).get("PUSH_SERVER_URL") or "").rstrip("/") + "/push/app/"
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
