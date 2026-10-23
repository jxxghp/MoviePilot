"""
验证插件发布技能的差异预览、同步和安全过滤。
"""

import importlib.util
import json
import sys
from argparse import Namespace
from pathlib import Path
from typing import Any


def load_publish_plugin_module() -> Any:
    """
    加载插件发布脚本模块。
    """
    script_path = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "publish-moviepilot-plugin"
        / "scripts"
        / "publish_plugin.py"
    )
    spec = importlib.util.spec_from_file_location("publish_plugin_skill", script_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_collect_local_files_excludes_secrets_and_keeps_dist(tmp_path: Path) -> None:
    """
    收集本地插件文件时应排除敏感文件并保留前端构建产物。
    """
    module = load_publish_plugin_module()
    plugin_dir = tmp_path / "plugins.v2" / "myplugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "__init__.py").write_text("class MyPlugin:\n    pass\n", encoding="utf-8")
    (plugin_dir / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (plugin_dir / "dist" / "assets").mkdir(parents=True)
    (plugin_dir / "dist" / "assets" / "remoteEntry.js").write_text(
        "export default {};\n",
        encoding="utf-8",
    )

    layout = module.Layout(package_file="package.v2.json", plugin_root="plugins.v2")
    files, rejected = module.collect_local_files(
        tmp_path,
        layout,
        "MyPlugin",
        list(module.DEFAULT_EXCLUDES),
        [],
    )

    assert "plugins.v2/myplugin/__init__.py" in files
    assert "plugins.v2/myplugin/dist/assets/remoteEntry.js" in files
    assert rejected == {"plugins.v2/myplugin/.env": ".env"}


def test_merge_package_content_preserves_other_plugins() -> None:
    """
    合并 package 文件时只更新目标插件条目。
    """
    module = load_publish_plugin_module()
    remote_content = json.dumps(
        {
            "OtherPlugin": {"name": "其他插件", "version": "1.0.0"},
            "MyPlugin": {"name": "旧插件", "version": "0.9.0"},
        },
        ensure_ascii=False,
    ).encode("utf-8")

    merged = module.merge_package_content(
        remote_content,
        "MyPlugin",
        {"name": "新插件", "version": "1.0.0"},
    )
    package_data = json.loads(merged.decode("utf-8"))

    assert package_data["OtherPlugin"] == {"name": "其他插件", "version": "1.0.0"}
    assert package_data["MyPlugin"] == {"name": "新插件", "version": "1.0.0"}


def test_push_creates_public_repo_when_explicitly_requested(tmp_path: Path) -> None:
    """
    显式允许自动建仓时，推送会默认创建公开仓库。
    """
    module = load_publish_plugin_module()
    plugin_dir = tmp_path / "plugins.v2" / "myplugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "__init__.py").write_text("class MyPlugin:\n    pass\n", encoding="utf-8")
    (tmp_path / "package.v2.json").write_text(
        json.dumps({"MyPlugin": {"name": "测试插件", "version": "1.0.0"}}, ensure_ascii=False),
        encoding="utf-8",
    )

    class FakeClient:
        """
        模拟缺失仓库的 GitHub 客户端。
        """

        def __init__(self) -> None:
            """
            初始化调用记录。
            """
            self.created_private = None
            self.commit_count = 0
            self.committed_files: list[str] = []
            self.committed_deletes: list[str] = []

        def repo_exists(self) -> bool:
            """
            返回仓库不存在。
            """
            return False

        def create_repo(self, private: bool = False) -> dict[str, Any]:
            """
            记录建仓可见性。
            """
            self.created_private = private
            return {"private": private, "html_url": "https://github.com/example/repo"}

        def commit_files(
            self,
            files: dict[str, bytes],
            deletes: list[str],
            message: str,
        ) -> str:
            """
            记录一次批量提交的文件集合。
            """
            self.commit_count += 1
            self.committed_files = sorted(files)
            self.committed_deletes = list(deletes)
            return "commit-sha"

    fake_client = FakeClient()

    def fake_build_context(args: Namespace, require_token: bool = False) -> dict[str, Any]:
        """
        构造无需访问网络的推送上下文。
        """
        layout = module.Layout(package_file="package.v2.json", plugin_root="plugins.v2")
        return {
            "repo": args.repo,
            "branch": args.branch,
            "local_repo": tmp_path,
            "layout": layout,
            "plugin_id": args.plugin_id,
            "remote_prefix": module.remote_plugin_prefix(layout, args.plugin_id),
            "client": fake_client,
            "excludes": list(module.DEFAULT_EXCLUDES),
            "includes": [],
        }

    original_build_context = module.build_context
    module.build_context = fake_build_context
    try:
        payload = module.push(
            Namespace(
                command="push",
                repo="example/repo",
                plugin_id="MyPlugin",
                local_repo=str(tmp_path),
                package_version="v2",
                branch="main",
                token="token",
                message="Publish MyPlugin",
                api_base=module.GITHUB_API_BASE,
                proxy="",
                timeout=module.DEFAULT_TIMEOUT,
                include=[],
                exclude=[],
                delete_remote=False,
                create_repo_if_missing=True,
                private=False,
                force=False,
                dry_run=False,
            )
        )
    finally:
        module.build_context = original_build_context

    assert payload["repo_existed"] is False
    assert fake_client.created_private is False
    assert fake_client.commit_count == 1
    assert "package.v2.json" in fake_client.committed_files
    assert "plugins.v2/myplugin/__init__.py" in fake_client.committed_files
    assert payload["commit_sha"] == "commit-sha"


def test_create_repo_dry_run_does_not_touch_github() -> None:
    """
    建仓 dry-run 不访问 GitHub，只返回计划。
    """
    module = load_publish_plugin_module()

    class FakeClient:
        """
        模拟不应被调用的 GitHub 客户端。
        """

        def repo_exists(self) -> bool:
            """
            如果被调用则说明 dry-run 行为错误。
            """
            raise AssertionError("dry-run should not query GitHub")

        def create_repo(self, private: bool = False) -> dict[str, Any]:
            """
            如果被调用则说明 dry-run 行为错误。
            """
            raise AssertionError("dry-run should not create GitHub repo")

    def fake_build_context(args: Namespace, require_token: bool = False) -> dict[str, Any]:
        """
        构造无需访问网络的建仓上下文。
        """
        return {
            "repo": args.repo,
            "branch": args.branch,
            "local_repo": Path.cwd(),
            "layout": module.Layout(package_file="package.v2.json", plugin_root="plugins.v2"),
            "plugin_id": "",
            "remote_prefix": "",
            "client": FakeClient(),
            "excludes": list(module.DEFAULT_EXCLUDES),
            "includes": [],
        }

    original_build_context = module.build_context
    module.build_context = fake_build_context
    try:
        payload = module.create_repo(
            Namespace(
                command="create-repo",
                repo="example/repo",
                plugin_id="",
                local_repo="",
                package_version="auto",
                branch="main",
                token="",
                message="",
                api_base=module.GITHUB_API_BASE,
                proxy="",
                timeout=module.DEFAULT_TIMEOUT,
                include=[],
                exclude=[],
                delete_remote=False,
                create_repo_if_missing=False,
                private=False,
                force=False,
                dry_run=True,
            )
        )
    finally:
        module.build_context = original_build_context

    assert payload["success"] is True
    assert payload["dry_run"] is True
    assert payload["private"] is False


def test_resolve_layout_supports_v3_repository(tmp_path: Path) -> None:
    """
    V3 仓库应被 auto 识别为 package.v3.json 与 plugins.v3。
    """
    module = load_publish_plugin_module()
    (tmp_path / "plugins.v3" / "myplugin").mkdir(parents=True)
    (tmp_path / "package.v3.json").write_text("{}", encoding="utf-8")

    layout = module.resolve_layout("auto", tmp_path, "MyPlugin")

    assert layout.package_file == "package.v3.json"
    assert layout.plugin_root == "plugins.v3"
    assert layout.version == "v3"


def test_resolve_layout_prefers_plugin_directory_over_manifest(tmp_path: Path) -> None:
    """
    同一仓库同时存在 v3 清单时，仍应按插件实际所在目录判定布局。
    """
    module = load_publish_plugin_module()
    (tmp_path / "plugins.v2" / "myplugin").mkdir(parents=True)
    (tmp_path / "package.v3.json").write_text("{}", encoding="utf-8")
    (tmp_path / "package.v2.json").write_text("{}", encoding="utf-8")

    layout = module.resolve_layout("auto", tmp_path, "MyPlugin")

    assert layout.package_file == "package.v2.json"
    assert layout.plugin_root == "plugins.v2"


def test_commit_files_creates_single_commit_for_all_changes() -> None:
    """
    多文件变更应通过 Git Data API 合并为一个提交。
    """
    module = load_publish_plugin_module()
    client = module.GitHubClient(repo="example/repo")
    calls: list[tuple[str, str, Any]] = []
    blob_index = {"value": 0}

    def fake_request(method: str, path: str, payload: Any = None, accept: str = "") -> Any:
        """
        模拟 GitHub API 响应并记录调用。
        """
        del accept
        calls.append((method, path, payload))
        if method == "GET" and "/branches/" in path:
            return {"commit": {"sha": "parent-1", "commit": {"tree": {"sha": "base-tree"}}}}
        if path.endswith("/git/blobs"):
            blob_index["value"] += 1
            return {"sha": f"blob-{blob_index['value']}"}
        if path.endswith("/git/trees"):
            return {"sha": "new-tree"}
        if path.endswith("/git/commits"):
            return {"sha": "new-commit"}
        if "/git/refs/heads/" in path:
            return {"ok": True}
        raise AssertionError(f"未预期的请求: {method} {path}")

    client.request = fake_request  # type: ignore[method-assign]
    commit_sha = client.commit_files(
        {"package.v3.json": b"{}", "plugins.v3/demo/__init__.py": b""},
        ["plugins.v3/demo/old.py"],
        "Publish Demo",
    )

    assert commit_sha == "new-commit"
    tree_calls = [payload for method, path, payload in calls if path.endswith("/git/trees")]
    assert len(tree_calls) == 1
    entries = {item["path"]: item for item in tree_calls[0]["tree"]}
    assert entries["plugins.v3/demo/old.py"]["sha"] is None
    assert tree_calls[0]["base_tree"] == "base-tree"
    commit_calls = [payload for method, path, payload in calls if path.endswith("/git/commits")]
    assert len(commit_calls) == 1
    assert commit_calls[0]["parents"] == ["parent-1"]
    ref_calls = [(method, path) for method, path, _ in calls if "/git/refs/heads/" in path]
    assert ref_calls == [("PATCH", "/repos/example/repo/git/refs/heads/main")]
    assert blob_index["value"] == 2


def test_commit_files_creates_branch_in_empty_repo() -> None:
    """
    空仓库没有分支时，应创建根提交并创建分支引用。
    """
    module = load_publish_plugin_module()
    client = module.GitHubClient(repo="example/repo")
    calls: list[tuple[str, str, Any]] = []

    def fake_request(method: str, path: str, payload: Any = None, accept: str = "") -> Any:
        """
        模拟空仓库响应。
        """
        del accept
        calls.append((method, path, payload))
        if method == "GET" and "/branches/" in path:
            raise module.GitHubError(404, "Branch not found")
        if path.endswith("/git/blobs"):
            return {"sha": "blob-1"}
        if path.endswith("/git/trees"):
            return {"sha": "new-tree"}
        if path.endswith("/git/commits"):
            return {"sha": "root-commit"}
        if path.endswith("/git/refs"):
            return {"ref": "refs/heads/main"}
        raise AssertionError(f"未预期的请求: {method} {path}")

    client.request = fake_request  # type: ignore[method-assign]
    commit_sha = client.commit_files({"package.v3.json": b"{}"}, [], "Init")

    assert commit_sha == "root-commit"
    commit_calls = [payload for method, path, payload in calls if path.endswith("/git/commits")]
    assert commit_calls[0]["parents"] == []
    tree_calls = [payload for method, path, payload in calls if path.endswith("/git/trees")]
    assert "base_tree" not in tree_calls[0]
    assert any(method == "POST" and path.endswith("/git/refs") for method, path, _ in calls)
