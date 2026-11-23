"""
渠道设置的条件写入、旧数据读取和配置失效通知。
"""

import re
from collections import Counter
from collections.abc import Awaitable
from typing import Any, Callable, TypeVar
from uuid import uuid4

from app.application.configuration import SystemConfigService
from app.application.messaging.channel.catalog import (
    import_legacy_channels,
    patch_provider_config,
    project_legacy_channels,
    provider_editable_fields,
    provider_form_values,
    supported_admission_modes,
    supported_rule_kinds,
)
from app.application.messaging.channel.rules import (
    migrate_notification_rules,
    notification_rule_types,
    notification_target_kinds,
    validate_notification_rule,
)
from app.runtime.execution import run_in_threadpool_to_completion
from app.schemas.channel import (
    ChannelCardOrder,
    ChannelConnection,
    ChannelConnectionCreate,
    ChannelConnectionDelete,
    ChannelConnectionEdit,
    ChannelConnectionForm,
    ChannelConnectionInfo,
    ChannelCredentialRotation,
    ChannelFeature,
    ChannelSettings,
    ChannelSettingsView,
    InteractionPolicy,
    InteractionPolicyEdit,
    NotificationRoute,
    NotificationRouteEdit,
)
from app.schemas.common import JsonData
from app.schemas.system import NotificationConf
from app.schemas.types import SystemConfigKey

_Resource = TypeVar("_Resource")
type _ConnectionView = tuple[ChannelConnectionInfo, ...]
type ConnectionReconciler = Callable[[_ConnectionView, _ConnectionView], Awaitable[None]]
type ConnectionIdentityProbe = Callable[[ChannelConnection], Awaitable[str | None]]
_connection_reconciler: ConnectionReconciler | None = None
_connection_identity_probe: ConnectionIdentityProbe | None = None


def configure_connection_identity_probe(probe: ConnectionIdentityProbe | None) -> None:
    """
    发布或撤销由组合根装配的平台账号核验入口。

    :param probe: 不启动平台运行客户端的账号查询端口
    """
    global _connection_identity_probe
    _connection_identity_probe = probe


def configure_connection_reconciler(reconciler: ConnectionReconciler | None) -> None:
    """
    由启动组合根发布连接保存后的生命周期执行入口。
    """
    global _connection_reconciler
    _connection_reconciler = reconciler


class ChannelSettingsConflict(ValueError):
    """
    资源已被另一项写入修改，客户端需刷新版本。
    """


class CommittedChannelSettingsError(RuntimeError):
    """
    配置已提交，但平台运行态尚未完成更新。
    """

    def __init__(self, data: Any) -> None:
        """
        :param data: 可返回给管理员的已保存配置摘要
        """
        super().__init__("渠道设置已保存，连接状态同步失败。请刷新设置后检查连接状态。")
        self.data = data


def read_channel_resources(raw: Any, legacy_rows: Any) -> ChannelSettings:
    """
    在第一次专用保存前从旧数据生成只读视图，已有新事实源时使用该快照。

    :param raw: 已持久化的渠道资源，尚未初始化时为 None
    :param legacy_rows: 旧通知配置
    :return: 供查询或事务起点使用的资源快照
    """
    if raw is not None:
        return migrate_notification_rules(ChannelSettings.model_validate(raw))
    return migrate_notification_rules(import_legacy_channels(_parse_legacy_rows(legacy_rows)))


def _parse_legacy_rows(legacy_rows: Any) -> list[NotificationConf]:
    """
    将旧配置规范化为稳定 ID，保留重复 ID 供修复入口核对。
    """
    records = []
    for row in legacy_rows or []:
        if not isinstance(row, dict):
            raise ValueError("旧通知配置必须是对象列表")
        normalized = dict(row)
        if not normalized.get("id"):
            normalized["id"] = f"legacy-{row.get('type') or 'notification'}-{row.get('name') or 'unnamed'}"
        records.append(NotificationConf.model_validate(normalized))
    return records


class ChannelSettingsService:
    """
    通过现有配置事务对三种渠道资源执行彼此独立的条件写入。
    """

    def __init__(
        self, config: SystemConfigService, *, reconcile: ConnectionReconciler | None = None,
        probe: ConnectionIdentityProbe | None = None,
    ) -> None:
        """
        :param config: 宿主配置事务与变更事件入口
        :param reconcile: 连接身份或名称变化后的运行态同步
        :param probe: 平台账号核验端口
        """
        self._config = config
        self._reconcile = reconcile if reconcile is not None else _connection_reconciler
        self._probe = probe if probe is not None else _connection_identity_probe

    def read(self) -> ChannelSettingsView:
        """
        返回可公开给管理员的版本化连接和功能配置。
        """
        snapshot = self._read_snapshot()
        live = {key: record for key, record in snapshot.connections.items()}
        return ChannelSettingsView(
            connections=tuple(self._summary(record) for record in live.values()),
            notifications=tuple(route for key, route in snapshot.notifications.items() if key in live),
            interactions=tuple(policy for key, policy in snapshot.interactions.items() if key in live),
            orders={
                "notification": self._card_order(snapshot, "notification"),
                "interaction": self._card_order(snapshot, "interaction"),
            },
            notification_target_kinds={key: kinds for key, record in live.items() if (kinds := notification_target_kinds(record))},
            admission_modes={
                connection_id: supported_admission_modes(live[connection_id])
                for connection_id in snapshot.interactions if connection_id in live
            },
            rule_kinds={
                connection_id: supported_rule_kinds(live[connection_id])
                for connection_id in snapshot.interactions if connection_id in live
            },
        )

    @staticmethod
    def _card_order(snapshot: ChannelSettings, feature: ChannelFeature) -> tuple[str, ...]:
        """
        保留已保存顺序，移除已删除项并将新卡片附在末尾。
        """
        records = snapshot.notifications if feature == "notification" else snapshot.interactions
        live = [key for key in snapshot.connections if key in records]
        saved = tuple(dict.fromkeys(key for key in snapshot.orders.get(feature, ()) if key in live))
        return saved + tuple(key for key in live if key not in saved)

    async def reorder(self, feature: ChannelFeature, edit: ChannelCardOrder) -> tuple[str, ...]:
        """
        只写当前页面的排位，冲突时要求重新读取。
        """
        def apply(snapshot: ChannelSettings) -> tuple[str, ...]:
            current = self._card_order(snapshot, feature)
            if edit.expected_order != current:
                raise ChannelSettingsConflict("渠道顺序已更改，请刷新后重试")
            if len(edit.order) != len(current) or set(edit.order) != set(current):
                raise ValueError("排序须包含此页面上的每张渠道卡片")
            snapshot.orders[feature] = edit.order
            return edit.order

        return await self._edit(apply, publish=False)

    def read_connection_form(self, connection_id: str) -> ChannelConnectionForm:
        """
        为平台表单返回经过允许列表筛选的值。
        """
        record = self._editable_connection(self._read_snapshot(), connection_id)
        return ChannelConnectionForm(
            revision=record.revision, values=provider_form_values(record), editable_fields=provider_editable_fields(record),
        )

    def _read_snapshot(self) -> ChannelSettings:
        """
        读取当前连接、通知路由和交互策略。
        """
        raw = self._config.get(SystemConfigKey.ChannelSettings)
        return read_channel_resources(raw, self._config.get(SystemConfigKey.Notifications) if raw is None else None)

    def read_legacy(self) -> Any:
        """
        旧客户端读取现有可见配置，切换后使用兼容投影。
        """
        raw = self._config.get(SystemConfigKey.ChannelSettings)
        if raw is None:
            return self._config.get(SystemConfigKey.Notifications)
        return project_legacy_channels(ChannelSettings.model_validate(raw))

    async def create_connection(self, request: ChannelConnectionCreate) -> ChannelConnectionInfo:
        """
        创建稳定连接及其功能记录，等待管理员显式开启通知或交互功能。
        """
        prepared = migrate_notification_rules(import_legacy_channels([NotificationConf(
            id=uuid4().hex, name=request.name, type=request.provider,
            config=request.provider_config, enabled=False,
        )]))
        record = next(iter(prepared.connections.values()))
        if self._probe is not None and self._credential_key(record) is not None:
            account = await self._probe(record)
            if account is not None:
                record = record.model_copy(update={"verified_platform_account": account})

        def apply(snapshot: ChannelSettings) -> ChannelConnectionInfo:
            """
            在一个事务中发布连接和独立配置。
            """
            snapshot.connections[record.id] = record
            snapshot.notifications.update(prepared.notifications)
            snapshot.interactions.update(prepared.interactions)
            return self._summary(record)

        return await self._edit(apply)

    def _summary(self, connection: ChannelConnection) -> ChannelConnectionInfo:
        """
        仅拷贝公共连接字段，即使输入是含私有配置的持久化连接。
        """
        fields = connection.model_dump(include=set(ChannelConnectionInfo.model_fields))
        fields["inbound_token_configured"] = bool(connection.provider_config.get("VOCECHAT_INBOUND_TOKEN"))
        fields["can_rotate_credential"] = self._credential_key(connection) is not None
        return ChannelConnectionInfo.model_validate(fields)

    def _credential_key(self, connection: ChannelConnection) -> str | None:
        """
        只有验证本身依赖可信平台接口的连接才可原地轮换。

        :param connection: 当前共享连接
        :return: 允许单独轮换的凭据字段
        """
        if self._probe is None:
            return None
        config = connection.provider_config
        if connection.provider == "telegram" and config.get("API_URL") in (
            None, "", "https://api.telegram.org", "https://api.telegram.org/",
        ):
            return "TELEGRAM_TOKEN"
        if connection.provider == "wechat" and connection.mode == "app" and config.get("WECHAT_PROXY") in (
            None, "", "https://qyapi.weixin.qq.com", "https://qyapi.weixin.qq.com/",
        ):
            return "WECHAT_APP_SECRET"
        if connection.provider == "slack":
            return "SLACK_OAUTH_TOKEN"
        return None

    async def edit_connection(self, connection_id: str, edit: ChannelConnectionEdit) -> ChannelConnectionInfo:
        """
        条件更新显示名称和总开关，保留身份空间及私有配置。
        """
        def apply(snapshot: ChannelSettings) -> ChannelConnectionInfo:
            """
            在快照事务内核对版本并更新指定连接。
            """
            record = self._editable_connection(snapshot, connection_id)
            self._expect_revision(record.revision, edit.expected_revision)
            changes: dict[str, Any] = {
                "name": edit.name, "enabled": edit.enabled, "revision": record.revision + 1,
            }
            if edit.provider_config is not None:
                changes["provider_config"] = patch_provider_config(record, edit.provider_config)
            if edit.inbound_token is not None:
                if record.provider != "vocechat":
                    raise ChannelSettingsConflict("该连接无需设置回调验证令牌")
                changes["provider_config"] = changes.get("provider_config", record.provider_config) | {
                    "VOCECHAT_INBOUND_TOKEN": edit.inbound_token.get_secret_value(),
                }
            updated = record.model_copy(update=changes)
            snapshot.connections[connection_id] = updated
            return self._summary(updated)

        return await self._edit(apply)

    async def delete_connection(self, connection_id: str, request: ChannelConnectionDelete) -> ChannelConnectionInfo:
        """
        完整移除连接、用途配置和排序引用，旧授权和任务不能绑定新连接。

        :param connection_id: 需要移除的连接 ID
        :param request: 确认时三项资源的版本
        :return: 记录此次移除的连接摘要
        """
        def apply(snapshot: ChannelSettings) -> ChannelConnectionInfo:
            """
            在同一次配置事务中确认全部资源版本并提交移除。
            """
            record = self._editable_connection(snapshot, connection_id)
            route = snapshot.notifications.get(connection_id)
            policy = snapshot.interactions.get(connection_id)
            self._expect_revision(record.revision, request.expected_revision)
            self._expect_revision(route.revision if route else None, request.notification_revision)
            self._expect_revision(policy.revision if policy else None, request.interaction_revision)
            removed = record.model_copy(update={"enabled": False, "revision": record.revision + 1})
            del snapshot.connections[connection_id]
            snapshot.notifications.pop(connection_id, None)
            snapshot.interactions.pop(connection_id, None)
            for feature, order in snapshot.orders.items():
                snapshot.orders[feature] = tuple(key for key in order if key != connection_id)
            return self._summary(removed)

        return await self._edit(apply)

    async def rotate_credential(self, connection_id: str, request: ChannelCredentialRotation) -> ChannelConnectionInfo:
        """
        查询平台后使用条件写入，只有明确同一账号才安装新凭据。

        :param connection_id: 需保留现有绑定的稳定连接 ID
        :param request: 候选凭据和发起操作时的版本
        :return: 已保存的连接摘要，含身份核验状态
        """
        snapshot = await run_in_threadpool_to_completion(self._read_snapshot)
        record = self._editable_connection(snapshot, connection_id)
        self._expect_revision(record.revision, request.expected_revision)
        probe = self._probe
        credential_key = self._credential_key(record)
        if probe is None or credential_key is None:
            raise ChannelSettingsConflict("该连接暂不支持核验账号身份，请新建连接以切换")
        secret = request.secret.get_secret_value().strip()
        if not secret:
            raise ValueError("缺少新凭据")
        candidate_config = {**record.provider_config, credential_key: secret}
        candidate = record.model_copy(update={"provider_config": candidate_config})
        existing_account = record.verified_platform_account or await probe(record)
        candidate_account = await probe(candidate)

        def apply(current: ChannelSettings) -> ChannelConnectionInfo:
            """
            比对平台账号与当前连接版本，再更新凭据或保存待核验状态。
            """
            latest = self._editable_connection(current, connection_id)
            self._expect_revision(latest.revision, request.expected_revision)
            if existing_account and candidate_account and existing_account != candidate_account:
                raise ChannelSettingsConflict("新凭据属于另一个平台账号，请新建连接")
            verified = bool(existing_account and candidate_account)
            updated = latest.model_copy(update={
                "credential_status": "ready" if verified else "verification_required",
                "provider_config": candidate_config if verified else latest.provider_config,
                "verified_platform_account": existing_account,
                "revision": latest.revision + 1,
            })
            current.connections[connection_id] = updated
            return self._summary(updated)

        return await self._edit(apply)

    async def edit_notification(self, connection_id: str, edit: NotificationRouteEdit) -> NotificationRoute:
        """
        条件更新指定连接的通知路由。
        """
        def apply(snapshot: ChannelSettings) -> NotificationRoute:
            """
            只更新路由和该资源的 revision。
            """
            connection = self._editable_connection(snapshot, connection_id)
            record = snapshot.notifications[connection_id]
            self._expect_revision(record.revision, edit.expected_revision)
            rules = edit.rules
            if rules is not None:
                if not notification_target_kinds(connection):
                    raise ValueError("该渠道使用消息类型配置")
                for rule in rules:
                    validate_notification_rule(connection, rule)
                types = notification_rule_types(rules)
            else:
                types = record.message_types if edit.message_types is None else tuple(dict.fromkeys(edit.message_types))
                record = self._update_compat_notification_types(record, types)
                rules = record.rules
            updated = record.model_copy(update={
                "enabled": edit.enabled, "message_types": types, "rules": rules,
                "revision": record.revision + 1,
            })
            snapshot.notifications[connection_id] = updated
            return updated

        return await self._edit(apply)

    @staticmethod
    def _update_compat_notification_types(
        route: NotificationRoute, types: tuple[str, ...],
    ) -> NotificationRoute:
        """
        旧入口只可调整未定制的迁移规则，不得覆盖后来配置的分流。
        """
        if types == route.message_types or route.rules is None:
            return route.model_copy(update={"message_types": types})
        if route.revision > 1:
            raise ChannelSettingsConflict("通知投递规则已更新，请在新版页面中读取并保存")
        rules = tuple(rule.model_copy(update={"message_types": types}) for rule in route.rules) if types else ()
        if types and not rules:
            raise ChannelSettingsConflict("请在通知投递中选择接收目标并保存规则")
        return route.model_copy(update={"message_types": types, "rules": rules})

    async def edit_interaction(self, connection_id: str, edit: InteractionPolicyEdit) -> InteractionPolicy:
        """
        条件更新指定连接的交互开关、接入策略和下载偏好。
        """
        def apply(snapshot: ChannelSettings) -> InteractionPolicy:
            """
            只更新交互配置和该资源的版本。
            """
            connection = self._editable_connection(snapshot, connection_id)
            record = snapshot.interactions[connection_id]
            self._expect_revision(record.revision, edit.expected_revision)
            updates = edit.model_dump(exclude={"expected_revision"}, exclude_none=True)
            data = record.model_dump() | updates | {"revision": record.revision + 1}
            if data["admission"] in {"manual", "open"}:
                data["admission_rules"] = ()
            if data["auto_download"] != "specified":
                data["auto_download_rules"] = ()
            updated = InteractionPolicy.model_validate(data)
            self._validate_interaction(connection, updated)
            if edit.command_guild_id is not None:
                guild = edit.command_guild_id.strip()
                if connection.provider != "discord" or (guild and not re.fullmatch(r"[1-9][0-9]*", guild)):
                    raise ValueError("请输入有效的 Discord 服务器 ID")
                updated = updated.model_copy(update={"command_guild_id": guild or None})
            snapshot.interactions[connection_id] = updated
            return updated

        return await self._edit(apply)

    @staticmethod
    def _validate_interaction(connection: ChannelConnection, policy: InteractionPolicy) -> None:
        """
        用与界面相同的能力约束审批和下载条件。
        """
        if policy.admission not in supported_admission_modes(connection):
            raise ChannelSettingsConflict("该连接无法验证所选接入方式")
        kinds = supported_rule_kinds(connection)
        if any(rule.enabled and rule.kind not in kinds for rule in (*policy.admission_rules, *policy.auto_download_rules)):
            raise ChannelSettingsConflict("该连接无法验证所选规则条件")

    async def replace_legacy(self, configs: list[NotificationConf]) -> list[dict[str, JsonData]]:
        """
        接受旧客户端能够表达的更新，保护拆分后的配置和原连接身份。
        """
        incoming = import_legacy_channels(configs)

        def apply(snapshot: ChannelSettings) -> list[dict[str, JsonData]]:
            """
            拒绝隐式删连接，按稳定 ID 合并可兼容的修改。
            """
            live = {key for key, record in snapshot.connections.items()}
            if live - incoming.connections.keys():
                raise ChannelSettingsConflict("共享连接仍有关联设置，请在新版连接设置中管理")
            for connection_id, replacement in incoming.connections.items():
                old = snapshot.connections.get(connection_id)
                if old is None:
                    self._add_legacy_record(snapshot, incoming, replacement)
                else:
                    self._editable_connection(snapshot, connection_id)
                    self._update_legacy_record(snapshot, incoming, old, replacement)
            return project_legacy_channels(snapshot)

        return await self._edit(apply, initial=lambda rows: self._legacy_repair_base(rows, incoming))

    @staticmethod
    def _legacy_repair_base(rows: Any, incoming: ChannelSettings) -> ChannelSettings:
        """
        修复历史重复 ID 时要求同一组全部分配新身份，不继承重复 ID 的成员授权。
        """
        records = _parse_legacy_rows(rows)
        counts = Counter(record.id for record in records)
        incoming_by_name = {record.name: record for record in incoming.connections.values()}
        retained = []
        for record in records:
            if counts[record.id] == 1:
                retained.append(record)
                continue
            replacement = incoming_by_name.get(record.name or "")
            if not replacement or replacement.id in counts or replacement.provider != record.type:
                raise ChannelSettingsConflict("重复渠道 ID 的每项配置都需要使用新的唯一 ID")
            if replacement.provider_config != (record.config or {}):
                raise ChannelSettingsConflict("修复重复 ID 时请保留原连接参数")
        return import_legacy_channels(retained)

    @staticmethod
    def _add_legacy_record(
        snapshot: ChannelSettings, incoming: ChannelSettings, record: ChannelConnection,
    ) -> None:
        """
        保存旧页面创建的整套连接和功能配置。
        """
        identity = uuid4().hex
        created = record.model_copy(update={"id": identity, "identity_namespace": identity})
        snapshot.connections[identity] = created
        snapshot.notifications[identity] = incoming.notifications[record.id].model_copy(update={
            "connection_id": identity, "identity_namespace": identity,
        })
        if record.id in incoming.interactions:
            snapshot.interactions[identity] = incoming.interactions[record.id].model_copy(update={
                "connection_id": identity, "identity_namespace": identity,
            })

    @staticmethod
    def _update_legacy_record(
        snapshot: ChannelSettings, incoming: ChannelSettings, old: ChannelConnection, record: ChannelConnection,
    ) -> None:
        """
        应用可以无歧义表达的旧编辑，保留发给旧前端时隐去的凭据。
        """
        connection_id = record.id
        if record.provider != old.provider or (record.provider_config and (
            record.mode != old.mode or record.provider_config != old.provider_config
        )):
            raise ChannelSettingsConflict("更换渠道账号或连接凭据需要使用新版连接设置")
        route = snapshot.notifications[connection_id]
        policy = snapshot.interactions.get(connection_id)
        runtime_enabled = old.enabled and old.credential_status == "ready" and (
            route.enabled or bool(policy and policy.enabled)
        )
        if old.revision > 1 and (record.name != old.name or record.enabled != runtime_enabled):
            raise ChannelSettingsConflict("连接配置已更新，请在新版页面中读取并保存")
        if record.enabled != runtime_enabled:
            ChannelSettingsService._apply_legacy_switch(snapshot, old, record.enabled)
            old = snapshot.connections[connection_id]
            route = snapshot.notifications[connection_id]
        wanted = incoming.notifications[connection_id].message_types
        if wanted != route.message_types and route.revision > 1:
            raise ChannelSettingsConflict("通知投递设置已更新，请在新版页面中读取并保存")
        if wanted != route.message_types:
            snapshot.notifications[connection_id] = ChannelSettingsService._update_compat_notification_types(
                route, wanted,
            ).model_copy(update={"revision": route.revision + 1})
        if record.name != old.name:
            snapshot.connections[connection_id] = old.model_copy(update={
                "name": record.name, "revision": old.revision + 1,
            })

    @staticmethod
    def _apply_legacy_switch(snapshot: ChannelSettings, connection: ChannelConnection, enabled: bool) -> None:
        """
        兼容尚未拆分保存过的旧开关，保留已独立设置的功能状态。
        """
        route = snapshot.notifications[connection.id]
        policy = snapshot.interactions.get(connection.id)
        if route.revision > 1 or (policy and policy.revision > 1):
            raise ChannelSettingsConflict("请在新版设置中分别切换主动通知、机器人交互或连接")
        snapshot.connections[connection.id] = connection.model_copy(update={
            "enabled": enabled, "revision": connection.revision + 1,
        })
        if enabled and not (route.enabled or (policy and policy.enabled)):
            snapshot.notifications[connection.id] = route.model_copy(update={"enabled": True})
            if policy:
                snapshot.interactions[connection.id] = policy.model_copy(update={"enabled": True})

    async def _edit(
        self, apply: Callable[[ChannelSettings], _Resource],
        *, initial: Callable[[Any], ChannelSettings] | None = None, publish: bool = True,
    ) -> _Resource:
        """
        在配置行锁内执行变更，然后通知运行时重读已提交配置。
        """
        legacy = self._config.get(SystemConfigKey.Notifications)

        def mutate(raw: JsonData) -> tuple[tuple[_Resource, _ConnectionView, _ConnectionView], Any]:
            """
            读取事务中的最新快照并验证提交数据。
            """
            snapshot = initial(legacy) if raw is None and initial else read_channel_resources(raw, legacy)
            previous = tuple(self._summary(item) for item in snapshot.connections.values())
            result = apply(snapshot)
            migrate_notification_rules(snapshot)
            serialized = snapshot.model_dump(mode="json")
            ChannelSettings.model_validate(serialized)
            current = tuple(self._summary(item) for item in snapshot.connections.values())
            return (result, previous, current), serialized

        result, previous, current = await self._config.async_update_atomically(SystemConfigKey.ChannelSettings, mutate)
        if not publish:
            return result
        previous_names = self._connection_names(previous)
        current_names = self._connection_names(current)
        removed = bool(previous_names.keys() - current_names.keys())
        try:
            if removed:
                await self._config.async_publish_committed_change(SystemConfigKey.ChannelSettings)
            if self._reconcile is not None and previous_names != current_names:
                await self._reconcile(previous, current)
        except Exception as error:
            raise CommittedChannelSettingsError(result) from error
        finally:
            if not removed:
                await self._config.async_publish_committed_change(SystemConfigKey.ChannelSettings)
        return result

    @staticmethod
    def _connection_names(records: _ConnectionView) -> dict[str, tuple[str, str]]:
        """
        仅连接身份和名称的变化需要更新平台登录缓存。
        """
        return {record.id: (record.name, record.provider) for record in records}

    @staticmethod
    def _editable_connection(snapshot: ChannelSettings, connection_id: str) -> ChannelConnection:
        """
        按稳定 ID 查找现存连接，删除后的迟到请求不能再次写入。
        """
        record = snapshot.connections[connection_id]
        return record

    @staticmethod
    def _expect_revision(current: int | None, expected: int | None) -> None:
        """
        拒绝同一资源中的迟到写入，彼此独立的资源使用各自的版本。
        """
        if current != expected:
            raise ChannelSettingsConflict("配置已更新，请重新读取后再保存")
