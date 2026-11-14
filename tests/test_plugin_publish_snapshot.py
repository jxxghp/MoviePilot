"""
验证插件发布读取固定远端快照及新建仓、并发提交的保护。
"""



import base64
import importlib.util
import json
import sys
from argparse import Namespace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest


@pytest.fixture
def publisher(monkeypatch):
    """
    加载真实发布脚本，并由夹具恢复动态模块登记。
    """
    script = Path(__file__).resolve().parents[1] / "skills/publish-moviepilot-plugin/scripts/publish_plugin.py"
    spec = importlib.util.spec_from_file_location("review_publisher", script)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("created", [False, True])
@pytest.mark.parametrize("concurrent", [False, True])
def test_publish_uses_one_snapshot_and_preserves_concurrent_package(tmp_path, monkeypatch, publisher, concurrent, created):
    """
    完整发布入口须以同一读取快照建树；并发推进时拒绝覆盖其他作者更新。
    """
    module = publisher
    packages = {"head-0": {"OtherPlugin": {"version": "1.0.0"}},
                "head-1": {"OtherPlugin": {"version": "2.0.0"}}}
    state = {"head": "head-0", "blobs": {}, "tree": {}, "commit": {}, "read_refs": [], "exists": not created}
    plugin_path = "plugins.v3/myplugin/__init__.py"
    remote_plugin = b"old plugin\n"

    class BoundaryClient(module.GitHubClient):
        """
        只替换 GitHub I/O，保留真实文件读取、合并和 Git Data 请求构造。
        """

        def __init__(self):
            """
            配置隔离仓库身份，不初始化真实网络客户端。
            """
            self.repo = "fixture/repo"
            self.branch = "main"

        def repo_exists(self):
            """
            创建前返回缺失，创建后返回存在。
            """
            return state["exists"]

        def request(self, method, path, payload=None, accept=""):
            """
            在清单读取完成时推进分支，并按 Git 非强推语义拒绝旧父提交。
            """
            del accept
            if method == "GET" and path == "/user":
                return {"login": "fixture"}
            if method == "POST" and path == "/user/repos":
                assert payload["auto_init"] is True
                # 自动初始化创建主分支；其后首次读取前，另一个作者也可提交插件清单。
                state["exists"] = True
                return {"default_branch": "main"}
            if method == "POST" and path.endswith("/git/refs"):
                raise module.GitHubError(422, "Reference already exists")
            if method == "GET" and "/branches/" in path:
                return {"commit": {"sha": state["head"], "commit": {"tree": {"sha": "tree-" + state["head"]}}}}
            if method == "GET" and "/git/trees/" in path:
                ref = path.split("/git/trees/")[1].split("?")[0]
                state["read_refs"].append(ref)
                return {"tree": [{"path": plugin_path, "type": "blob", "sha": "plugin-old"}]}
            if method == "GET" and "/contents/" in path:
                url = urlsplit(path)
                ref = parse_qs(url.query)["ref"][0]
                ref = state["head"] if ref == "main" else ref
                state["read_refs"].append(ref)
                content_path = url.path.split("/contents/")[1]
                if content_path == "package.v3.json":
                    content = json.dumps(packages[ref]).encode()
                    if concurrent:
                        state["head"] = "head-1"
                else:
                    assert content_path == plugin_path
                    content = remote_plugin
                return {"type": "file", "sha": "old", "content": base64.b64encode(content).decode()}
            if method == "POST" and path.endswith("/git/blobs"):
                sha = f"blob-{len(state['blobs'])}"
                state["blobs"][sha] = base64.b64decode(payload["content"])
                return {"sha": sha}
            if method == "POST" and path.endswith("/git/trees"):
                state["tree"] = payload
                return {"sha": "new-tree"}
            if method == "POST" and path.endswith("/git/commits"):
                state["commit"] = payload
                return {"sha": "new-head"}
            if method == "PATCH" and "/git/refs/heads/" in path:
                assert payload["force"] is False
                if state["commit"]["parents"] != [state["head"]]:
                    raise module.GitHubError(422, "Update is not a fast forward")
                for entry in state["tree"]["tree"]:
                    if entry["path"] == "package.v3.json":
                        packages["new-head"] = json.loads(state["blobs"][entry["sha"]])
                state["head"] = "new-head"
                return {}
            raise AssertionError((method, path))

    (tmp_path / "plugins.v3/myplugin").mkdir(parents=True)
    (tmp_path / plugin_path).write_bytes(b"new plugin\n")
    (tmp_path / "package.v3.json").write_text(json.dumps({"MyPlugin": {"version": "1.1.0"}}))
    client = BoundaryClient()
    context = {"repo": client.repo, "branch": client.branch, "plugin_id": "MyPlugin", "local_repo": tmp_path,
               "layout": module.Layout("package.v3.json", "plugins.v3"), "remote_prefix": "plugins.v3/myplugin",
               "client": client, "excludes": list(module.DEFAULT_EXCLUDES), "includes": []}
    monkeypatch.setattr(module, "build_context", lambda *_args, **_kwargs: context)
    args = Namespace(dry_run=False, delete_remote=False, create_repo_if_missing=created, private=False, message="Publish")
    if concurrent:
        with pytest.raises(module.GitHubError, match="fast forward"):
            module.push(args)
        assert state["head"] == "head-1"
        assert packages[state["head"]]["OtherPlugin"]["version"] == "2.0.0"
    else:
        result = module.push(args)
        assert result["commit_sha"] == "new-head"
        assert packages["new-head"] == {"OtherPlugin": {"version": "1.0.0"}, "MyPlugin": {"version": "1.1.0"}}
    assert state["read_refs"] == ["head-0", "head-0", "head-0"]
    assert state["tree"]["base_tree"] == "tree-head-0"
    assert state["commit"]["parents"] == ["head-0"]
