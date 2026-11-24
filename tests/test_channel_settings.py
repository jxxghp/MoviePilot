"""
验证共享渠道配置的事务、兼容投影、隐私和两项开关。
"""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.endpoints import notification
from app.api.endpoints import settings as settings_endpoint
from app.application.messaging.channel.catalog import project_runtime_channels
from app.application.messaging.channel.settings import (
    ChannelSettingsConflict,
    ChannelSettingsService,
    CommittedChannelSettingsError,
)
from app.modules.telegram.module import TelegramModule
from app.schemas.channel import (
    ChannelCardOrder,
    ChannelConnectionCreate,
    ChannelConnectionDelete,
    ChannelConnectionEdit,
    ChannelCredentialRotation,
    ChannelSettings,
    InteractionPolicy,
    InteractionPolicyEdit,
    NotificationRouteEdit,
)
from app.schemas.message import Message
from app.schemas.system import NotificationConf
from app.schemas.types import MessageType, NotificationChannel, SystemConfigKey


class _Configuration:
    """
    用内存事务替身记录读写和无凭据的事件通知。
    """

    def __init__(self, rows):
        """
        用一份旧通知配置建立迁移起点。
        """
        self.events = []
        self._lock = asyncio.Lock()
        self.state = {SystemConfigKey.Notifications: rows}

    def get(self, key):
        """
        返回配置值的独立副本。
        """
        return deepcopy(self.state.get(key))

    async def async_update_atomically(self, key, change):
        """
        只在应用回调成功完成时发布新值。
        """
        async with self._lock:
            result, value = change(self.get(key))
            self.state[key] = value
            return result

    async def async_publish_committed_change(self, key):
        """
        记录事务完成后的配置失效键。
        """
        assert key in self.state
        self.events.append(key)


def test_channel_settings_http_contract(monkeypatch):
    """
    管理员接口从同一配置事务读取连接并在冲突时保留其他资源。
    """
    source = _Configuration([{
        "id": "fixture", "name": "机器人", "type": "telegram", "enabled": True,
        "switchs": ["下载"], "config": {},
    }])
    monkeypatch.setattr(notification, "get_configured_system_config", lambda: source)
    monkeypatch.setattr(settings_endpoint, "get_configured_system_config", lambda: source)
    monkeypatch.setattr(notification, "get_channel_authorization",
                        lambda: SimpleNamespace(connection_reference_counts=lambda _: (2, 3)))
    app = FastAPI()
    app.include_router(notification.router)
    app.include_router(settings_endpoint.router)
    app.dependency_overrides[notification.get_current_active_superuser] = lambda: None
    app.dependency_overrides[settings_endpoint.get_current_active_superuser_async] = lambda: None
    with TestClient(app) as client:
        before = client.get("/channels")
        assert before.status_code == 200
        data = before.json()["data"]
        route, = data["notifications"]
        reference = client.get("/channels/fixture/references")
        assert reference.status_code == 200
        assert reference.json()["data"] == {"member_count": 2, "task_count": 3}
        assert client.get("/channels/missing/references").status_code == 404
        assert client.get("/setting/ChannelSettings").status_code == 404
        legacy = client.get("/setting/Notifications")
        assert legacy.status_code == 200 and legacy.json()["data"]["value"][0]["id"] == "fixture"
        payload = {"enabled": False, "expected_revision": route["revision"], "message_types": route["message_types"]}
        assert client.put("/channels/missing/notification", json=payload).status_code == 404
        edited = client.put("/channels/fixture/notification", json=payload)
        assert edited.status_code == 200
        assert edited.json()["data"]["enabled"] is False
        assert client.put("/channels/fixture/notification", json={**payload, "enabled": True}).status_code == 409
        assert client.get("/channels").json()["data"]["interactions"] == data["interactions"]
        removed = client.request("DELETE", "/channels/fixture/connection", json={
            "expected_revision": 1, "notification_revision": 2, "interaction_revision": 1,
        })
        assert removed.status_code == 200
        snapshot = client.get("/channels").json()["data"]
        assert snapshot["connections"] == [] and "reserved_names" not in snapshot
        collision = client.post("/channels/connections", json={
            "name": "机器人", "provider": "synologychat", "provider_config": {},
        })
        assert collision.status_code == 200
        assert collision.json()["data"]["name"] == "机器人"
        assert collision.json()["data"]["id"] != "fixture"


@pytest.mark.asyncio
async def test_slack_rotation_uses_confirmed_workspace_and_bot_after_old_token_expires(monkeypatch):
    """
    新建时保存平台身份，轮换拒绝不同工作区或机器人并保留原有设置。
    """
    from app.startup.initializers.modules import _probe_channel_identity

    config = _Configuration([])
    service = ChannelSettingsService(config, probe=_probe_channel_identity)
    accounts = {
        "xoxb-original": ("T1", "B1"),
        "xoxb-replacement": ("T1", "B1"),
        "xoxb-other-team": ("T2", "B1"),
        "xoxb-other-bot": ("T1", "B2"),
    }
    responses = []

    def transport(*, verify, timeout, headers):
        """
        模拟固定官方接口及令牌过期，不启动 Socket Mode。
        """
        assert verify is True and timeout == 8
        token = headers["Authorization"].removeprefix("Bearer ")
        team, bot = accounts[token]
        response = Mock(status_code=200)
        response.json.return_value = {"ok": True, "team_id": team, "bot_id": bot}
        responses.append(response)
        client = Mock()
        client.post_res.return_value = response
        return client

    http = Mock(side_effect=transport)
    monkeypatch.setattr("app.modules.slack.slack.RequestUtils", http)
    created = await service.create_connection(ChannelConnectionCreate(
        name="工作区 Slack", provider="slack", provider_config={
            "SLACK_OAUTH_TOKEN": "xoxb-original", "SLACK_APP_TOKEN": "socket-fixture",
        },
    ))
    assert created.can_rotate_credential
    before = service.read()
    del accounts["xoxb-original"]

    for secret in ("xoxb-other-team", "xoxb-other-bot"):
        with pytest.raises(ChannelSettingsConflict, match="另一个平台账号"):
            await service.rotate_credential(created.id, ChannelCredentialRotation.model_validate({
                "secret": secret, "expected_revision": created.revision,
            }))
        assert service.read() == before
    rotated = await service.rotate_credential(created.id, ChannelCredentialRotation.model_validate({
        "secret": "xoxb-replacement", "expected_revision": created.revision,
    }))
    current = ChannelSettings.model_validate(config.get(SystemConfigKey.ChannelSettings))
    assert rotated.credential_status == "ready" and rotated.revision == created.revision + 1
    assert rotated.identity_namespace == created.identity_namespace
    assert service.read().notifications == before.notifications and service.read().interactions == before.interactions
    assert current.connections[created.id].provider_config == {
        "SLACK_OAUTH_TOKEN": "xoxb-replacement", "SLACK_APP_TOKEN": "socket-fixture",
    }
    assert "T1:B1" not in service.read().model_dump_json()
    assert "xoxb-" not in service.read().model_dump_json()
    assert http.call_count == 4
    assert all(response.close.call_count == 1 for response in responses)


@pytest.mark.parametrize(("status", "payload"), [
    (302, {"ok": True, "team_id": "T1", "bot_id": "B1"}),
    (200, {"ok": False, "team_id": "T1", "bot_id": "B1"}),
    (200, {"ok": True, "user_id": "U1", "team_id": "T1"}),
    (200, {"ok": True, "bot_id": "B1", "enterprise_id": "E1"}),
    (200, {"ok": True, "team_id": "T1", "bot_id": "B1", "is_enterprise_install": True}),
])
def test_slack_claims_require_explicit_workspace_bot_and_success(monkeypatch, status, payload):
    """
    跳转、业务错误、企业安装或身份字段缺失均不能确认工作区机器人。
    """
    from app.modules.slack.slack import Slack

    http = Mock()
    response = Mock(status_code=status)
    response.json.return_value = payload
    http.return_value.post_res.return_value = response
    monkeypatch.setattr("app.modules.slack.slack.RequestUtils", http)

    assert Slack.probe_workspace_identity("xoxb-fixture") is None
    http.return_value.post_res.assert_called_once_with("https://slack.com/api/auth.test", allow_redirects=False)
    response.close.assert_called_once()


@pytest.mark.asyncio
async def test_credential_rotation_keeps_identity_and_pauses_when_unverifiable(monkeypatch):
    """
    同账号轮换保留功能配置，账号不一致时拒绝，无法核验时暂停实际收发。
    """
    config = _Configuration([{
        "id": "tg", "type": "telegram", "name": "Telegram", "enabled": True,
        "config": {"TELEGRAM_TOKEN": "first-token", "TELEGRAM_CHAT_ID": "member"},
    }])
    from app.modules.telegram import module as telegram_module
    from app.runtime.extensions.service import configure_service_config_reader
    from app.startup.initializers import modules as startup_modules

    clients = []

    class Telegram:
        """
        记录正常模块生命周期启动的平台客户端及其账号配置。
        """

        def __init__(self, **arguments):
            """
            保存模块实际交给客户端的连接配置。
            """
            self.initial_config = arguments
            self.stop = Mock(return_value=True)
            clients.append(self)

    monkeypatch.setattr(telegram_module, "Telegram", Telegram)
    monkeypatch.setattr(startup_modules, "get_configured_system_config", lambda: config)
    previous_reader = configure_service_config_reader(startup_modules._read_service_config)
    module = TelegramModule()
    publish_change = config.async_publish_committed_change

    async def reload_after_commit(key):
        """
        使用真实模块的配置重载回调停止并重建客户端。
        """
        await publish_change(key)
        module.on_config_changed()

    monkeypatch.setattr(config, "async_publish_committed_change", reload_after_commit)
    module.init_module()
    measured: list[str] = []

    async def verify(connection):
        """
        模拟官方返回的账号身份。
        """
        token = connection.provider_config["TELEGRAM_TOKEN"]
        measured.append(token)
        return {"first-token": "10", "second-token": "10", "other-account": "20", "renewed-token": "10"}.get(token)

    service = ChannelSettingsService(config, probe=verify)
    original = service.read()
    assert original.connections[0].can_rotate_credential

    def request(secret, revision):
        """
        构造与前端请求等价的模型。
        """
        return ChannelCredentialRotation.model_validate({"secret": secret, "expected_revision": revision})

    async def rotate(secret, revision):
        """
        执行现有连接的条件写入。
        """
        return await service.rotate_credential("tg", request(secret, revision))

    try:
        rotated = await rotate("second-token", 1)
        assert rotated.identity_namespace == original.connections[0].identity_namespace
        assert rotated.revision == 2 and rotated.credential_status == "ready"
        assert measured == ["first-token", "second-token"]
        assert "second-token" not in service.read().model_dump_json()
        assert service.read().interactions == original.interactions
        assert service.read().notifications == original.notifications
        snapshot = config.get(SystemConfigKey.ChannelSettings)

        with pytest.raises(ChannelSettingsConflict, match="另一个平台账号"):
            await rotate("other-account", 2)
        assert config.get(SystemConfigKey.ChannelSettings) == snapshot
        paused = await rotate("unknown-token", 2)
        assert paused.credential_status == "verification_required" and paused.enabled
        frozen = ChannelSettings.model_validate(config.get(SystemConfigKey.ChannelSettings))
        assert frozen.connections["tg"].provider_config["TELEGRAM_TOKEN"] == "second-token"
        runtime = project_runtime_channels(frozen)[0]
        assert not runtime.enabled and not runtime.notification_enabled and not runtime.interaction_enabled
        assert module.get_instance("Telegram") is None

        with pytest.raises(ChannelSettingsConflict, match="已更新"):
            await rotate("renewed-token", 2)
        resumed = await rotate("renewed-token", 3)
        assert resumed.credential_status == "ready"
        assert measured == ["first-token", "second-token", "other-account", "unknown-token", "renewed-token"]
        assert [client.initial_config["TELEGRAM_TOKEN"] for client in clients] == [
            "first-token", "second-token", "renewed-token",
        ]
        clients[0].stop.assert_called_once()
        clients[1].stop.assert_called_once()
        assert module.get_instance("Telegram") is clients[2]
    finally:
        module.stop()
        configure_service_config_reader(previous_reader)


@pytest.mark.asyncio
async def test_settings_write_uses_separate_revisions_and_legacy_only_imports_once():
    """
    通知和交互分别保存各自的版本，重读旧键不覆盖新设置或凭据。
    """
    config = _Configuration([
        {
            "id": "tg", "type": "telegram", "name": "Telegram", "enabled": True,
            "config": {"TELEGRAM_TOKEN": "fixture-token"}, "switchs": ["下载"],
        },
        {"id": "push", "type": "webpush", "name": "浏览器", "enabled": False},
    ])
    service = ChannelSettingsService(config)
    initial = service.read()
    assert [item.connection_id for item in initial.interactions] == ["tg"]
    assert initial.interactions[0].admission == "manual"
    assert initial.admission_modes["tg"] == ("manual", "manual_rules", "rules", "open")
    assert initial.rule_kinds["tg"] == ("user", "group", "private")
    assert "fixture-token" not in initial.model_dump_json()

    notify, interact = await asyncio.gather(
        service.edit_notification("tg", NotificationRouteEdit(enabled=False, message_types=(), expected_revision=1)),
        service.edit_interaction("tg", InteractionPolicyEdit(enabled=True, expected_revision=1)),
    )
    assert (notify.enabled, interact.enabled, notify.revision, interact.revision) == (False, True, 2, 2)
    assert config.events == [SystemConfigKey.ChannelSettings, SystemConfigKey.ChannelSettings]
    with pytest.raises(ChannelSettingsConflict):
        await service.edit_notification("tg", NotificationRouteEdit(enabled=True, message_types=(), expected_revision=1))

    changed = await service.edit_connection("tg", ChannelConnectionEdit(name="重命名", enabled=True, expected_revision=1))
    assert changed.identity_namespace == initial.connections[0].identity_namespace
    assert "fixture-token" not in str(service.read_legacy())
    config.state[SystemConfigKey.Notifications] = []
    assert service.read().notifications[0] == notify
    stored = config.get(SystemConfigKey.ChannelSettings)
    assert stored["connections"]["tg"]["provider_config"]["TELEGRAM_TOKEN"] == "fixture-token"

    with pytest.raises(ChannelSettingsConflict):
        await service.replace_legacy([NotificationConf(
            id="tg", name="重命名", type="telegram", enabled=True,
            config={"TELEGRAM_TOKEN": "another-account"},
        ), NotificationConf(id="push", name="浏览器", type="webpush")])
    assert config.get(SystemConfigKey.ChannelSettings) == stored


@pytest.mark.asyncio
async def test_remove_connection_preserves_history_and_rejects_stale_writes():
    """
    另一用途的保存使移除确认过期。成功移除后删除完整配置，新建同名连接分配新的身份。
    """
    legacy = [NotificationConf(id="tg", name="机器人", type="telegram", enabled=True,
                               config={"private": "fixture"}),
              NotificationConf(id="push", name="浏览器", type="webpush", enabled=True)]
    config = _Configuration([item.model_dump() for item in legacy])

    async def reconcile(previous, current):
        """
        删除后凭据清理只读取已经提交的当前配置。
        """
        assert "tg" not in config.get(SystemConfigKey.ChannelSettings)["connections"]
        if any(item.id == "tg" for item in previous):
            assert [item.id for item in current] == ["push"]

    service = ChannelSettingsService(config, reconcile=reconcile)
    request = ChannelConnectionDelete(expected_revision=1, notification_revision=1, interaction_revision=1)
    await service.edit_interaction("tg", InteractionPolicyEdit(enabled=False, expected_revision=1))
    with pytest.raises(ChannelSettingsConflict, match="配置已更新"):
        await service.delete_connection("tg", request)
    before = service.read()
    old = before.connections[0]
    removed = await service.delete_connection("tg", request.model_copy(update={"interaction_revision": 2}))
    assert not removed.enabled and removed.revision == 2
    stored = ChannelSettings.model_validate(config.get(SystemConfigKey.ChannelSettings))
    assert "tg" not in stored.connections and "tg" not in stored.notifications and "tg" not in stored.interactions
    assert all("tg" not in order for order in stored.orders.values())
    assert [record.id for record in service.read().connections] == ["push"]
    assert [record["id"] for record in service.read_legacy()] == ["push"]

    for update in (
        service.edit_connection("tg", ChannelConnectionEdit(name="旧机器人", enabled=True, expected_revision=2)),
        service.edit_notification("tg", NotificationRouteEdit(enabled=True, message_types=(), expected_revision=1)),
    ):
        with pytest.raises(KeyError):
            await update
    new = await service.create_connection(ChannelConnectionCreate(
        name="机器人", provider="telegram", provider_config={"private": "replacement"},
    ))
    assert new.id != old.id and new.identity_namespace != old.identity_namespace
    current = ChannelSettings.model_validate(config.get(SystemConfigKey.ChannelSettings))
    assert not current.notifications[new.id].enabled
    assert not current.interactions[new.id].enabled and current.interactions[new.id].admission == "manual"


@pytest.mark.asyncio
async def test_same_name_connection_writes_cannot_make_legacy_routing_ambiguous():
    """
    并发创建只能保存一个连接，重命名冲突及大小写变体不改变已有身份。
    """
    config = _Configuration([{"id": "tg", "type": "telegram", "name": "原连接", "enabled": True}])
    service = ChannelSettingsService(config)
    created = await asyncio.gather(
        service.create_connection(ChannelConnectionCreate(name="New Bot", provider="plugin-one", provider_config={})),
        service.create_connection(ChannelConnectionCreate(name=" new bot ", provider="plugin-two", provider_config={})),
        return_exceptions=True,
    )
    (conflict,) = (result for result in created if isinstance(result, ValidationError))
    assert conflict.errors()[0]["type"] == "channel_connection_name_conflict"
    current = service.read()
    assert len(current.connections) == len(current.notifications) == 2
    assert len(config.events) == 1

    with pytest.raises(ValidationError, match="连接名称已存在"):
        await service.edit_connection("tg", ChannelConnectionEdit(name="NEW BOT", enabled=False, expected_revision=1))
    assert service.read() == current
    assert len(config.events) == 1

    changed = await service.edit_connection("tg", ChannelConnectionEdit(name="新名称", enabled=True, expected_revision=1))
    assert changed.id == changed.identity_namespace == "tg"


@pytest.mark.asyncio
async def test_plugin_notification_type_is_preserved_without_interaction_capability():
    """
    混合配置中的插件类型随内置渠道导入，始终只生成通知资源。
    """
    config = _Configuration([
        {"id": "tg", "type": "telegram", "name": "Telegram", "enabled": True},
        {"id": "custom", "type": "plugin-notify", "name": "插件提醒", "enabled": True, "config": {"profile": "alpha"}},
    ])
    service = ChannelSettingsService(config)
    assert [item.provider for item in service.read().connections] == ["telegram", "plugin-notify"]

    await service.edit_notification("tg", NotificationRouteEdit(enabled=False, message_types=(), expected_revision=1))
    snapshot = ChannelSettings.model_validate(config.get(SystemConfigKey.ChannelSettings))
    projected = {row.id: row for row in project_runtime_channels(snapshot)}
    assert projected["custom"].type == "plugin-notify"
    assert projected["custom"].config == {"profile": "alpha"}
    assert projected["custom"].enabled and projected["custom"].notification_enabled
    assert projected["custom"].interaction_enabled is False
    assert "custom" not in service.read().admission_modes
    assert next(row for row in service.read_legacy() if row["id"] == "custom")["type"] == "plugin-notify"

    created = await service.create_connection(
        ChannelConnectionCreate(name="新插件提醒", provider="second-plugin", provider_config={}),
    )
    assert created.provider == "second-plugin" and not created.enabled
    assert created.id not in service.read().admission_modes


@pytest.mark.asyncio
async def test_stale_legacy_form_cannot_undo_shared_connection_edit():
    """
    旧版表单带着旧名称和启用状态提交时，保留新版最近保存的连接。
    """
    legacy = NotificationConf(id="tg", name="旧名称", type="telegram", enabled=True)
    config = _Configuration([legacy.model_dump()])
    service = ChannelSettingsService(config)
    await service.edit_connection("tg", ChannelConnectionEdit(name="新名称", enabled=False, expected_revision=1))
    with pytest.raises(ChannelSettingsConflict, match="连接配置已更新"):
        await service.replace_legacy([legacy])
    current = service.read().connections[0]
    assert (current.name, current.enabled, current.revision) == ("新名称", False, 2)


@pytest.mark.asyncio
async def test_channel_admission_capabilities_match_write_constraints():
    """
    验证企业微信两模式可选策略、自定义网关限制及配置保存后的行为隔离。
    """
    bot_config = {"WECHAT_MODE": "bot", "WECHAT_BOT_ID": "bot-id", "WECHAT_BOT_SECRET": "fixture-secret"}
    config = _Configuration([
        {"id": "official", "type": "wechat", "name": "官方机器人", "enabled": True, "config": bot_config},
        {"id": "proxy", "type": "wechat", "name": "自定义机器人", "enabled": True,
         "config": {**bot_config, "WECHAT_BOT_WS_URL": "wss://test.invalid"}},
        {"id": "app", "type": "wechat", "name": "企业应用", "enabled": True, "config": {
            "WECHAT_CORPID": "corp", "WECHAT_APP_ID": "app", "WECHAT_ENCODING_AESKEY": "key", "WECHAT_TOKEN": "token",
        }},
    ])
    service = ChannelSettingsService(config)
    view = service.read()
    assert view.admission_modes == {
        "official": ("manual", "manual_rules", "rules", "open"),
        "proxy": ("manual", "open"),
        "app": ("manual", "manual_rules", "rules", "open"),
    }
    edit = InteractionPolicyEdit(
        enabled=True, expected_revision=1, admission="manual_rules",
        admission_rules=[{"kind": "group", "target": "g1", "scopes": ["search", "download"]}],
        auto_download="specified", auto_download_rules=[{"kind": "user", "target": "alice"}],
    )
    for unsupported in ("proxy", "app"):
        with pytest.raises(ChannelSettingsConflict, match="无法验证"):
            await service.edit_interaction(unsupported, edit)
    saved = await service.edit_interaction("official", edit)
    assert saved.admission == "manual_rules" and saved.auto_download == "specified"
    assert saved.auto_download_rules[0].target == "alice"
    assert service.read().interactions[1:] == view.interactions[1:]
    assert service.read().notifications == view.notifications
    assert "fixture-secret" not in service.read().model_dump_json()
    cleared = await service.edit_interaction("official", InteractionPolicyEdit(
        enabled=True, expected_revision=saved.revision, admission_rules=(), auto_download_rules=(),
    ))
    assert cleared.admission == "manual_rules" and not cleared.admission_rules
    assert cleared.auto_download == "specified" and not cleared.auto_download_rules



@pytest.mark.asyncio
async def test_connection_reconciliation_runs_after_commit_and_before_refresh():
    """
    两种写入口共用生命周期边界，失败后仍发布已提交配置。
    """
    config = _Configuration([{
        "id": "claw", "type": "wechatclawbot", "name": "ClawBot", "enabled": True,
    }])
    calls = []

    async def reconcile(before, after):
        """
        核对已提交名称是否只由稳定连接传给运行态。
        """
        stored = config.get(SystemConfigKey.ChannelSettings)["connections"]["claw"]["name"]
        calls.append((before[0].name, after[0].name))
        assert stored == after[0].name
        if len(calls) == 2:
            raise ValueError("模拟缓存不可用")

    service = ChannelSettingsService(config, reconcile=reconcile)
    await service.edit_notification("claw", NotificationRouteEdit(enabled=False, message_types=(), expected_revision=1))
    assert not calls
    await service.replace_legacy([NotificationConf(id="claw", name="旧客户端改名", type="wechatclawbot", enabled=True)])
    assert calls == [("ClawBot", "旧客户端改名")]

    with pytest.raises(CommittedChannelSettingsError) as saved:
        await service.edit_connection("claw", ChannelConnectionEdit(name="新名称", enabled=True, expected_revision=2))
    assert saved.value.data.name == "新名称"
    assert calls[-1] == ("旧客户端改名", "新名称")
    assert service.read().connections[0].name == "新名称"
    assert config.events == [SystemConfigKey.ChannelSettings] * 3


@pytest.mark.parametrize(
    "enabled,notify,interact,expected",
    [
        (False, True, True, (False, False, False)),
        (True, False, False, (False, False, False)),
        (True, True, False, (True, True, False)),
        (True, False, True, (True, False, True)),
        (True, True, True, (True, True, True)),
    ],
)
@pytest.mark.asyncio
async def test_shared_client_and_dispatch_obey_independent_switches(monkeypatch, enabled, notify, interact, expected):
    """
    检查运行实例是否活动，并使用模块实际过滤器检查事件通知和交互回复。
    """
    config = _Configuration([{
        "id": "tg", "type": "telegram", "name": "Telegram", "enabled": True,
        "switchs": [MessageType.Agent.value],
    }])
    service = ChannelSettingsService(config)
    await service.edit_notification("tg", NotificationRouteEdit(
        enabled=notify, message_types=(MessageType.Agent.value,), expected_revision=1,
    ))
    await service.edit_interaction("tg", InteractionPolicyEdit(enabled=interact, expected_revision=1))
    await service.edit_connection("tg", ChannelConnectionEdit(name="Telegram", enabled=enabled, expected_revision=1))
    from app.startup.initializers import modules as startup_modules

    monkeypatch.setattr(startup_modules, "get_configured_system_config", lambda: config)
    projected = startup_modules._read_service_config(SystemConfigKey.Notifications)
    (projection,) = [NotificationConf.model_validate(row) for row in projected]
    from app.application.notification import channel_interaction_is_enabled
    from app.runtime.extensions.service import ServiceConfigHelper, configure_service_config_reader

    reader = configure_service_config_reader(startup_modules._read_service_config)
    try:
        assert ServiceConfigHelper.get_notification_configs() == [projection]
        from app.application import notification as notification_application

        monkeypatch.setattr(notification_application, "get_service_configs",
                            lambda key, _: ServiceConfigHelper.get_notification_configs())
        assert channel_interaction_is_enabled(NotificationChannel.Telegram, "Telegram") is expected[2]
        assert not channel_interaction_is_enabled(NotificationChannel.Slack, "Telegram")
    finally:
        configure_service_config_reader(reader)

    module = TelegramModule()
    monkeypatch.setattr(module, "_channel", NotificationChannel.Telegram)
    monkeypatch.setattr(module, "get_config", lambda _: projection)

    event = Message(channel=NotificationChannel.Telegram, source="Telegram", mtype=MessageType.Agent, text="主动通知")
    reply = Message(channel=NotificationChannel.Telegram, source="Telegram", userid="42", delivery_kind="reply", text="搜索结果")
    assert (projection.enabled, module.check_message(event, "Telegram"), module.check_message(reply, "Telegram")) == expected


def test_channel_settings_migration_writes_final_resources(tmp_path, monkeypatch):
    """
    在隔离 SQLite 里重复升级后只保留最终资源，正式版本配置仍能读取。
    """
    import importlib

    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from app.db.models.systemconfig import SystemConfig

    migration = importlib.import_module("database.versions.62c91ad8b057_channel_resources")
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'settings.sqlite'}")
    table = SystemConfig.__table__
    legacy = [{"id": "legacy-tg", "name": "机器人", "type": "telegram"}]
    try:
        table.create(engine)
        with engine.begin() as connection:
            connection.execute(sa.insert(table).values(key=SystemConfigKey.Notifications.value, value=legacy))
            monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
            migration._migrate_channel_configuration()
            migration._migrate_channel_configuration()
            values = dict(connection.execute(sa.select(table.c.key, table.c.value)).tuples().all())
            assert values[SystemConfigKey.Notifications.value] == legacy
            current = values[SystemConfigKey.ChannelSettings.value]
            assert current["connections"]["legacy-tg"]["name"] == "机器人"
            assert current["interactions"]["legacy-tg"]["admission"] == "manual"
            assert current["notifications"]["legacy-tg"]["rules"] is not None
            with pytest.raises(RuntimeError, match="备份"):
                migration.downgrade()
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_orders_are_independent_and_reject_stale_card_sets():
    """
    页面只保存自己的顺序，删除或新建后的旧页面不能覆盖新顺序。
    """
    config = _Configuration([
        {"id": "tg", "name": "Telegram", "type": "telegram", "enabled": False, "config": {}},
        {"id": "qq", "name": "QQ", "type": "qqbot", "enabled": False, "config": {}},
        {"id": "push", "name": "Web Push", "type": "webpush", "enabled": False, "config": {}},
    ])
    service = ChannelSettingsService(config)
    initial = service.read()
    assert initial.orders == {"notification": ("tg", "qq", "push"), "interaction": ("tg", "qq")}
    await service.reorder("notification", ChannelCardOrder(
        expected_order=initial.orders["notification"], order=("push", "tg", "qq"),
    ))
    await service.reorder("interaction", ChannelCardOrder(expected_order=("tg", "qq"), order=("qq", "tg")))
    assert service.read().orders == {"notification": ("push", "tg", "qq"), "interaction": ("qq", "tg")}
    assert not config.events
    with pytest.raises(ChannelSettingsConflict):
        await service.reorder("interaction", ChannelCardOrder(expected_order=("tg", "qq"), order=("qq", "tg")))
    with pytest.raises(ValueError):
        await service.reorder("interaction", ChannelCardOrder(expected_order=("qq", "tg"), order=("tg", "tg")))


@pytest.mark.asyncio
async def test_platform_form_updates_values_without_exposing_or_overwriting_credentials():
    """
    二次编辑通过版本约束只提交允许的字段，不更换已保存的平台身份。
    """
    config = _Configuration([{
        "id": "qq", "name": "QQ", "type": "qqbot", "enabled": True,
        "config": {"QQ_APP_ID": "app", "QQ_APP_SECRET": "existing-secret", "QQ_GROUP_OPENID": "old"},
        "switchs": ["订阅"],
    }])
    service = ChannelSettingsService(config)
    form = service.read_connection_form("qq")
    assert form.values == {"QQ_APP_ID": "app"}
    assert "QQ_GROUP_OPENID" not in form.editable_fields
    route = service.read().notifications[0]
    assert route.rules and route.rules[1].target == "old"
    await service.edit_notification("qq", NotificationRouteEdit(
        enabled=True, rules=(route.rules[1].model_copy(update={"target": "new"}),), expected_revision=route.revision,
    ))
    assert "QQ_APP_SECRET" in form.editable_fields
    saved = await service.edit_connection("qq", ChannelConnectionEdit(
        name="QQ", enabled=True, expected_revision=form.revision,
        provider_config={"QQ_APP_SECRET": ""},
    ))
    snapshot = config.get(SystemConfigKey.ChannelSettings)
    assert snapshot["connections"]["qq"]["provider_config"] == {
        "QQ_APP_ID": "app", "QQ_APP_SECRET": "existing-secret",
    }
    with pytest.raises(ValueError, match="平台身份"):
        await service.edit_connection("qq", ChannelConnectionEdit(
            name="QQ", enabled=True, expected_revision=saved.revision, provider_config={"QQ_APP_ID": "another"},
        ))
    assert service.read().notifications[0].rules[0].target == "new"
    assert service.read().rule_kinds["qq"] == ("group", "user")
    await service.edit_interaction("qq", InteractionPolicyEdit(
        enabled=True, expected_revision=1, admission="open",
    ))


@pytest.mark.parametrize("mode", ["manual_allowlist", "manual_exemption", "exemption"])
def test_intermediate_policy_modes_are_rejected(mode):
    """
    最终设置只接收人工审批、人工与规则、规则、无需审批四种模式。
    """
    with pytest.raises(ValueError):
        InteractionPolicy(connection_id="qq", identity_namespace="qq", enabled=True, revision=1, admission=mode)


@pytest.mark.asyncio
async def test_legacy_client_cannot_reuse_deleted_identity():
    """
    旧客户端重传已删除 ID 时仍分配新身份，避免恢复旧授权和在途任务。
    """
    config = _Configuration([{"id": "old", "name": "Same", "type": "telegram"}])
    service = ChannelSettingsService(config)
    await service.delete_connection("old", ChannelConnectionDelete(
        expected_revision=1, notification_revision=1, interaction_revision=1,
    ))
    await service.replace_legacy([NotificationConf(id="old", name="Same", type="telegram")])
    connection = service.read().connections[0]
    assert connection.id != "old" and connection.identity_namespace != "old"


@pytest.mark.asyncio
async def test_wechat_app_edit_and_rotation_keep_identity_and_hide_credentials(monkeypatch):
    """
    企业微信应用通过真实平台扩展编辑回调和轮换 secret；诊断模拟，公开表单永不回填凭据。
    """
    from app.modules.wechat.channel import WechatPlatform
    platform = WechatPlatform()
    source = _Configuration([{
        "id": "corp", "name": "corp", "type": "wechat", "enabled": True, "switchs": ["订阅"],
        "config": {"WECHAT_MODE": "app", "WECHAT_CORPID": "fixture-corp", "WECHAT_APP_ID": "3",
                   "WECHAT_APP_SECRET": "fixture-old", "WECHAT_TOKEN": "callback-token", "WECHAT_ENCODING_AESKEY": "callback-key"},
    }])
    verify = Mock(side_effect=lambda _corp, _agent, secret: "fixture-account" if secret in {"fixture-old", "fixture-new"} else "other-account")
    monkeypatch.setattr("app.modules.wechat.wechat.WeChat.probe_app_identity", verify)

    async def probe(connection):
        """
        使用实际模块诊断逻辑，外部 API 替身保持身份比较行为。
        """
        return platform.probe_identity(connection)

    service = ChannelSettingsService(source, probe=probe)
    original, = service.read().connections
    assert original.can_rotate_credential
    form = service.read_connection_form("corp")
    assert set(form.editable_fields) == {"WECHAT_TOKEN", "WECHAT_ENCODING_AESKEY"}
    assert not {"WECHAT_TOKEN", "WECHAT_ENCODING_AESKEY", "WECHAT_APP_SECRET"}.intersection(form.values)
    edited = await service.edit_connection("corp", ChannelConnectionEdit(
        name="corp", enabled=True, expected_revision=1,
        provider_config={"WECHAT_TOKEN": "", "WECHAT_ENCODING_AESKEY": "replacement-callback-key"},
    ))
    snapshot = source.state[SystemConfigKey.ChannelSettings]
    assert snapshot["connections"]["corp"]["provider_config"]["WECHAT_TOKEN"] == "callback-token"
    assert edited.identity_namespace == original.identity_namespace
    with pytest.raises(ValueError):
        await service.edit_connection("corp", ChannelConnectionEdit(
            name="corp", enabled=True, expected_revision=2, provider_config={"WECHAT_CORPID": "replacement-corp"},
        ))
    rotated = await service.rotate_credential("corp", ChannelCredentialRotation(secret="fixture-new", expected_revision=2))
    assert rotated.revision == 3 and rotated.identity_namespace == original.identity_namespace
    verify.assert_any_call("fixture-corp", "3", "fixture-new")
    with pytest.raises(ChannelSettingsConflict, match="另一个平台账号"):
        await service.rotate_credential("corp", ChannelCredentialRotation(secret="fixture-foreign", expected_revision=3))
    for address in ("@all", "alice|bob"):
        with pytest.raises(ValueError, match="单个企业微信"):
            await service.edit_notification("corp", NotificationRouteEdit(
                enabled=True, expected_revision=1,
                rules=({"kind": "user", "target": address, "message_types": ["订阅"]},),
            ))
    assert source.state[SystemConfigKey.ChannelSettings]["connections"]["corp"]["provider_config"]["WECHAT_APP_SECRET"] == "fixture-new"


@pytest.mark.asyncio
async def test_wechat_bot_protected_fields_and_unverified_gateway_remain_closed():
    """
    智能机器人只可修改当前身份的密钥；非官方网关不能原地编辑受保护的连接参数。
    """
    from app.modules.wechat.channel import WechatPlatform
    source = _Configuration([{
        "id": "bot", "name": "bot", "type": "wechat", "enabled": True,
        "config": {"WECHAT_MODE": "bot", "WECHAT_BOT_ID": "fixture-bot", "WECHAT_BOT_SECRET": "fixture-key"},
    }])
    service = ChannelSettingsService(source)
    form = service.read_connection_form("bot")
    assert form.editable_fields == ("WECHAT_BOT_SECRET",)
    assert "WECHAT_BOT_SECRET" not in form.values
    await service.edit_connection("bot", ChannelConnectionEdit(
        name="bot", enabled=True, expected_revision=1, provider_config={"WECHAT_BOT_SECRET": "replacement-key"},
    ))
    record = ChannelSettings.model_validate(source.state[SystemConfigKey.ChannelSettings]).connections["bot"]
    platform = WechatPlatform()
    assert platform.credential_key(record) is None
    assert platform.probe_identity(record) is None
    unknown = record.model_copy(update={"provider_config": record.provider_config | {"WECHAT_BOT_WS_URL": "wss://fixture.invalid"}})
    assert platform.editable_fields(unknown) == ()
    assert platform.capabilities(unknown).rule_kinds == ()
    with pytest.raises(ValueError):
        platform.patch_config(unknown, {"WECHAT_BOT_SECRET": "new-key"})
