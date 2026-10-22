"""
独立验证插件拉取预检、SQL词法和前端发布失败时的数据边界。
"""

import json
from argparse import Namespace
from types import SimpleNamespace

import pytest

from tests.test_dev_skills_review_regressions import _load_script


def _pull_context(tmp_path, monkeypatch):
    """
    准备真实临时插件目录，仅替换远端读取边界。
    """
    module = _load_script("skills/publish-moviepilot-plugin/scripts/publish_plugin.py", monkeypatch)
    directory = tmp_path / "plugins.v2/myplugin"
    directory.mkdir(parents=True)
    (directory / "__init__.py").write_text("# 旧本地源码\n")
    package = tmp_path / "package.v2.json"
    prefix = "plugins.v2/myplugin"
    remote = {f"{prefix}/__init__.py": module.FileState(f"{prefix}/__init__.py", (directory / "__init__.py").read_bytes()),
              f"{prefix}/new.py": module.FileState(f"{prefix}/new.py", b"# new")}
    context = {"repo": "fixture/review", "branch": "v3", "plugin_id": "MyPlugin", "local_repo": tmp_path,
        "layout": module.Layout("package.v2.json", "plugins.v2"), "remote_prefix": prefix,
        "client": SimpleNamespace(list_files=lambda _prefix: remote, get_file=lambda path: module.FileState(
            path, json.dumps({"MyPlugin": {"version": "1.0.0"}}).encode())),
        "excludes": list(module.DEFAULT_EXCLUDES), "includes": []}
    monkeypatch.setattr(module, "build_context", lambda _args: context)
    return module, directory, package, context


@pytest.mark.parametrize("package_content", ["{broken", "[]"])
@pytest.mark.parametrize("force", [False, True])
def test_pull_invalid_local_package_never_partially_writes_plugin(tmp_path, monkeypatch, package_content, force):
    """
    本地package无效必须在任何文件写入前拒绝，强制模式也不能先创建文件再失败。
    """
    module, directory, package, context = _pull_context(tmp_path, monkeypatch)
    package.write_text(package_content)
    try:
        result = module.pull(Namespace(force=force, dry_run=False))
    except ValueError:
        pass
    else:
        assert context["layout"].package_file in result["changes"]["conflicts"]
    assert not (directory / "new.py").exists()
    assert package.read_text() == package_content


@pytest.mark.parametrize("existing_package", [False, True])
def test_pull_valid_missing_entry_preserves_other_plugin_metadata(tmp_path, monkeypatch, existing_package):
    """
    缺少本地package或目标条目仍可正常拉取，新条目不能覆盖其它插件的元数据。
    """
    module, directory, package, _ = _pull_context(tmp_path, monkeypatch)
    other = {"Other": {"version": "3.0.0", "name": "其它插件"}}
    if existing_package:
        package.write_text(json.dumps(other))
    result = module.pull(Namespace(force=False, dry_run=False))
    assert not result["changes"]["conflicts"]
    assert (directory / "new.py").read_bytes() == b"# new"
    expected = {**(other if existing_package else {}), "MyPlugin": {"version": "1.0.0"}}
    assert json.loads(package.read_text()) == expected
