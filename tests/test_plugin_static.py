"""
验证插件静态资源的浏览器缓存、条件请求和访问安全边界。
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, HTTPException
from starlette.testclient import TestClient

from app.api.endpoints import plugin


@pytest.fixture
def static_files(tmp_path, monkeypatch):
    """
    在临时插件目录与独立路由中提供资源，保留真实访问校验依赖。
    """
    root = tmp_path / "app/plugins/demo/dist"
    root.mkdir(parents=True)
    (root / "remoteEntry.js").write_text("export const version = 1", encoding="utf-8")
    (root / "chunk.js").write_text("export const value = 1", encoding="utf-8")
    manager = Mock()
    manager.get_plugin_source_id.return_value = "Demo"
    manager.get_plugin_ids.return_value = ["Demo"]
    manager.get_plugin_attr.return_value = "1.0.0"
    manager.get_plugin_auth_providers.return_value = []
    verify = Mock()
    monkeypatch.setattr(plugin, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(plugin, "verify_resource_token", verify)
    monkeypatch.setattr(plugin, "get_api_runtime_config_snapshot", lambda: SimpleNamespace(root_path=tmp_path))
    app = FastAPI()
    app.include_router(plugin.router, prefix="/plugin")
    return TestClient(app), root, manager, verify


def test_current_version_entry_is_private_and_cacheable(static_files):
    """
    只有当前版本参数可授予长期私有缓存，且返回文件校验头。
    """
    client, _, manager, _ = static_files
    response = client.get("/plugin/file/demo/dist/remoteEntry.js?v=1.0.0")
    assert response.status_code == 200
    assert response.content == b"export const version = 1"
    assert response.headers["content-type"].startswith("application/javascript")
    assert response.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert response.headers["etag"]
    assert response.headers["last-modified"]
    manager.get_plugin_attr.assert_called_once_with("Demo", "plugin_version")
    manager.get_plugin_attr.return_value = "2.0.0"
    assert client.get("/plugin/file/Demo/dist/remoteEntry.js?v=1.0.0").headers["cache-control"] == "private, no-cache"
    assert "immutable" in client.get("/plugin/file/Demo/dist/remoteEntry.js?v=2.0.0").headers["cache-control"]


@pytest.mark.parametrize("validator", ["etag", "last-modified"])
def test_unversioned_chunks_revalidate_without_resending_body(static_files, validator):
    """
    无版本 chunk 保留缓存内容，条件命中时仍校验权限并返回无响应体的 304。
    """
    client, _, _, verify = static_files
    url = "/plugin/file/Demo/dist/chunk.js"
    first = client.get(url)
    header = "If-None-Match" if validator == "etag" else "If-Modified-Since"
    response = client.get(url, headers={header: first.headers[validator]})
    assert response.status_code == 304
    assert response.content == b""
    assert response.headers["cache-control"] == "private, no-cache"
    assert response.headers[validator] == first.headers[validator]
    assert verify.call_count == 2
    verify.side_effect = HTTPException(status_code=403, detail="Forbidden")
    assert client.get(url, headers={header: first.headers[validator]}).status_code == 403


def test_changed_file_and_nonmatching_etag_return_new_body(static_files):
    """
    文件改变或 ETag 不匹配时，不被旧修改时间错误判为未修改。
    """
    client, root, _, _ = static_files
    url = "/plugin/file/Demo/dist/chunk.js"
    first = client.get(url)
    (root / "chunk.js").write_text("export const value = 'updated'", encoding="utf-8")
    response = client.get(url, headers={
        "If-None-Match": first.headers["etag"],
        "If-Modified-Since": "Wed, 01 Jan 2099 00:00:00 GMT",
    })
    assert response.status_code == 200
    assert response.headers["etag"] != first.headers["etag"]
    assert response.content == b"export const value = 'updated'"


def test_missing_and_escaped_files_are_not_cacheable(static_files, tmp_path):
    """
    缺失资源与指向插件外的符号链接保持原有拒绝语义。
    """
    client, root, _, _ = static_files
    outside = tmp_path / "outside.js"
    outside.write_text("private", encoding="utf-8")
    (root / "escaped.js").symlink_to(outside)
    assert client.get("/plugin/file/Demo/dist/escaped.js").status_code == 403
    assert client.get("/plugin/file/Demo/dist/missing.js?v=1.0.0").status_code == 404
