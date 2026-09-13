"""插件实例彻底清理的路径穿越回归测试。

彻底清理是唯一会对用户可控标识拼出的目录执行递归删除的入口，而插件数据根是配置根下
的一层子目录。移植这项功能时原实现把标识直接拼进 ``PLUGIN_DATA_PATH`` 再 ``rmtree``，
``%2e%2e`` 经 ASGI 解码后就是 ``..``，一次请求即递归删掉整个配置目录。这里从 HTTP 入口
把这条攻击路径按编码形式逐个钉住。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_active_superuser
from app.api.dependencies.plugin import get_plugin_config_command
from app.api.endpoints import plugininstance as plugininstance_endpoint
from app.application.plugin.config import PluginConfigCommand, PluginPurgeScope
from app.runtime.extensions.plugin import datadir as datadir_module
from app.runtime.extensions.plugin.datadir import remove_plugin_data_directory

# 路径段在 URL 里可以写成的形态。服务端拿到的是 ASGI 解码之后的结果，因而这些写法在
# 处理函数里各自等价于 `..`、`../` 等——只在路由层面过滤字面量 `..` 一个都拦不住。
TRAVERSAL_URL_SEGMENTS = (
    "..",
    "%2e%2e",
    "%2E%2E",
    "..%5c",
    "%2e%2e%5c",
    ".%2e",
    "%2e",
)

# 到达用例层的解码后标识，逐个都要被格式校验挡掉。
TRAVERSAL_IDS = (
    "..",
    "../",
    "..\\",
    "./..",
    ".././..",
    "a/../..",
    "/etc",
    "C:\\Windows",
    "",
    ".",
    "Demo Plugin",
    "1Plugin",
)


class _NullMutation:
    """无准入约束的 mutation 替身。"""

    def __enter__(self) -> None:
        """进入空上下文。"""

    def __exit__(self, *_exc: object) -> bool:
        """退出空上下文且不吞异常。"""
        return False


@pytest.fixture(name="config_root")
def fixture_config_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """搭出「配置根 / 插件数据根 / 同级受保护内容」这一真实布局。"""
    config_root = tmp_path / "config"
    plugin_root = config_root / "plugins"
    (plugin_root / "DemoPlugin").mkdir(parents=True)
    (plugin_root / "DemoPlugin" / "cache.bin").write_text("x", encoding="utf-8")
    (config_root / "protected").mkdir()
    (config_root / "protected" / "keep.txt").write_text("must survive", encoding="utf-8")
    (config_root / "user.db").write_text("must survive", encoding="utf-8")
    monkeypatch.setattr(
        datadir_module,
        "get_runtime_setting",
        lambda key, default=None: plugin_root if key == "PLUGIN_DATA_PATH" else default,
    )
    return config_root


def _purge_command(calls: list[tuple]) -> PluginConfigCommand:
    """装出一个只有目录删除是真实实现的清理用例。

    其余端口全部记账：本文件要断言的是「越界目录没有被删」以及「越界请求根本没有走到
    任何删除动作」，把真实存储接进来只会让失败原因变糊。
    """
    return PluginConfigCommand(
        save_config=lambda *a: True,
        initialize=lambda *a: None,
        stop=lambda plugin_id: calls.append(("stop", plugin_id)),
        delete_config=lambda plugin_id, force: calls.append(("config", plugin_id)),
        delete_data=lambda *a: True,
        reload_runtime=lambda *a: None,
        publish_reset=lambda *a: None,
        refresh_registrations=lambda *a: None,
        mutation=lambda _operation: _NullMutation(),
        delete_plugin_data_rows=lambda plugin_id: calls.append(("rows", plugin_id)),
        destroy_own_database=lambda plugin_id: calls.append(("own_db", plugin_id)),
        delete_data_directory=remove_plugin_data_directory,
        purge_instance=lambda plugin_id: True,
        is_clone=lambda _plugin_id: False,
    )


def _client(calls: list[tuple]) -> TestClient:
    """挂上真实路由与真实目录删除实现的测试客户端。"""
    app = FastAPI()
    app.include_router(plugininstance_endpoint.router, prefix="/api/v1/plugin")
    app.dependency_overrides[get_current_active_superuser] = lambda: object()
    app.dependency_overrides[get_plugin_config_command] = lambda: _purge_command(calls)
    return TestClient(app)


def _sentinels_intact(config_root: Path) -> bool:
    """插件数据根之外的内容是否原封不动。"""
    return (
        (config_root / "protected" / "keep.txt").is_file()
        and (config_root / "user.db").is_file()
        and (config_root / "plugins" / "DemoPlugin" / "cache.bin").is_file()
    )


def test_percent_encoded_dot_dot_really_reaches_the_handler(
    config_root: Path,
) -> None:
    """``%2e%2e`` 确实会被路由匹配并交到处理函数手里，服务端才是最后一道防线。

    客户端与代理通常会把字面量 ``..`` 规范化掉（这里表现为 404），百分号编码则原样
    送达、由 ASGI 解码成 ``..``。把「路由层面过滤 `..`」当成防护是无效的，这条用例
    就是用来防止将来有人据此撤掉服务端校验。
    """
    calls: list[tuple] = []

    response = _client(calls).post(
        "/api/v1/plugin/instance/%2e%2e/purge",
        json={"data_directory": True},
    )

    assert response.status_code == 200
    assert response.json()["success"] is False
    assert calls == []
    assert _sentinels_intact(config_root)


@pytest.mark.parametrize("segment", TRAVERSAL_URL_SEGMENTS)
def test_no_url_encoding_of_dot_dot_deletes_outside_the_plugin_data_root(
    config_root: Path,
    segment: str,
) -> None:
    """越界标识的任何 URL 写法都不得删到插件数据根之外，也不得触发任何一步删除。

    未修版本对 ``%2e%2e`` 会返回 200、``purged: ["data_directory"]``，而配置根下的
    全部内容已被 ``rmtree`` 清空——``PLUGIN_DATA_PATH/..`` 正是配置根。
    """
    calls: list[tuple] = []

    response = _client(calls).post(
        f"/api/v1/plugin/instance/{segment}/purge",
        json={"data_directory": True},
    )

    # 路由不匹配（404）同样算拦住；匹配上就必须由用例层拒绝
    assert response.status_code in (200, 404)
    if response.status_code == 200:
        assert response.json()["success"] is False
    assert calls == []
    assert _sentinels_intact(config_root)


@pytest.mark.parametrize("instance_id", TRAVERSAL_IDS)
def test_purge_refuses_every_malformed_instance_id(
    config_root: Path,
    instance_id: str,
) -> None:
    """解码后的非法标识逐个被拒，且在停实例之前就被拒。

    校验放在最前面，非法标识不该先把配置行删掉再发现路径不对。
    """
    calls: list[tuple] = []

    result = _purge_command(calls).purge(
        instance_id,
        PluginPurgeScope(
            config=True,
            plugin_data=True,
            own_database=True,
            data_directory=True,
        ),
    )

    assert result.success is False
    assert result.purged == ()
    assert calls == []
    assert _sentinels_intact(config_root)


def test_purge_deletes_the_directory_for_a_well_formed_instance_id(
    config_root: Path,
) -> None:
    """合法标识照常清理，且只动它自己那一层。"""
    calls: list[tuple] = []

    result = _purge_command(calls).purge(
        "DemoPlugin", PluginPurgeScope(data_directory=True)
    )

    assert result.success is True
    assert result.purged == ("data_directory",)
    assert not (config_root / "plugins" / "DemoPlugin").exists()
    assert (config_root / "protected" / "keep.txt").is_file()
    assert (config_root / "user.db").is_file()


@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="Windows 下建软链需要额外权限，逃逸语义由 POSIX 用例覆盖",
)
def test_purge_reports_a_refused_directory_removal_instead_of_raising(
    config_root: Path,
) -> None:
    """兜底判断拒绝删除时回报失败，而不是让 ValueError 变成 500。

    实例 ID 合法但目录本身被换成了指向根外的软链，这是运维环境造成的状况而非请求
    非法；把它收成失败结果，调用方才拿得到可读原因。
    """
    (config_root / "plugins" / "Evil").symlink_to(
        config_root / "protected", target_is_directory=True
    )
    calls: list[tuple] = []

    result = _purge_command(calls).purge("Evil", PluginPurgeScope(data_directory=True))

    assert result.success is False
    assert "不在插件数据根" in result.message or "软链" in result.message
    assert (config_root / "protected" / "keep.txt").is_file()
