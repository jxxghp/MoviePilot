"""系统日志查看与下载接口的权限和打包行为测试。"""

import asyncio
import io
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.responses import Response

from app.api.endpoints import system as system_endpoint
from app.core.config import settings


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


def test_download_plugin_logs_packages_plugin_files_only(isolated_log_path):
    """传入插件 ID 时只下载该插件滚动日志，最多打包 10 个文件。"""
    plugin_dir = isolated_log_path / "plugins"
    plugin_dir.mkdir()
    for index in range(11):
        (plugin_dir / f"demoplugin.log.{index}").write_text(f"plugin-{index}", encoding="utf-8")
    (plugin_dir / "demoplugin.log").write_text("current", encoding="utf-8")
    (plugin_dir / "other.log").write_text("other", encoding="utf-8")
    (isolated_log_path / "moviepilot.log").write_text("main", encoding="utf-8")

    response = asyncio.run(system_endpoint.download_logging(name="DemoPlugin", _=SimpleNamespace()))
    body = asyncio.run(_read_streaming_body(response))

    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        names = archive.namelist()

    plugin_zip_root = response.headers["Content-Disposition"].split('filename="', 1)[1].removesuffix('.zip"')
    assert len(names) == 10
    assert f"{plugin_zip_root}/demoplugin.log" in names
    assert "demoplugin.log" not in names
    assert "plugins/demoplugin.log" not in names
    assert "plugins/other.log" not in names
    assert "moviepilot.log" not in names


async def _read_streaming_body(response) -> bytes:
    """读取 StreamingResponse 内容，便于断言 zip 文件条目。"""
    return b"".join([chunk async for chunk in response.body_iterator])
