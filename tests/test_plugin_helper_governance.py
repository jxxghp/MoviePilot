"""PluginHelper 兼容门面与插件 Application/Adapter owner 的治理回归。"""

import ast
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from app.adapters.external.market import PluginHelper
from app.adapters.external.plugin.client import (
    build_local_repo_url,
    parse_local_repo_generation,
    parse_local_repo_path,
)
from app.adapters.system.plugin.package import (
    PluginPackageCheckpoint,
    PluginPackageManager,
)
from app.domain.plugin import (
    build_local_plugin_source,
    build_plugin_release_install_plan,
    check_plugin_system_version,
    compatible_plugin_generations,
    is_plugin_generation_compatible,
    parse_local_plugin_generation,
    parse_local_plugin_path,
    parse_local_plugin_reference,
    plugin_generation_candidates,
)


def _class_methods(path: Path, class_name: str) -> set[str]:
    """读取生产类的方法集合，避免兼容门面重新吸收具体实现。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                item.name
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
    raise AssertionError(f"未找到生产类：{class_name}")


def test_helper_keeps_package_implementation_out_of_compat_facade() -> None:
    """Helper 不得重新物理拥有安装、解压、备份或恢复实现。"""
    methods = _class_methods(
        Path("app/adapters/external/market.py"),
        "PluginHelper",
    )
    forbidden = {
        "__install_package",
        "__async_install_package",
        "__install_from_release",
        "__async_install_from_release",
        "__backup_plugin",
        "__async_backup_plugin",
        "__restore_plugin",
        "__async_restore_plugin",
        "__remove_old_plugin",
        "__async_remove_old_plugin",
        "__validate_release_zip_name",
        "__validate_release_zip_type",
        "__iter_release_zip_targets",
    }
    assert methods.isdisjoint(forbidden), sorted(methods & forbidden)


def test_package_owner_has_no_helper_private_port() -> None:
    """包 owner 与组合根不得反向解析 Helper 私有实现。"""
    helper_private_prefix = "_Plugin" "Helper__"
    package_path = Path("app/adapters/system/plugin/package.py")
    package_source = package_path.read_text(encoding="utf-8")
    startup_source = Path("app/startup/initializers/plugins.py").read_text(
        encoding="utf-8"
    )
    assert "PluginHelper" not in package_source
    assert helper_private_prefix not in package_source
    assert helper_private_prefix not in startup_source

    package_tree = ast.parse(package_source)
    package_class = next(
        node
        for node in package_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "PluginPackageManager"
    )
    constructor = next(
        node
        for node in package_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    constructor_arguments = {
        argument.arg
        for argument in (*constructor.args.args, *constructor.args.kwonlyargs)
    }
    assert "helper" not in constructor_arguments


def test_plugin_tests_do_not_reach_helper_private_implementation() -> None:
    """插件测试只能验证 Helper 公开 ABI，市场实现细节应直接落到 owner。"""
    helper_private_prefix = "_Plugin" "Helper__"
    forbidden_attributes = {
        "_package_version_candidates",
        "_build_remote_plugin_install_plan",
    }
    violations = []

    for test_path in sorted(Path("tests").glob("test_plugin*.py")):
        source = test_path.read_text(encoding="utf-8")
        if helper_private_prefix in source:
            violations.append(f"{test_path}: Helper name-mangled private access")
        for node in ast.walk(ast.parse(source)):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "PluginHelper"
                and node.attr in forbidden_attributes
            ):
                violations.append(
                    f"{test_path}:{node.lineno}: PluginHelper.{node.attr}"
                )

    assert violations == []


def test_local_source_policy_round_trip(tmp_path: Path) -> None:
    """本地来源编码、插件 ID、路径和代际解析共享同一纯领域合同。"""
    repo_path = tmp_path / "plugin source"
    repo_url = build_local_repo_url(
        "Demo Plugin",
        repo_path=repo_path,
        package_version="v3",
    )

    assert parse_local_plugin_reference(repo_url) == "Demo Plugin"
    assert parse_local_repo_path(repo_url, root_path=tmp_path) == repo_path.resolve()
    assert parse_local_repo_generation(repo_url) == "v3"
    assert PluginHelper.make_local_repo_url(
        "Demo Plugin",
        repo_path,
        "v3",
    ) == repo_url


def test_local_source_policy_covers_empty_and_invalid_references() -> None:
    """纯领域解析器对无参数、非本地和畸形本地标识返回稳定结果。"""
    assert build_local_plugin_source("Demo Plugin") == "local://Demo%20Plugin"
    assert parse_local_plugin_reference("https://example.com/plugin") is None
    assert parse_local_plugin_path("https://example.com/plugin") is None
    assert parse_local_plugin_generation("https://example.com/plugin") is None
    assert parse_local_plugin_reference("local://") is None
    assert parse_local_plugin_path("local://Demo") is None
    assert parse_local_plugin_generation("local://Demo") is None
    assert parse_local_plugin_reference("local://[") is None
    assert parse_local_plugin_path("local://[") is None
    assert parse_local_plugin_generation("local://[") is None


def test_generation_candidate_policy_is_stable_and_deduplicated() -> None:
    """兼容代际和安装候选顺序必须稳定，并把基础索引放在末尾。"""
    assert compatible_plugin_generations(None) == ()
    assert compatible_plugin_generations("v3") == ("v3", "v2")
    assert compatible_plugin_generations("v2") == ("v2",)
    assert plugin_generation_candidates(None, current_generation="v3") == (
        "v3",
        "v2",
        "",
    )
    assert plugin_generation_candidates("v2", current_generation="v3") == (
        "v2",
        "",
    )


@pytest.mark.parametrize(
    ("metadata", "generation", "expected"),
    [
        ({"v3": True}, None, True),
        ({"v2": True}, None, True),
        ({"v3": False, "v2": True}, None, False),
        ({"v3": False}, "v3", False),
        ({"v3t": False}, "v3", False),
    ],
)
def test_generation_policy_has_one_explicit_false_rule(
    metadata: dict,
    generation: str | None,
    expected: bool,
) -> None:
    """当前代际显式 false 和自由线程显式 false 都不得被专用索引绕过。"""
    assert is_plugin_generation_compatible(
        metadata,
        generation,
        current_generation="v3",
        free_threaded=True,
    ) is expected


@pytest.mark.parametrize(
    ("metadata", "generation", "current", "expected"),
    [
        (object(), None, "v3", False),
        ({}, None, None, True),
        ({}, "v3", None, False),
        ({}, "v3", "v3", True),
        ({}, "v2", "v3", True),
        ({}, "v1", "v3", False),
        ({"v3": True}, None, "v3", True),
    ],
)
def test_generation_policy_covers_all_owner_decisions(
    metadata: object,
    generation: str | None,
    current: str | None,
    expected: bool,
) -> None:
    """领域 owner 必须独立覆盖无代际、当前代际、兼容代际和未知代际。"""
    assert is_plugin_generation_compatible(
        metadata,
        generation,
        current_generation=current,
        free_threaded=False,
    ) is expected


@pytest.mark.parametrize(
    ("metadata", "current_version", "compatible", "message_part"),
    [
        (object(), "3.0.0", True, ""),
        ({}, "3.0.0", True, ""),
        ({"system_version": 3}, "3.0.0", False, "必须是字符串"),
        ({"system_version": "not a specifier"}, "3.0.0", False, "格式不正确"),
        ({"system_version": "==3.0.0"}, "not-a-version", False, "无法解析"),
        ({"system_version": "==3.0.0"}, "3.0.0", True, ""),
        ({"system_version": "==4.0.0"}, "3.0.0", False, "不满足"),
    ],
)
def test_system_version_policy_covers_valid_and_invalid_contracts(
    metadata: object,
    current_version: str,
    compatible: bool,
    message_part: str,
) -> None:
    """系统版本约束必须拒绝错误类型、错误格式、坏宿主版本和不匹配范围。"""
    accepted, message = check_plugin_system_version(
        metadata,
        current_version=current_version,
    )
    assert accepted is compatible
    assert message_part in message


def test_release_plan_policy_rejects_unlisted_version() -> None:
    """指定 Release 必须来自市场已经读取的可安装版本列表。"""
    plan, message = build_plugin_release_install_plan(
        plugin_id="Demo",
        metadata={"release": True, "version": "2.0.0"},
        release_version="1.0.0",
        release_items=(),
        current_version="3.0.0",
    )

    assert plan is None
    assert message == "Demo 未找到可安装的 Release 版本：1.0.0"


def _checkpoint(tmp_path: Path) -> PluginPackageCheckpoint:
    """构造不触碰文件系统的包事务快照。"""
    return PluginPackageCheckpoint(
        plugin_id="Demo",
        plugin_dir=tmp_path / "demo",
        persistent_backup_dir=tmp_path / "backup",
        backup_staging_dir=None,
        backup_previous_dir=None,
        transaction_dir=tmp_path / "transaction",
        plugin_existed=True,
        persistent_backup_existed=False,
    )


def test_package_owner_disables_compat_backup_when_checkpoint_exists(
    tmp_path: Path,
) -> None:
    """主安装事务已有 durable checkpoint 时，兼容下载层不得再维护第二套备份。"""
    manager = PluginPackageManager(Mock())
    manager.install_raw = Mock(return_value=(True, ""))
    checkpoint = _checkpoint(tmp_path)

    assert manager.install(
        "Demo",
        "https://github.com/example/plugins",
        checkpoint=checkpoint,
    ) == (True, "")
    assert manager.install_raw.call_args.kwargs["force_install"] is True


@pytest.mark.asyncio
async def test_async_package_owner_disables_compat_backup_when_checkpoint_exists(
    tmp_path: Path,
) -> None:
    """异步主安装事务同样只允许 durable checkpoint 拥有恢复材料。"""
    manager = PluginPackageManager(Mock())
    manager.async_install_raw = AsyncMock(return_value=(True, ""))
    checkpoint = _checkpoint(tmp_path)

    assert await manager.async_install(
        "Demo",
        "https://github.com/example/plugins",
        checkpoint=checkpoint,
    ) == (True, "")
    assert manager.async_install_raw.call_args.kwargs["force_install"] is True
