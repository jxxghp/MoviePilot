"""系统日志查看与下载接口的权限和打包行为测试。"""

import asyncio
import io
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.responses import Response

from app.api.endpoints import system as system_endpoint
from app.runtime.config import settings


def test_logging_routes_use_superuser_dependency():
    """日志查看和下载路由都必须绑定管理员依赖，避免普通登录用户读取敏感日志。"""
    routes = {route.path: route for route in system_endpoint.router.routes}

    logging_dependencies = {dependency.call for dependency in routes["/logging"].dependant.dependencies}
    download_dependencies = {dependency.call for dependency in routes["/logging/download/{name}"].dependant.dependencies}

    assert system_endpoint._verify_log_resource_superuser in logging_dependencies
    assert system_endpoint._verify_log_resource_superuser in download_dependencies


def test_log_resource_dependency_rejects_normal_user():
    """日志资源依赖必须拒绝非管理员 resource token。"""
    with pytest.raises(HTTPException) as exc_info:
        system_endpoint._verify_log_resource_superuser(
            SimpleNamespace(super_user=False),
        )

    assert exc_info.value.status_code == 403


@pytest.fixture(name="isolated_log_path")
def fixture_isolated_log_path(monkeypatch, tmp_path: Path) -> Path:
    """将日志目录隔离到临时目录，避免测试读取或打包真实运行日志。"""
    config_path = tmp_path / "config"
    log_path = config_path / "logs"
    log_path.mkdir(parents=True)
    monkeypatch.setattr(settings, "CONFIG_DIR", str(config_path))
    return log_path


def test_logging_requires_superuser_dependency(monkeypatch, isolated_log_path):
    """实时日志查看接口必须通过管理员依赖，普通资源令牌不能直接读取日志。"""
    (isolated_log_path / "moviepilot.log").write_text("hello\n", encoding="utf-8")
    response = asyncio.run(
        system_endpoint.get_logging(
            request=SimpleNamespace(is_disconnected=lambda: False),
            length=-1,
            logfile="moviepilot.log",
            _=SimpleNamespace(id=1, name="admin", is_superuser=True),
        )
    )

    assert isinstance(response, Response)


def test_download_moviepilot_logs_packages_latest_ten_log_files(isolated_log_path):
    """传入 moviepilot 时下载主程序滚动日志，最多打包 10 个文件。"""
    for index in range(12):
        (isolated_log_path / f"moviepilot.log.{index}").write_text(f"old-{index}", encoding="utf-8")
    (isolated_log_path / "moviepilot.log").write_text("current", encoding="utf-8")
    (isolated_log_path / "moviepilot.txt").write_text("ignored", encoding="utf-8")
    (isolated_log_path / "plugins").mkdir()
    (isolated_log_path / "plugins" / "demo.log").write_text("plugin", encoding="utf-8")

    response = asyncio.run(system_endpoint.download_logging(name="moviepilot", _=SimpleNamespace()))
    body = asyncio.run(_read_streaming_body(response))

    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        names = archive.namelist()

    moviepilot_zip_root = response.headers["Content-Disposition"].split('filename="', 1)[1].removesuffix('.zip"')
    assert response.media_type == "application/zip"
    assert 'filename="moviepilot-logs-' in response.headers["Content-Disposition"]
    assert "moviepilot-moviepilot-logs" not in response.headers["Content-Disposition"]
    assert len(names) == 10
    assert f"{moviepilot_zip_root}/moviepilot.log" in names
    assert "moviepilot.log" not in names
    assert "plugins/demo.log" not in names
    assert "moviepilot.txt" not in names


def test_download_logging_rejects_non_moviepilot_name(isolated_log_path):
    """`name` 只接受 "moviepilot"；插件日志改走实例专属下载端点，旧的单参数入口
    不再兼容以插件 ID 命名的扁平文件，避免中文实例名无法通过 ASCII 正则。
    """
    plugin_dir = isolated_log_path / "plugins"
    plugin_dir.mkdir()
    (plugin_dir / "demoplugin.log").write_text("current", encoding="utf-8")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(system_endpoint.download_logging(name="DemoPlugin", _=SimpleNamespace()))

    assert exc_info.value.status_code == 404


@pytest.fixture(name="plugin_instance_log_dir_resolver")
def fixture_plugin_instance_log_dir_resolver(monkeypatch, isolated_log_path):
    """把插件实例日志目录解析器指向隔离目录下的 plugins/<id>/<instance>/logs。

    直接替换 `app.runtime.log` 的私有解析器和目录缓存，借助 monkeypatch 在测试
    结束后自动还原，避免污染同一进程内的其它测试。
    """
    from app.runtime import log as log_module

    plugin_root = isolated_log_path.parent / "plugins"

    def resolver(plugin_id: str, instance_id: str) -> Path:
        return plugin_root / plugin_id / instance_id / "logs"

    monkeypatch.setattr(log_module, "_plugin_log_dir_resolver", resolver)
    monkeypatch.setattr(log_module, "_plugin_log_dir_cache", {})
    return resolver


def test_download_plugin_instance_logs_packages_instance_files_only(
    plugin_instance_log_dir_resolver,
):
    """按插件标识与实例标识下载时只打包该实例的滚动日志，最多 10 个文件。"""
    log_dir = plugin_instance_log_dir_resolver("DemoPlugin", "second")
    log_dir.mkdir(parents=True)
    for index in range(11):
        (log_dir / f"plugin.log.{index}").write_text(f"plugin-{index}", encoding="utf-8")
    (log_dir / "plugin.log").write_text("current", encoding="utf-8")
    other_instance_dir = plugin_instance_log_dir_resolver("DemoPlugin", "default")
    other_instance_dir.mkdir(parents=True)
    (other_instance_dir / "plugin.log").write_text("other instance", encoding="utf-8")

    response = asyncio.run(
        system_endpoint.download_plugin_instance_logging(
            plugin_id="DemoPlugin", instance_id="second", _=SimpleNamespace()
        )
    )
    body = asyncio.run(_read_streaming_body(response))

    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        names = archive.namelist()

    assert len(names) == 10
    assert any(name.endswith("/plugin.log") for name in names)
    assert not any("default" in name for name in names)


def test_download_plugin_instance_logs_supports_chinese_instance_id(
    plugin_instance_log_dir_resolver,
):
    """中文实例名的日志必须可以下载，不受下载端点字符集限制影响。"""
    instance_id = "中文实例"
    log_dir = plugin_instance_log_dir_resolver("DemoPlugin", instance_id)
    log_dir.mkdir(parents=True)
    (log_dir / "plugin.log").write_text("current", encoding="utf-8")

    response = asyncio.run(
        system_endpoint.download_plugin_instance_logging(
            plugin_id="DemoPlugin", instance_id=instance_id, _=SimpleNamespace()
        )
    )
    body = asyncio.run(_read_streaming_body(response))

    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        names = archive.namelist()

    assert any(name.endswith("/plugin.log") for name in names)
    assert "filename*=UTF-8''" in response.headers["Content-Disposition"]


def test_download_plugin_instance_logs_rejects_unknown_instance(
    plugin_instance_log_dir_resolver,
):
    """实例目录不存在时返回 404，而不是打包空文件。"""
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            system_endpoint.download_plugin_instance_logging(
                plugin_id="DemoPlugin", instance_id="ghost", _=SimpleNamespace()
            )
        )

    assert exc_info.value.status_code == 404


def test_get_logging_streams_plugin_instance_log(plugin_instance_log_dir_resolver):
    """流式日志接口给出 plugin_id 时改读该实例目录下的 plugin.log。"""
    log_dir = plugin_instance_log_dir_resolver("DemoPlugin", "second")
    log_dir.mkdir(parents=True)
    (log_dir / "plugin.log").write_text("instance log line\n", encoding="utf-8")

    response = asyncio.run(
        system_endpoint.get_logging(
            request=SimpleNamespace(is_disconnected=lambda: False),
            length=-1,
            plugin_id="DemoPlugin",
            instance_id="second",
            _=SimpleNamespace(id=1, name="admin", is_superuser=True),
        )
    )

    assert isinstance(response, Response)
    assert "instance log line" in response.body.decode("utf-8")


def test_download_log_zip_generation_runs_outside_event_loop_thread(monkeypatch, isolated_log_path):
    """日志压缩 I/O 必须离开事件循环线程执行，避免大日志下载阻塞其他请求。"""
    (isolated_log_path / "moviepilot.log").write_text("current", encoding="utf-8")
    event_loop_thread = threading.current_thread().name
    write_threads = []
    original_write = zipfile.ZipFile.write

    def capture_write_thread(self, filename, arcname=None, compress_type=None, compresslevel=None):
        """记录实际 zip 写入线程，并保持原始 ZipFile.write 行为。"""
        write_threads.append(threading.current_thread().name)
        return original_write(
            self,
            filename,
            arcname=arcname,
            compress_type=compress_type,
            compresslevel=compresslevel,
        )

    monkeypatch.setattr(zipfile.ZipFile, "write", capture_write_thread)

    response = asyncio.run(system_endpoint.download_logging(name="moviepilot", _=SimpleNamespace()))
    body = asyncio.run(_read_streaming_body(response))

    assert body
    assert write_threads
    assert all(thread_name != event_loop_thread for thread_name in write_threads)


async def _read_streaming_body(response) -> bytes:
    """读取 StreamingResponse 内容，便于断言 zip 文件条目。"""
    return b"".join([chunk async for chunk in response.body_iterator])
