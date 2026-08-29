"""远端插件同步与异步安装模式共享策略测试。"""

import pytest

from app.domain.plugin import build_plugin_release_install_plan


@pytest.mark.parametrize(
    ("meta", "release_version", "release_items", "tag", "fallback", "error"),
    [
        ({"release": False, "version": "1.2.3"}, None, [], None, False, ""),
        ({"release": True, "version": "1.2.3"}, None, [], "DemoPlugin_v1.2.3", True, ""),
        (
            {"release": True, "version": "1.2.3", "system_version": ">=9"},
            "1.2.0",
            [{"version": "1.2.0"}],
            "DemoPlugin_v1.2.0",
            False,
            "",
        ),
        (
            {"release": True, "version": "1.2.3"},
            "1.2.0",
            [{"version": "1.2.3"}],
            None,
            False,
            "DemoPlugin 未找到可安装的 Release 版本：1.2.0",
        ),
        (
            {"release": False, "version": "1.2.3"},
            "1.2.0",
            [{"version": "1.2.0"}],
            None,
            False,
            "DemoPlugin 未声明 Release 安装，无法安装指定版本",
        ),
        (
            {"release": True},
            None,
            [],
            None,
            False,
            "未在插件清单中找到 DemoPlugin 的版本号，无法进行 Release 安装",
        ),
    ],
)
def test_remote_plugin_install_plan_preserves_mode_contract(
    meta: dict,
    release_version: str | None,
    release_items: list[dict],
    tag: str | None,
    fallback: bool,
    error: str,
) -> None:
    """唯一计划器必须保留文件列表、当前 Release、指定 Release 与拒绝结果。"""
    plan, message = build_plugin_release_install_plan(
        plugin_id="DemoPlugin",
        metadata=meta,
        release_version=release_version,
        release_items=release_items,
        current_version="3.0.0",
    )

    assert message == error
    assert (plan.release_tag if plan else None) == tag
    assert (plan.fallback_to_filelist if plan else False) is fallback


def test_current_release_keeps_system_version_admission() -> None:
    """指定当前索引版本仍须通过系统版本限制，旧 Release 则沿用历史兼容语义。"""
    plan, message = build_plugin_release_install_plan(
        plugin_id="DemoPlugin",
        metadata={
            "release": True,
            "version": "1.2.3",
            "system_version": ">=9",
        },
        release_version="1.2.3",
        release_items=[{"version": "1.2.3"}],
        current_version="3.0.0",
    )

    assert plan is None
    assert "MoviePilot 版本 >=9" in message
