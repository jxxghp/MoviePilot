"""插件市场同步与异步包代际解析的统一合同测试。"""

from types import SimpleNamespace

import pytest

from app.adapters.external import market
from app.adapters.external.market import PluginHelper


@pytest.mark.parametrize(
    ("configured_version", "requested_version", "expected"),
    [
        ("v3", None, ("v3", "v2", "")),
        ("v3", "v2", ("v2", "")),
        ("", None, ("",)),
    ],
)
def test_package_version_candidates_have_one_canonical_order(
    monkeypatch,
    configured_version: str,
    requested_version: str | None,
    expected: tuple[str, ...],
) -> None:
    """显式版本、向后兼容版本和基础索引必须由一个有序事实源产生。"""
    monkeypatch.setattr(
        market,
        "settings",
        SimpleNamespace(VERSION_FLAG=configured_version),
    )

    assert PluginHelper._package_version_candidates(requested_version) == expected


@pytest.mark.asyncio
async def test_sync_and_async_package_resolution_visit_same_candidates(
    monkeypatch,
) -> None:
    """同步与异步安装必须按相同顺序停止在首个兼容插件索引。"""
    monkeypatch.setattr(
        market,
        "settings",
        SimpleNamespace(VERSION_FLAG="v3"),
    )
    helper = PluginHelper.__new__(PluginHelper)
    indexes = {
        "v3": {},
        "v2": {"CompatiblePlugin": {"version": "1.0.0"}},
        None: {"CompatiblePlugin": {"version": "0.9.0", "v2": True}},
    }
    sync_candidates: list[str | None] = []
    async_candidates: list[str | None] = []

    def get_plugins(_repo_url: str, package_version: str | None = None) -> dict:
        """记录同步入口访问顺序并返回固定索引。"""
        sync_candidates.append(package_version)
        return indexes[package_version]

    async def async_get_plugins(
        _repo_url: str,
        package_version: str | None = None,
    ) -> dict:
        """记录异步入口访问顺序并返回同一份固定索引。"""
        async_candidates.append(package_version)
        return indexes[package_version]

    monkeypatch.setattr(helper, "get_plugins", get_plugins)
    monkeypatch.setattr(helper, "async_get_plugins", async_get_plugins)

    sync_result = helper.get_plugin_package_version(
        "CompatiblePlugin",
        "https://github.com/example/plugins",
    )
    async_result = await helper.async_get_plugin_package_version(
        "CompatiblePlugin",
        "https://github.com/example/plugins",
    )

    assert sync_result == async_result == "v2"
    assert sync_candidates == async_candidates == ["v3", "v2"]
