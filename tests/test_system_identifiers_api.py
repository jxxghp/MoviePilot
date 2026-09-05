"""自定义识别词专用 API 合同测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

import app.api.endpoints.identifier as system_endpoint
from app.application.settings import SystemSettingConflictError
from app.schemas.system import CustomIdentifiersUpdateRequest
from app.schemas.types import SystemConfigKey


class _RecordingSettingsService:
    """记录专用接口传入设置服务的条件替换参数。"""

    calls: list[dict] = []
    error: Exception | None = None

    def __init__(self, *_args) -> None:
        """兼容真实设置服务的构造参数。"""

    async def update(self, **kwargs):
        """记录调用，并按测试需要返回或抛出结果。"""
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {"message": "updated", "changed": True}


@pytest.fixture(autouse=True)
def reset_recording_service() -> None:
    """隔离每个接口测试的记录和异常状态。"""
    _RecordingSettingsService.calls = []
    _RecordingSettingsService.error = None


@pytest.mark.asyncio
async def test_query_identifiers_returns_only_public_string_rules(monkeypatch) -> None:
    """查询接口应过滤历史坏项，使返回值与字符串请求合同一致。"""
    config = MagicMock()
    config.get.return_value = ["A", None, 7, "B"]
    monkeypatch.setattr(system_endpoint, "get_configured_system_config", lambda: config)

    response = await system_endpoint.query_custom_identifiers(_=object())

    assert response.data == {"count": 2, "identifiers": ["A", "B"]}


@pytest.mark.asyncio
async def test_update_identifiers_forwards_expected_snapshot(monkeypatch) -> None:
    """专用写接口应把前端基线作为原子条件传给设置服务。"""
    monkeypatch.setattr(system_endpoint, "SystemSettingsService", _RecordingSettingsService)
    monkeypatch.setattr(system_endpoint, "get_runtime_settings", MagicMock())
    monkeypatch.setattr(system_endpoint, "get_configured_system_config", MagicMock())
    runtime = SimpleNamespace(system=SimpleNamespace(publish_config_changed=AsyncMock()))

    response = await system_endpoint.update_custom_identifiers(
        payload=CustomIdentifiersUpdateRequest(
            identifiers=["A", "B"],
            expected_identifiers=["A"],
        ),
        _=object(),
        runtime=runtime,
    )

    assert response.success is True
    assert response.data["identifiers"] == ["A", "B"]
    assert _RecordingSettingsService.calls == [
        {
            "setting_key": SystemConfigKey.CustomIdentifiers.value,
            "value": ["A", "B"],
            "expected_value": ["A"],
            "enforce_expected_value": True,
        }
    ]


@pytest.mark.asyncio
async def test_update_identifiers_maps_stale_snapshot_to_http_409(monkeypatch) -> None:
    """过期识别词基线必须返回冲突，不能伪装为保存成功。"""
    _RecordingSettingsService.error = SystemSettingConflictError("配置已被其他会话更新")
    monkeypatch.setattr(system_endpoint, "SystemSettingsService", _RecordingSettingsService)
    monkeypatch.setattr(system_endpoint, "get_runtime_settings", MagicMock())
    monkeypatch.setattr(system_endpoint, "get_configured_system_config", MagicMock())
    runtime = SimpleNamespace(system=SimpleNamespace(publish_config_changed=AsyncMock()))

    with pytest.raises(HTTPException) as error:
        await system_endpoint.update_custom_identifiers(
            payload=CustomIdentifiersUpdateRequest(
                identifiers=["mine"],
                expected_identifiers=["stale"],
            ),
            _=object(),
            runtime=runtime,
        )

    assert error.value.status_code == 409
    assert error.value.detail == "配置已被其他会话更新"
