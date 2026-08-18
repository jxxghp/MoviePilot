"""
网盘存储通用管理契约（storage_manage）守护测试

验证与通知渠道一致的通用模式：
1. 链层 manage_storage 只透明转发存储标识、动作与参数，不做任何存储特定处理
2. 模块按存储标识路由，动作语义与参数解释封闭在模块内
3. 端点层的 ManageRequest 通用请求结构：target + action + params
"""
from types import SimpleNamespace
from typing import Any, Dict

import pytest

from app import schemas
from app.chain.storage import StorageChain
from app.modules.filemanager import FileManagerModule


class _FakeStorageOper:
    """记录管理动作调用情况的假存储实现"""

    schema = SimpleNamespace(value="fakestore")
    calls = []

    def set_config(self, conf):
        _FakeStorageOper.calls.append(("set_config", conf))

    def reset_config(self):
        _FakeStorageOper.calls.append(("reset_config", None))

    def usage(self):
        return schemas.StorageUsage(total=100, available=40)

    def support_transtype(self):
        return {"move": True}

    def check_login(self, **kwargs):
        _FakeStorageOper.calls.append(("check_login", kwargs))
        return {"status": True}, None


@pytest.fixture
def module(monkeypatch):
    _FakeStorageOper.calls.clear()
    module = FileManagerModule()
    monkeypatch.setattr(module, "_support_storages", ["fakestore"])
    monkeypatch.setattr(module, "_storage_schemas", [_FakeStorageOper])
    return module


def test_manage_request_schema():
    """ManageRequest 仅定义目标标识、动作标识与透传参数，无任何特定领域字段。"""
    request = schemas.ManageRequest(target="fakestore", action="usage")
    assert request.target == "fakestore"
    assert request.action == "usage"
    assert request.params == {}


def test_storage_chain_forwards_target_action_and_params(monkeypatch):
    """链层按 storage_manage 契约原样透传，不引入存储特定逻辑。"""
    captured = {}

    def fake_unicast(self, method, **kwargs):
        captured.update(method=method, kwargs=kwargs)
        return {"success": True, "data": {"total": 100}}

    monkeypatch.setattr(StorageChain, "unicast", fake_unicast)
    chain = StorageChain.__new__(StorageChain)
    result = chain.manage_storage(storage="fakestore", action="usage", extra="value")

    assert captured["method"] == "storage_manage"
    assert captured["kwargs"] == {"storage": "fakestore", "action": "usage", "extra": "value"}
    assert result["success"] is True


def test_storage_chain_reports_missing_module(monkeypatch):
    """无模块实现 storage_manage 时返回统一失败结构。"""
    monkeypatch.setattr(StorageChain, "unicast", lambda self, method, **kwargs: None)
    chain = StorageChain.__new__(StorageChain)
    result = chain.manage_storage(storage="unknown", action="usage")
    assert result["success"] is False
    assert result["message"]


def test_storage_manage_rejects_unknown_action(module):
    """动作词汇表之外的请求返回统一错误结构。"""
    result = module.storage_manage(storage="fakestore", action="not_an_action")
    assert result["success"] is False
    assert "不支持" in result["message"]


def test_storage_manage_rejects_unknown_storage(module):
    """未注册的存储标识直接返回错误，不进入动作分发。"""
    result = module.storage_manage(storage="unknown_store", action="usage")
    assert result["success"] is False
    assert "不支持的存储类型" in result["message"]


def test_storage_manage_save_config_passes_conf_through(module):
    """save_config 动作将 params.conf 原样交给存储实现持久化。"""
    result = module.storage_manage(
        storage="fakestore", action="save_config", conf={"token": "abc"}
    )
    assert result["success"] is True
    assert ("set_config", {"token": "abc"}) in _FakeStorageOper.calls


def test_storage_manage_usage_returns_oper_data(module):
    """usage 动作返回纯 dict 用量数据，空结果同样成功。"""
    result = module.storage_manage(storage="fakestore", action="usage")
    assert result["success"] is True
    assert result["data"] == {"total": 100.0, "available": 40.0}


def test_storage_manage_usage_data_passes_open_mapping_response(module):
    """用量数据必须能透过通用响应 Response[Dict[str, Any]] 的开放映射校验。

    回归守护：存储实现返回 pydantic 模型时若未转 dict，
    端点响应校验会直接 500，前端存储页整体报未知错误。
    """
    result = module.storage_manage(storage="fakestore", action="usage")
    response = schemas.Response[Dict[str, Any]](
        success=result["success"], message=result.get("message"), data=result["data"]
    )
    assert response.data == {"total": 100.0, "available": 40.0}


def test_storage_manage_usage_defaults_when_oper_returns_none(module, monkeypatch):
    """存储实现查不到用量时返回成功的默认空结构而非业务失败。"""
    monkeypatch.setattr(_FakeStorageOper, "usage", lambda self: None)
    result = module.storage_manage(storage="fakestore", action="usage")
    assert result["success"] is True
    assert result["data"] == {"total": 0.0, "available": 0.0}


def test_storage_manage_support_transtype(module):
    """support_transtype 动作返回值包装为 transtype，与旧契约结构一致。"""
    result = module.storage_manage(storage="fakestore", action="support_transtype")
    assert result["success"] is True
    assert result["data"] == {"transtype": {"move": True}}


def test_storage_manage_support_transtype_empty_still_succeeds(module, monkeypatch):
    """存储不支持任何整理方式时返回成功空结构，避免前端把空结果当业务失败。"""
    monkeypatch.setattr(_FakeStorageOper, "support_transtype", lambda self: {})
    result = module.storage_manage(storage="fakestore", action="support_transtype")
    assert result["success"] is True
    assert result["data"] == {"transtype": {}}


def test_storage_manage_login_action_forwards_params(module):
    """登录类动作透传表单参数并归一化元组返回值。"""
    result = module.storage_manage(storage="fakestore", action="check_login", ck="ck1", t="t1")
    assert result["success"] is True
    assert result["data"] == {"status": True}
    assert ("check_login", {"ck": "ck1", "t": "t1"}) in _FakeStorageOper.calls


def test_storage_manage_reports_unsupported_login_action(module):
    """存储实现未提供对应登录方法时返回明确失败信息。"""
    result = module.storage_manage(storage="fakestore", action="generate_qrcode")
    assert result["success"] is False
    assert "不支持" in result["message"]
