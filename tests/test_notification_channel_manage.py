"""
通知渠道通用管理契约（channel_manage）守护测试

验证通用模式的三条核心性质：
1. 按渠道名路由：非本渠道直接返回 None，交由分发机制继续执行其它模块
2. 动作词汇表由 schemas 契约层统一定义，未支持动作返回统一错误结构
3. 临时参数初始化封闭在模块内：无已保存配置时可基于表单参数构造临时客户端
"""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.chain.notification import NotificationChain
from app.modules.wechatclawbot import WechatClawBotModule
from app.runtime.extensions.service import ServiceConfigHelper
from app.schemas.system import NotificationConf
from app.schemas.types import NotificationAction, NotificationChannel


@pytest.fixture
def module():
    """
    为渠道管理测试创建独立的微信机器人模块实例。
    """
    return WechatClawBotModule()


def test_channel_manage_routes_only_matching_channel(module):
    """
    非本渠道的管理请求返回 None，run_module 分发将继续执行其它模块。
    """
    result = module.channel_manage(
        channel=NotificationChannel.Telegram,
        action=NotificationAction.STATUS,
    )
    assert result is None


def test_channel_manage_rejects_unknown_action(module):
    """
    动作词汇表之外的请求返回统一错误结构。
    """
    result = module.channel_manage(
        channel=NotificationChannel.WechatClawBot,
        action="not_an_action",
    )
    assert result["success"] is False
    assert "不支持" in result["message"]


def test_channel_manage_requires_saved_config_without_form_params(module, monkeypatch):
    """
    无任何配置且未提供表单参数时，返回提示保存配置的错误。
    """
    monkeypatch.setattr(module, "get_instance", lambda name=None: None)
    result = module.channel_manage(
        channel=NotificationChannel.WechatClawBot,
        action=NotificationAction.TEST_CONNECTION,
    )
    assert result["success"] is False
    assert "保存" in result["message"]


def test_channel_manage_builds_temporary_client_from_form_params(module, monkeypatch):
    """
    无已保存配置时基于表单参数构造临时客户端，实现未保存即预览。
    """
    monkeypatch.setattr(module, "get_instance", lambda name=None: None)

    captured = {}

    class _FakeClient:
        def test_connection(self):
            """
            记录临时客户端的连通性检查，并返回需要扫码绑定的失败结果。
            """
            captured["called"] = True
            return False, "未登录，请先扫码完成绑定"

        def stop(self):
            """
            记录管理请求退出后关闭临时客户端。
            """
            captured["stopped"] = True
            return True

    monkeypatch.setattr(
        "app.modules.wechatclawbot.WechatClawBot",
        lambda **kwargs: (captured.update(kwargs=kwargs), _FakeClient())[1],
    )

    result = module.channel_manage(
        channel=NotificationChannel.WechatClawBot,
        action=NotificationAction.TEST_CONNECTION,
        source="预览渠道",
        WECHATCLAWBOT_BASE_URL="http://127.0.0.1:1",
    )

    assert captured["called"] is True
    assert captured["stopped"] is True
    assert captured["kwargs"]["name"] == "预览渠道"
    assert captured["kwargs"]["auto_start_polling"] is False
    assert result["success"] is False
    assert "未登录" in result["message"]


def test_channel_manage_prefers_saved_instance(module, monkeypatch):
    """
    存在已保存配置实例时优先使用，不构造临时客户端。
    """
    saved = SimpleNamespace()
    saved.get_status = lambda refresh_remote, auto_generate_qrcode: {
        "success": True,
        "connected": True,
    }
    monkeypatch.setattr(module, "get_config", lambda name=None: SimpleNamespace(name="已保存"))
    monkeypatch.setattr(module, "get_instance", lambda name=None: saved)

    result = module.channel_manage(
        channel=NotificationChannel.WechatClawBot,
        action=NotificationAction.STATUS,
        source="已保存",
    )
    assert result["success"] is True
    assert result["data"]["connected"] is True


def test_saved_clawbot_login_targets_stable_id_even_when_disabled(module, monkeypatch):
    """
    专用登录请求只读已保存配置，停用连接使用原凭据身份且不启动轮询。
    """
    config = NotificationConf(id="stored-id", name="已保存", type="wechatclawbot", enabled=False,
                              config={"WECHATCLAWBOT_BASE_URL": "https://fixture.example"})
    monkeypatch.setattr(ServiceConfigHelper, "get_notification_configs", lambda: [config])
    status = Mock(return_value={"success": True, "connected": True})
    close = Mock(return_value=True)
    build = Mock(return_value=SimpleNamespace(get_status=status, stop=close))
    monkeypatch.setattr("app.modules.wechatclawbot.WechatClawBot", build)
    runtime = Mock()
    monkeypatch.setattr(module, "get_instance", runtime)

    for source in ("unknown", "已保存"):
        assert module.channel_manage(channel="WechatClawBot", action="status", source=source,
                                     fallback_source="stored-id", saved_connection=True)["success"] is False
    build.assert_not_called()

    resolved = module.channel_manage(channel="WechatClawBot", action="status", source="stored-id",
                                     saved_connection=True, WECHATCLAWBOT_BASE_URL="https://other.example")
    assert resolved["data"]["connected"] is True
    assert build.call_args.kwargs == {"identity": "stored-id", "name": "stored-id",
                                     "WECHATCLAWBOT_BASE_URL": "https://fixture.example", "auto_start_polling": False,
                                     "message_ingress": None}
    close.assert_called_once()
    runtime.assert_not_called()

    config.enabled = True
    monkeypatch.setattr(module, "get_config", lambda name: config if name == config.name else None)
    runtime.return_value = SimpleNamespace(get_status=status)
    assert module.channel_manage(channel="WechatClawBot", action="status", source=config.id,
                                 saved_connection=True)["data"]["connected"] is True
    runtime.assert_called_once_with(config.name)
    assert build.call_count == 1


def test_management_chain_reaches_saved_clawbot_when_no_module_is_running(monkeypatch):
    """
    宿主管理入口在无运行模块时仍可查询保存的 ClawBot，异常时也关闭临时实例。
    """
    from app.application import notification
    from app.startup.initializers.modules import _manage_inactive_notification

    config = NotificationConf(id="saved", name="已停用", type="wechatclawbot", enabled=False)
    monkeypatch.setattr(ServiceConfigHelper, "get_notification_configs", lambda: [config])
    close = Mock(return_value=True)
    client = SimpleNamespace(get_status=Mock(return_value={"success": True, "connected": True}), stop=close)
    build = Mock(return_value=client)
    monkeypatch.setattr("app.modules.wechatclawbot.WechatClawBot", build)
    monkeypatch.setattr(notification, "_inactive_channel_manager", _manage_inactive_notification)
    chain = object.__new__(NotificationChain)
    monkeypatch.setattr(chain, "run_module", Mock(return_value=None))
    params = {"channel": "WechatClawBot", "action": "status", "source": "saved", "saved_connection": True}
    assert chain.manage_channel(**params)["data"]["connected"]
    assert build.call_args.kwargs["auto_start_polling"] is False
    close.assert_called_once()

    client.get_status.side_effect = ValueError("诊断失败")
    with pytest.raises(ValueError, match="诊断失败"):
        chain.manage_channel(**params)
    assert close.call_count == 2

    monkeypatch.setattr(ServiceConfigHelper, "get_notification_configs", lambda: [])
    assert not chain.manage_channel(**params)["success"]
    assert build.call_count == 2

    assert _manage_inactive_notification("WechatClawBot", "delete", params) is None
    assert _manage_inactive_notification("WechatClawBot", "status", {**params, "saved_connection": False}) is None
    assert build.call_count == 2


def test_channel_manage_migrate_cache_dispatches_without_client(module, monkeypatch):
    """
    缓存迁移动作不依赖客户端实例，直接走静态迁移逻辑。
    """

    def _fake_migrate(old_name, new_name, cleanup_old, overwrite):
        assert old_name == "旧名"
        assert new_name == "新名"
        return True, "迁移成功"

    monkeypatch.setattr(
        "app.modules.wechatclawbot.WechatClawBot.migrate_cached_state",
        staticmethod(_fake_migrate),
    )

    result = module.channel_manage(
        channel=NotificationChannel.WechatClawBot,
        action=NotificationAction.MIGRATE_CACHE,
        old_name="旧名",
        new_name="新名",
    )
    assert result == {"success": True, "message": "迁移成功"}


def test_notification_chain_forwards_target_action_and_params(monkeypatch):
    """
    链层接受字符串标识与透传参数，按 channel_manage 契约原样转发。
    """
    captured = {}

    def fake_run_module(self, method, **kwargs):
        captured.update(method=method, kwargs=kwargs)
        return {"success": True, "data": {"connected": True}}

    monkeypatch.setattr(NotificationChain, "run_module", fake_run_module)
    chain = NotificationChain.__new__(NotificationChain)
    result = chain.manage_channel(channel="WechatClawBot", action="status", source="预览")

    assert captured["method"] == "channel_manage"
    assert captured["kwargs"] == {
        "channel": "WechatClawBot",
        "action": "status",
        "source": "预览",
    }
    assert result["success"] is True


def test_notification_chain_reports_missing_module(monkeypatch):
    """
    无模块实现 channel_manage 时返回统一失败结构。
    """
    monkeypatch.setattr(NotificationChain, "run_module", lambda self, method, **kwargs: None)
    chain = NotificationChain.__new__(NotificationChain)
    result = chain.manage_channel(channel="unknown", action="status")
    assert result["success"] is False
    assert result["message"]


def test_channel_manage_accepts_plain_string_identifiers(module, monkeypatch):
    """
    端点层透传的原始字符串渠道名与动作名可被模块正确路由与解释。
    """
    saved = SimpleNamespace()
    saved.test_connection = lambda: (True, None)
    monkeypatch.setattr(module, "get_instance", lambda name=None: saved)

    result = module.channel_manage(channel="WechatClawBot", action="test_connection")
    assert result == {"success": True, "message": None}
