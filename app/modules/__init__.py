"""
模块生命周期、配置化服务实例和消息路由的公共基类。
"""

import re
import threading
from abc import ABCMeta, abstractmethod
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Callable, Dict, Generic, Mapping, Optional, Protocol, Tuple, Type, TypeVar, Union

from app.application.messaging.attachment import notification_parts
from app.application.messaging.channel.rules import notification_rule_applies, routed_notification_messages
from app.application.messaging.delivery import (
    delivery_status,
    legacy_attachment_layout_active,
    notification_preflight_active,
    notification_rules_for_dispatch,
    platform_message_id,
)
from app.runtime.extensions.service import ServiceConfigHelper
from app.runtime.log import logger
from app.runtime.reload import ConfigReloadMixin
from app.schemas.delivery import DeliveryResult, DeliveryStatus
from app.schemas.message import Message, MessageResponse
from app.schemas.system import DownloaderConf, MediaServerConf, NotificationConf
from app.schemas.types import (
    DownloaderType,
    MediaRecognizeType,
    MediaServerType,
    ModuleType,
    NotificationChannel,
    OtherModulesType,
    StorageSchema,
    SystemConfigKey,
)


class _ModuleBase(ConfigReloadMixin, metaclass=ABCMeta):
    """
    模块调用与生命周期的公共约定。

    链路调用返回 None 表示本模块未处理，由调度方继续尝试后续模块。
    输入与输出结构相同或没有返回值的能力，可由多个模块依次执行。
    """

    # 宿主模块的配置事件由统一适配器协调，避免同一配置版本被重复重载。
    CONFIG_RELOAD_MANAGED_EXTERNALLY = True

    def __init__(self) -> None:
        """
        初始化模块生命周期锁。
        """
        super().__init__()
        self._reload_lock = threading.RLock()

    def on_config_changed(self) -> None:
        """
        串行停止旧资源并按最新配置重新初始化模块。
        """
        with self._reload_lock:
            try:
                self.stop()
            except Exception as err:
                logger.error(
                    f"停止 {self.get_reload_name()} 旧资源失败，继续按最新配置初始化：{err}"
                )
            self.init_module()

    def get_reload_name(self) -> Optional[str]:
        """
        返回模块声明的日志名称，未实现元数据方法时保留 None。
        """
        return self.get_name()

    @abstractmethod
    def init_module(self) -> None:
        """
        模块初始化。
        """
        pass

    @abstractmethod
    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """
        声明决定模块是否启用的配置项。

        配置值为 True 时按设置是否有值判断启用；多个选择值可使用逗号分隔。

        :return: 配置键与匹配值；返回 None 表示没有独立开关
        """
        pass

    @staticmethod
    def get_name() -> Optional[str]:
        """
        获取模块名称。
        """
        pass

    @staticmethod
    def get_type() -> Optional[ModuleType]:
        """
        获取模块类型。
        """
        pass

    @staticmethod
    def get_subtype() -> Union[
        DownloaderType,
        MediaServerType,
        NotificationChannel,
        StorageSchema,
        OtherModulesType,
        MediaRecognizeType,
        None,
    ]:
        """
        获取模块子类型（下载器、媒体服务器、消息通道、存储类型、其他杂项模块类型）。
        """
        pass

    @staticmethod
    def get_priority() -> Optional[int]:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效。
        """
        pass

    @abstractmethod
    def stop(self) -> Optional[bool]:
        """
        释放模块持有的服务与后台资源。

        :return: False 表示资源尚未停止；None 或 True 表示本次停止完成
        """
        pass

    @abstractmethod
    def test(self) -> Optional[Tuple[bool, str]]:
        """
        检查模块配置或外部服务是否可用。

        :return: 是否通过及失败说明；模块未启用时返回 None；不提供独立检查的
            已运行模块应返回成功结果并说明无需探测，而不是返回 None
        """
        pass


class _ServiceConfig(Protocol):
    """
    服务配置提供的名称与构造参数；保留现有模型允许的空配置。
    """

    @property
    def name(self) -> Optional[str]:
        """
        读取服务配置的名称。

        :return: 配置名称；尚未命名时返回 None
        """
        ...

    @property
    def config(self) -> Optional[Mapping[str, object]]:
        """
        读取服务构造参数。

        :return: 原始参数映射；空配置仍返回 None，由实例化入口报告错误
        """
        ...


# 定义泛型，用于表示具体的服务类型和配置类型
TService = TypeVar("TService", bound=object)
TConf = TypeVar("TConf", bound=_ServiceConfig)


class ServiceBase(Generic[TService, TConf], metaclass=ABCMeta):
    """
    抽象服务基类，负责服务的初始化、获取实例和配置管理。
    """

    def __init__(self) -> None:
        """
        建立尚未绑定服务类型的配置和实例容器。
        """
        self._configs: Optional[Dict[Optional[str], TConf]] = None
        self._instances: Optional[Dict[Optional[str], TService]] = None
        self._service_name: Optional[str] = None
        self._init_errors: Dict[Optional[str], str] = {}

    def init_service(self, service_name: str,
                     service_type: Optional[Union[Type[TService], Callable[..., TService]]] = None) -> None:
        """
        按服务类型读取配置并创建实例。

        传入类时按配置展开关键字参数；传入工厂时把整个配置对象作为参数。
        先在临时映射中逐个构造实例再整体切换，避免热重载中途异常导致已运行映射被清空或停留在半初始化状态；
        单条配置实例化失败时记录错误并隔离其余合法配置，仅当全部已启用配置均失败时抛出首个异常。

        :param service_name: 匹配配置中 type 的服务类型名称，不接受空值
        :param service_type: 服务类或接收配置对象的工厂；为空时只读取配置
        :raises Exception: 服务名称为空或全部已启用配置均实例化失败
        :raises TypeError: 通过类构造服务时配置参数为空且无其他可用配置
        """
        if not service_name:
            raise Exception("service_name is null")
        self._service_name = service_name
        configs = self.get_configs()
        if configs is None:
            return
        if not service_type:
            self._configs = configs
            self._instances = {}
            self._init_errors = {}
            return
        instances, init_errors, first_error = self._build_service_instances(
            service_name=service_name,
            service_type=service_type,
            configs=configs,
        )
        self._configs = configs
        self._instances = instances
        self._init_errors = init_errors
        if configs and not instances and first_error is not None:
            raise first_error

    def _build_service_instances(
        self,
        *,
        service_name: str,
        service_type: Union[Type[TService], Callable[..., TService]],
        configs: Mapping[Optional[str], TConf],
    ) -> Tuple[Dict[Optional[str], TService], Dict[Optional[str], str], Optional[Exception]]:
        """
        在独立临时映射中实例化全部已启用配置，并隔离单条配置的初始化异常。

        :param service_name: 服务类型名称，用于错误日志标识
        :param service_type: 服务类或接收配置对象的工厂
        :param configs: 已启用的服务配置映射
        :return: 成功实例映射、失败配置错误映射及首个异常对象
        """
        instances: Dict[Optional[str], TService] = {}
        init_errors: Dict[Optional[str], str] = {}
        first_error: Optional[Exception] = None
        for conf in configs.values():
            try:
                # 通过服务类型或工厂函数来创建实例
                if isinstance(service_type, type):
                    # 如果传入的是类类型，调用构造函数实例化
                    instances[conf.name] = service_type(
                        **self._service_kwargs(conf)
                    )
                else:
                    # 如果传入的是工厂函数，直接调用工厂函数
                    instances[conf.name] = service_type(conf)
            except Exception as err:
                if first_error is None:
                    first_error = err
                init_errors[conf.name] = str(err)
                logger.error(
                    f"初始化 {service_name} 服务实例 {conf.name or '<未命名>'} 失败：{err}"
                )
        return instances, init_errors, first_error

    def get_init_errors(self) -> Dict[Optional[str], str]:
        """
        读取最近一次初始化中失败的服务实例错误映射。

        :return: 配置名称到错误说明的映射副本；全部成功或尚未初始化时返回空字典
        """
        return dict(getattr(self, "_init_errors", None) or {})

    def _service_kwargs(self, conf: TConf) -> Dict[str, Any]:
        """
        构建类服务实例参数，允许具体模块传递稳定配置身份。
        """
        name = conf.name
        config = conf.config
        if config is None:
            # 保留原字典解包对空配置的 TypeError，不能静默实例化默认连接。
            raise TypeError("'NoneType' object is not a mapping")
        return {"name": name, **config}

    def get_instances(self) -> Dict[Optional[str], TService]:
        """
        读取已创建的服务实例。

        :return: 按配置名称索引的实例映射；尚未初始化或没有实例时返回空字典
        """
        return self._instances or {}

    def get_instance(self, name: Optional[str] = None) -> Optional[TService]:
        """
        获取指定名称的服务实例。

        :param name: 实例名称，可选。如果为 None，则返回默认实例
        :return: 返回符合条件的服务实例，若不存在则返回 None
        """
        if not self._instances:
            return None
        if name:
            return self._instances.get(name)
        name = self.get_default_config_name()
        return self._instances.get(name) if name else None

    @abstractmethod
    def get_configs(self) -> Dict[Optional[str], TConf]:
        """
        获取已启用的服务配置字典。

        :return: 返回配置字典
        """
        pass

    def get_config(self, name: Optional[str] = None) -> Optional[TConf]:
        """
        获取指定名称的服务配置。

        :param name: 配置名称，可选。如果为 None，则返回默认服务配置
        :return: 返回符合条件的配置，若不存在则返回 None
        """
        if not self._configs:
            return None
        if name:
            return self._configs.get(name)
        name = self.get_default_config_name()
        return self._configs.get(name) if name else None

    def get_default_config_name(self) -> Optional[str]:
        """
        获取默认服务配置的名称。

        优先返回被显式标记为默认的配置。只有在没有任何配置带标记时，才退回到第一个配置——
        配置的先后来自读取顺序，用户既看不见也控制不了，删掉一个配置会让「默认」静默改指
        另一个，因此它只能作为无标记时的兜底，不能盖过用户的显式选择。

        :return: 默认配置的名称，无配置时为 None
        """
        configs = self._configs
        if configs is None:
            return None
        marked = next(
            (conf for conf in configs.values() if getattr(conf, "default", False)),
            None,
        )
        if marked:
            return marked.name
        first_conf = next(iter(configs.values()), None)
        return first_conf.name if first_conf else None


class _MessageBase(ServiceBase[TService, NotificationConf]):
    """
    消息基类。
    """
    CONFIG_WATCH = {SystemConfigKey.Notifications.value, SystemConfigKey.ChannelSettings.value}
    # 当前用户个人信息中允许用于定向投递的渠道绑定键。
    _notification_user_target_keys: tuple[str, ...] = ()
    # 管理员未绑定个人 ID 时可作为管理员默认目标的渠道配置键。
    _notification_admin_default_config_keys: tuple[str, ...] = ()

    def __init__(self) -> None:
        """
        初始化消息基类，并设置消息通道。
        """
        super().__init__()
        self._channel: Optional[NotificationChannel] = None

    def get_configs(self) -> Dict[Optional[str], NotificationConf]:
        """
        获取已启用的消息通知渠道的配置字典。

        :return: 返回消息通知的配置字典
        """
        configs = ServiceConfigHelper.get_notification_configs()
        if not self._service_name:
            return {}
        return {conf.name: conf for conf in configs if conf.type == self._service_name and conf.enabled}

    def prepare_message(self, message: Message) -> Message:
        """
        在发送边界验证强制私聊的实例和个人地址，清除群回复及编辑上下文。

        :param message: 不在本方法中修改的原始通知
        :return: 通过检查的私聊副本；普通或其他平台的消息保持原样
        :raises PermissionError: 平台不支持、实例不明确、缺少用户或地址属于群
        """
        if not message.private_delivery:
            return message
        if message.channel is None:
            raise PermissionError("强制私聊必须明确指定平台，不能按实例名称广播")
        channel = self._channel or getattr(self, "get_subtype")()
        if message.channel is not None and message.channel != channel:
            return message
        if channel not in {
            NotificationChannel.Telegram, NotificationChannel.Wechat, NotificationChannel.WechatClawBot,
            NotificationChannel.Feishu, NotificationChannel.QQ, NotificationChannel.Discord,
            NotificationChannel.Slack, NotificationChannel.VoceChat, NotificationChannel.SynologyChat,
        }:
            raise PermissionError("该渠道不支持机器人个人私聊投递")
        if not message.source or message.userid is None or not str(message.userid).strip():
            raise PermissionError("强制私聊必须指定渠道实例和个人用户 ID")
        if message.source not in self.get_configs():
            raise RuntimeError("私聊渠道实例不存在或已停用")
        userid = str(message.userid).strip()
        if userid.lower().startswith(("group:", "gid#")) or userid.endswith("@chatroom"):
            raise PermissionError("强制私聊不能使用群地址")
        if channel == NotificationChannel.Telegram and (not userid.isascii() or not userid.isdecimal() or int(userid) <= 0):
            raise PermissionError("Telegram 私聊必须使用正整数个人用户 ID")
        if channel == NotificationChannel.Wechat and (
                userid.lower() == "@all" or not re.fullmatch(r"[A-Za-z0-9_@.-]+", userid)
        ):
            raise PermissionError("企业微信私聊必须是单个个人用户，不能使用全体或多用户地址")
        if channel == NotificationChannel.Slack and not re.fullmatch(r"[UW][A-Z0-9]+", userid):
            raise PermissionError("Slack 私聊必须使用个人用户 ID，不能使用频道或频道名称")
        if channel == NotificationChannel.Feishu and userid.startswith("oc_"):
            raise PermissionError("飞书私聊不能使用群会话 ID")
        return message.model_copy(update={
            "channel": channel, "userid": userid, "targets": None,
            "original_chat_id": None, "original_message_id": None, "thread_id": None, "force_reply": False,
        })

    @staticmethod
    def confirm_private_delivery(message: Message, result: Any) -> Any:
        """
        私聊路径只接受平台明确确认，不将失败或未知结果解释成成功。

        :param message: 待确认的通知；普通消息沿用原发送结果
        :param result: 平台布尔、元组或带 success 字段的结果
        :return: 原平台结果
        :raises RuntimeError: 强制私聊没有明确成功确认
        """
        if message.private_delivery:
            if isinstance(result, dict):
                confirmed = result.get("success")
            elif isinstance(result, tuple) and result:
                confirmed = result[0]
            else:
                confirmed = result
            if confirmed is not True:
                raise RuntimeError("私聊投递未确认，未回退到群聊或默认目标")
        return result

    def record_delivery(
        self, message: Message, config: NotificationConf, result: Any,
        *, target: Optional[str] = None, part: int = 0, status: DeliveryStatus | None = None,
        code: Optional[str] = None, retry_after: int | None = None,
    ) -> Any:
        """
        收集平台回执并维持旧发送入口的返回值和私聊约束。

        :param message: 当前派发，持有此次调用的回执记录
        :param config: 当前命中的渠道连接配置
        :param result: 平台直接返回的结果
        :param target: 已经明确选定的目标，缺失时记录连接级回执
        :param part: 该次传输在分段中的序号
        :param status: 传输层能明确识别时提供更具体的失败类型
        :param code: 脱敏的固定分类
        :param retry_after: 平台明确建议的等待秒数
        :return: 平台原始结果
        """
        connection_id = getattr(config, "id", None)
        if notification_preflight_active() and code == "client_unavailable":
            raise RuntimeError("通知渠道尚未就绪，尚未提交投递清单")
        channel = self._channel or message.channel
        if channel and connection_id:
            outcome = status or delivery_status(result)
            message.record_delivery_result(DeliveryResult(
                channel=channel, connection_id=connection_id, target_id=target,
                identity_namespace=getattr(config, "identity_namespace", None),
                part=message.attachment_part_offset + part, status=outcome, code=code,
                message_id=platform_message_id(result) if outcome == "confirmed" else None,
                retry_after=retry_after,
            ))
        return self.confirm_private_delivery(message, result)

    def get_message_client(self, source: Optional[str], message: Message) -> Optional[TService]:
        """
        读取指定实例的发送客户端，私聊实例未就绪时显式失败。

        :param source: 已由消息范围选中的实例名称
        :param message: 当前消息，决定是否需要私聊的严格失败语义
        :return: 已存在的客户端；普通消息未就绪时为空
        :raises RuntimeError: 私聊实例没有可用客户端
        """
        client = self.get_instance(source)
        if client is None and message.private_delivery:
            raise RuntimeError("私聊渠道客户端尚未就绪，未使用其他实例或默认目标")
        return client

    def iter_message_configs(self, message: Message) -> Iterator[tuple[NotificationConf, Message]]:
        """
        先按原通知过滤实例，再为每个实例独立适配图片与附件。

        :param message: 保留原始接收人、通知范围和内容的统一消息
        :yields: 通过原通知规则的配置与渠道消息；附件拆分不会改写其他实例的输入
        """
        for conf in self.get_configs().values():
            if not self.check_message(message, conf.name):
                continue
            channel = getattr(self, "_channel", None)
            if channel is None:
                subtype = getattr(self, "get_subtype", lambda: None)()
                channel = subtype if isinstance(subtype, NotificationChannel) else None
            messages = self._rule_messages(message, conf)
            for routed in messages:
                if legacy_attachment_layout_active():
                    if channel != NotificationChannel.Wechat and routed.native_image:
                        routed = routed.model_copy(update={"native_image": False})
                    yield conf, routed
                    continue
                for part in notification_parts(routed, channel) if channel is not None else (routed,):
                    yield conf, part

    def _rule_messages(self, message: Message, config: NotificationConf) -> Iterator[Message]:
        """
        应用主动通知规则，保留兼容渠道及明确收件地址。
        """
        rules = notification_rules_for_dispatch(config) if notification_rule_applies(message) else None
        if rules is None:
            yield message
            return
        yield from routed_notification_messages(
            message, config, rules, has_bound_target=self._has_notification_target(message),
        )

    def _send_direct_parts(
        self, message: Message, sender: Callable[[Message], Optional[MessageResponse]],
    ) -> Optional[MessageResponse]:
        """
        直接发送接口也保留组合附件；后续部分绑定首条成功实例，任一失败不得当作完整成功。
        """
        message = self.prepare_message(message)
        channel = getattr(self, "_channel", None) or getattr(self, "get_subtype", lambda: None)()
        parts = notification_parts(message, channel) if isinstance(channel, NotificationChannel) else [message]
        first = None
        for part in parts:
            if first is not None and first.source:
                part = part.model_copy(update={"source": first.source})
            response = sender(part)
            if response is None or not response.success:
                return None
            if first is None:
                first = response
        return first

    def check_message(self, message: Message, source: Optional[str] = None) -> bool:
        """
        检查消息渠道及消息类型，判断是否处理消息。

        :param message: 要检查的通知消息
        :param source: 消息来源，可选
        :return: 返回布尔值，表示是否处理该消息
        """
        # 检查消息渠道
        if message.channel and message.channel != self._channel:
            return False
        # 检查消息来源
        if message.source and message.source != source:
            return False
        if not self._feature_allows_delivery(message, source):
            return False
        conf = self.get_config(source)
        if conf is not None and notification_rule_applies(message) and notification_rules_for_dispatch(conf) is not None:
            # 规则负责事件和受众匹配，避免先按已经移走的适配器默认目标过滤。
            return True
        route_scope = message.notification_route_scope
        if route_scope == "user":
            if not self._has_notification_target(message):
                return False
        elif route_scope == "admin":
            if not self._has_notification_target(message):
                config = self.get_config(source)
                if not self._has_admin_default(config):
                    return False
        # 没有类型且没有显式目标时不得进入渠道的默认广播路径。
        if route_scope is None and not message.userid and not message.mtype:
            return bool(message.targets)
        # 不是定向发送时，检查消息类型开关
        if not message.userid and message.mtype and message.delivery_kind not in {"reply", "task_result", "authorization_receipt"}:
            conf = self.get_config(source)
            if conf:
                switchs = conf.switchs or []
                if message.mtype.value not in switchs:
                    return False
        return True

    def _feature_allows_delivery(self, message: Message, source: Optional[str]) -> bool:
        """
        按通知语义检查共享连接所启用的功能。

        :param message: 已完成回复上下文归一化的发送请求
        :param source: 待检查的连接名称
        :return: 旧连接或该功能可用时为 True
        """
        config = self.get_config(source)
        binding = message.delivery_connection
        if binding is not None and (
            config is None or (config.id, config.identity_namespace or config.id) != binding
        ):
            return False
        connection_id = getattr(config, "id", None)
        if not message.accepts_delivery_connection(
            connection_id, getattr(config, "identity_namespace", None) or connection_id,
        ):
            return False
        if config is None:
            return True
        if message.delivery_kind == "authorization_receipt":
            return getattr(config, "enabled", True) is not False
        if message.delivery_kind in {"reply", "task_result"}:
            return getattr(config, "interaction_enabled", None) is not False
        return getattr(config, "notification_enabled", None) is not False

    def _has_notification_target(self, message: Message) -> bool:
        """
        判断消息是否包含当前渠道允许使用的明确用户目标。
        """
        if message.userid:
            return True
        targets = message.targets or {}
        return any(
            str(targets.get(key) or "").strip()
            for key in self._notification_user_target_keys
        )

    def get_notification_targets(self, message: Message) -> Optional[dict[str, Any]]:
        """
        返回渠道可用的通知目标，管理员和所有用户范围缺少本渠道目标时使用默认目标。

        :param message: 已确定路由范围和用户绑定目标的通知
        :return: 渠道目标映射；允许默认投递的范围没有本渠道目标时返回 None
        """
        if (
            message.notification_route_scope in {"admin", "all"}
            and not self._has_notification_target(message)
        ):
            return None
        return message.targets

    def get_notification_userid(
        self,
        message: Message,
        *target_keys: str,
    ) -> Optional[str]:
        """
        按消息路由范围解析当前渠道的用户 ID，缺少时返回 None 以使用渠道默认目标。

        :param message: 带显式用户标识或渠道目标映射的通知
        :param target_keys: 按优先级查找的渠道绑定键
        :return: 首个有效用户标识；未找到时返回 None
        """
        if message.userid:
            return str(message.userid)
        targets = self.get_notification_targets(message) or {}
        for key in target_keys:
            value = str(targets.get(key) or "").strip()
            if value:
                return value
        return None

    def _has_admin_default(self, config: Optional[NotificationConf]) -> bool:
        """
        判断渠道配置是否维护了可供管理员范围使用的默认目标。
        """
        if not config:
            return False
        values = config.config or {}
        return any(
            str(values.get(key) or "").strip()
            for key in self._notification_admin_default_config_keys
        )


class _DownloaderBase(ServiceBase[TService, DownloaderConf]):
    """
    下载器基类。
    """
    CONFIG_WATCH = {SystemConfigKey.Downloaders.value}

    def __init__(self) -> None:
        """
        初始化下载器基类。
        """
        super().__init__()
        self._default_config_name: Optional[str] = None

    def init_service(self, service_name: str,
                     service_type: Optional[Union[Type[TService], Callable[..., TService]]] = None) -> None:
        """
        初始化服务，获取配置并实例化对应服务。

        :param service_name: 服务名称，作为配置匹配的依据
        :param service_type: 服务的类型，可以是类类型（Type[TService]）、工厂函数（Callable）或 None 来跳过实例化
        """
        # 重置默认配置名称
        self.reset_default_config_name()
        # 初始化服务
        super().init_service(service_name, service_type)

    def get_default_config_name(self) -> Optional[str]:
        """
        获取默认服务配置的名称。

        只从已启用的下载器配置中选择：已禁用配置不会被实例化，若命中已禁用项会导致所有走默认
        下载器的请求找不到实例。每次按最新内存配置快照实时解析，避免配置变更后沿用过期的默认名称缓存。

        :return: 优先从已启用的下载器中查找配置了默认的下载器，如果没有配置，则获取第一个已启用的下载器名称
        """
        configs = [
            conf for conf in ServiceConfigHelper.get_downloader_configs() if conf.enabled
        ]
        for conf in configs:
            if conf.default:
                self._default_config_name = conf.name
                return self._default_config_name
        # 如果没有默认配置，返回第一个已启用配置的名称
        first_conf = next(iter(configs), None)
        self._default_config_name = first_conf.name if first_conf else None
        return self._default_config_name

    def get_configs(self) -> Dict[Optional[str], DownloaderConf]:
        """
        获取已启用的下载器的配置字典。

        :return: 返回下载器配置字典
        """
        configs = ServiceConfigHelper.get_downloader_configs()
        if not self._service_name:
            return {}
        return {conf.name: conf for conf in configs if conf.type == self._service_name and conf.enabled}

    def reset_default_config_name(self) -> None:
        """
        重置默认配置名称。
        """
        self._default_config_name = None

    @staticmethod
    def __replace_path_prefix(path: Union[Path, str], source: str, target: str) -> Optional[str]:
        """
        按完整路径段替换路径前缀，避免 /media 误匹配 /media2 这类相邻目录。
        """
        if not source or not source.strip() or not target or not target.strip():
            return None

        path_text = Path(path).as_posix()
        source_path = Path(source.strip()).as_posix()
        target_path = Path(target.strip()).as_posix()
        if path_text == source_path:
            return target_path

        source_prefix = f"{source_path.rstrip('/')}/"
        if path_text.startswith(source_prefix):
            suffix = path_text[len(source_prefix):]
            return (Path(target_path) / suffix).as_posix()
        return None

    @staticmethod
    def __strip_storage_prefix(path: str) -> str:
        """
        去掉存储协议前缀，下载器无法识别本地存储协议。
        """
        for s in StorageSchema:
            prefix = f"{s.value}:"
            if path.startswith(prefix):
                return path[len(prefix):]
        return path

    def normalize_path(self, path: Path, downloader: Optional[str]) -> str:
        """
        根据下载器配置和路径映射，规范化下载路径。

        :param path: 存储路径
        :param downloader: 下载器名称
        :return: 规范化后发送给下载器的路径
        """
        normalized_path = path.as_posix()
        conf = self.get_config(downloader)
        if conf and conf.path_mapping:
            for (storage_path, download_path) in conf.path_mapping:
                mapped_path = self.__replace_path_prefix(normalized_path, storage_path, download_path)
                if mapped_path:
                    normalized_path = mapped_path
                    break
        return self.__strip_storage_prefix(normalized_path)

    def normalize_return_path(self, path: Path, downloader: Optional[str]) -> str:
        """
        将下载器路径反向映射为存储 URI，远程前缀用于选择实际文件访问接口。

        :param path: 下载器返回的路径
        :param downloader: 下载器名称
        :return: 本地路径或带存储前缀的远程 URI；不能对远程 URI 调用本地文件接口
        """
        normalized_path = path.as_posix()
        conf = self.get_config(downloader)
        if conf and conf.path_mapping:
            for (storage_path, download_path) in conf.path_mapping:
                mapped_path = self.__replace_path_prefix(normalized_path, download_path, storage_path)
                if mapped_path:
                    normalized_path = mapped_path
                    break
        # 下发下载器时才移除远程前缀；返回时必须保留，否则整理会误查本地同名路径。
        return normalized_path.removeprefix("local:")


class _MediaServerBase(ServiceBase[TService, MediaServerConf]):
    """
    媒体服务器基类。
    """
    CONFIG_WATCH = {SystemConfigKey.MediaServers.value}

    def get_configs(self) -> Dict[Optional[str], MediaServerConf]:
        """
        获取已启用的媒体服务器的配置字典。

        :return: 返回媒体服务器配置字典
        """
        configs = ServiceConfigHelper.get_mediaserver_configs()
        if not self._service_name:
            return {}
        return {conf.name: conf for conf in configs if conf.type == self._service_name and conf.enabled}
