#!/usr/bin/env python3
"""发布和同步 MoviePilot 本地插件到 GitHub 仓库。"""

from __future__ import annotations

import argparse
import base64
import fnmatch
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Optional


DEFAULT_BRANCH = "main"
DEFAULT_TIMEOUT = 60
GITHUB_API_BASE = "https://api.github.com"
USER_AGENT = "MoviePilot-Plugin-Publisher"
COMMANDS_REQUIRE_LOCAL_PLUGIN = {"preview", "push", "pull"}
PACKAGE_BY_VERSION = {
    "legacy": ("package.json", "plugins"),
    "v1": ("package.json", "plugins"),
    "v2": ("package.v2.json", "plugins.v2"),
}
TOKEN_ENV_NAMES = ("MOVIEPILOT_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")
DEFAULT_EXCLUDES = (
    ".git/",
    ".idea/",
    ".vscode/",
    ".env",
    ".env.*",
    ".DS_Store",
    "__pycache__/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    "node_modules/",
    "config/",
    "data/",
    "cache/",
    "logs/",
    "tmp/",
    "*.pyc",
    "*.pyo",
    "*.db",
    "*.sqlite",
    "*.sqlite3",
    "*.log",
    "*.bak",
    "*.tmp",
    "*.secret",
    "*.key",
    "*.pem",
    "*.crt",
    "*.p12",
    "*.pfx",
)


@dataclass
class FileState:
    """表示一个本地或远端文件的内容状态。"""

    path: str
    content: bytes
    sha: Optional[str] = None

    @property
    def sha256(self) -> str:
        """返回文件内容的 SHA256 摘要。"""
        return hashlib.sha256(self.content).hexdigest()


@dataclass
class ChangeSet:
    """保存一次插件同步对比得到的差异集合。"""

    create: list[str] = field(default_factory=list)
    update: list[str] = field(default_factory=list)
    delete: list[str] = field(default_factory=list)
    same: list[str] = field(default_factory=list)
    rejected: dict[str, str] = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转换为可序列化的字典。"""
        return {
            "create": sorted(self.create),
            "update": sorted(self.update),
            "delete": sorted(self.delete),
            "same": sorted(self.same),
            "rejected": dict(sorted(self.rejected.items())),
            "conflicts": sorted(self.conflicts),
            "summary": {
                "create": len(self.create),
                "update": len(self.update),
                "delete": len(self.delete),
                "same": len(self.same),
                "rejected": len(self.rejected),
                "conflicts": len(self.conflicts),
            },
        }


@dataclass
class Layout:
    """描述 MoviePilot 插件仓库的 package 与插件目录布局。"""

    package_file: str
    plugin_root: str

    @property
    def version(self) -> str:
        """返回布局版本标识。"""
        if self.plugin_root == "plugins.v2":
            return "v2"
        return "legacy"


class GitHubError(RuntimeError):
    """GitHub API 调用失败。"""

    def __init__(self, status: int, message: str):
        """初始化 GitHub 错误。"""
        super().__init__(message)
        self.status = status
        self.message = message


class GitHubClient:
    """基于 GitHub Contents API 的轻量客户端。"""

    def __init__(
        self,
        repo: str,
        token: str = "",
        branch: str = DEFAULT_BRANCH,
        api_base: str = GITHUB_API_BASE,
        timeout: int = DEFAULT_TIMEOUT,
        proxy: str = "",
    ) -> None:
        """初始化客户端配置。"""
        self.repo = normalize_repo(repo)
        self.branch = branch
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self.token = token
        self.proxy = proxy
        self.request_utils_class = load_request_utils_class()
        self.proxies = build_proxy_config(proxy)

    def request(
        self,
        method: str,
        path: str,
        payload: Optional[dict[str, Any]] = None,
        accept: str = "application/vnd.github+json",
    ) -> Any:
        """
        发送 GitHub API 请求并返回解析后的 JSON。

        :param method: HTTP 方法
        :param path: API 路径
        :param payload: JSON 请求体
        :param accept: Accept 请求头
        :return: 解析后的响应 JSON
        """
        url = f"{self.api_base}{path}"
        body = None
        headers = {
            "Accept": accept,
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        response = None
        try:
            response = self.request_utils_class(
                proxies=self.proxies,
                headers=headers,
                timeout=self.timeout,
            ).request(
                method=method.lower(),
                url=url,
                data=body,
                raise_exception=True,
            )
            if response is None:
                raise GitHubError(0, "GitHub API 未返回响应")
            if response.status_code >= 400:
                raise GitHubError(response.status_code, parse_github_error(response.text))
            raw = response.content
        except Exception as err:
            if isinstance(err, GitHubError):
                raise
            status = getattr(getattr(err, "response", None), "status_code", 0)
            raw_error = getattr(getattr(err, "response", None), "text", "")
            message = parse_github_error(raw_error) if raw_error else str(err)
            raise GitHubError(status, message) from err
        finally:
            if response is not None:
                response.close()

        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return raw.decode("utf-8", errors="replace")

    def get_file(self, path: str) -> Optional[FileState]:
        """
        读取远端单个文件，文件不存在时返回 None。

        :param path: 仓库内 POSIX 路径
        :return: 文件状态或 None
        """
        encoded_path = quote_path(path)
        try:
            data = self.request(
                "GET",
                f"/repos/{self.repo}/contents/{encoded_path}?ref={quote(self.branch)}",
            )
        except GitHubError as err:
            if err.status == 404:
                return None
            raise
        if not isinstance(data, dict) or data.get("type") != "file":
            return None
        raw_content = str(data.get("content") or "")
        content = base64.b64decode(raw_content.encode("utf-8"), validate=False)
        return FileState(path=path, content=content, sha=data.get("sha"))

    def list_files(self, prefix: str) -> dict[str, FileState]:
        """
        递归列出远端路径下的全部文件。

        :param prefix: 仓库内目录路径
        :return: 以仓库路径为键的远端文件状态
        """
        normalized_prefix = normalize_remote_path(prefix)
        if not normalized_prefix:
            return {}
        tree = self.get_tree()
        files: dict[str, FileState] = {}
        prefix_with_slash = f"{normalized_prefix}/"
        for item in tree:
            path = item.get("path")
            if item.get("type") != "blob" or not isinstance(path, str):
                continue
            if path == normalized_prefix or path.startswith(prefix_with_slash):
                files[path] = FileState(path=path, content=b"", sha=item.get("sha"))
        for path in list(files):
            file_state = self.get_file(path)
            if file_state is not None:
                files[path] = file_state
        return files

    def get_tree(self) -> list[dict[str, Any]]:
        """
        读取目标分支的递归 Git tree。

        :return: Git tree 条目列表
        """
        branch_data = self.request(
            "GET",
            f"/repos/{self.repo}/branches/{quote(self.branch)}",
        )
        commit_sha = branch_data.get("commit", {}).get("sha")
        if not commit_sha:
            raise GitHubError(0, f"无法读取分支 {self.branch} 的提交 SHA")
        tree_data = self.request(
            "GET",
            f"/repos/{self.repo}/git/trees/{commit_sha}?recursive=1",
        )
        tree = tree_data.get("tree") if isinstance(tree_data, dict) else None
        if not isinstance(tree, list):
            return []
        return tree

    def put_file(self, path: str, content: bytes, message: str, sha: Optional[str]) -> Any:
        """
        创建或更新远端文件。

        :param path: 仓库内 POSIX 路径
        :param content: 文件内容
        :param message: 提交消息
        :param sha: 远端现有文件 SHA，创建时为 None
        :return: GitHub API 响应
        """
        payload: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content).decode("ascii"),
            "branch": self.branch,
        }
        if sha:
            payload["sha"] = sha
        return self.request(
            "PUT",
            f"/repos/{self.repo}/contents/{quote_path(path)}",
            payload=payload,
        )

    def delete_file(self, path: str, message: str, sha: str) -> Any:
        """
        删除远端文件。

        :param path: 仓库内 POSIX 路径
        :param message: 提交消息
        :param sha: 远端文件 SHA
        :return: GitHub API 响应
        """
        payload = {
            "message": message,
            "sha": sha,
            "branch": self.branch,
        }
        return self.request(
            "DELETE",
            f"/repos/{self.repo}/contents/{quote_path(path)}",
            payload=payload,
        )

    def repo_exists(self) -> bool:
        """
        检查目标仓库是否存在或当前 token 是否可访问。

        :return: 仓库存在或可访问时返回 True
        """
        try:
            self.request("GET", f"/repos/{self.repo}")
            return True
        except GitHubError as err:
            if err.status == 404:
                return False
            raise

    def create_repo(self, private: bool = False) -> Any:
        """
        创建目标 GitHub 仓库。

        :param private: 是否创建私有仓库，默认创建公开仓库
        :return: GitHub API 响应
        """
        owner, repo_name = self.repo.split("/", 1)
        payload = {
            "name": repo_name,
            "private": private,
            "auto_init": True,
        }
        user_data = self.request("GET", "/user")
        login = user_data.get("login") if isinstance(user_data, dict) else ""
        if login and str(login).lower() == owner.lower():
            return self.request("POST", "/user/repos", payload=payload)
        return self.request("POST", f"/orgs/{quote(owner)}/repos", payload=payload)


def normalize_repo(repo: str) -> str:
    """
    规范化 GitHub 仓库名称。

    :param repo: owner/repo 或 GitHub URL
    :return: owner/repo
    """
    value = repo.strip().removesuffix(".git")
    if value.startswith("http://") or value.startswith("https://"):
        parsed = urllib.parse.urlparse(value)
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if len(parts) >= 2:
            value = f"{parts[0]}/{parts[1]}"
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", value):
        raise ValueError("GitHub 仓库必须是 owner/repo 或 https://github.com/owner/repo")
    return value


def quote(value: str) -> str:
    """
    编码 URL 查询参数。

    :param value: 待编码文本
    :return: URL 编码结果
    """
    return urllib.parse.quote(value, safe="")


def quote_path(path: str) -> str:
    """
    编码 GitHub Contents API 路径。

    :param path: 仓库内 POSIX 路径
    :return: URL 路径编码结果
    """
    return "/".join(urllib.parse.quote(part, safe="") for part in path.split("/"))


def normalize_remote_path(path: str) -> str:
    """
    规范化远端仓库内路径。

    :param path: 原始路径
    :return: 去掉首尾斜杠后的 POSIX 路径
    """
    return str(PurePosixPath(path.strip("/"))) if path.strip("/") else ""


def build_proxy_config(proxy: str = "") -> Optional[dict[str, str]]:
    """
    构建 RequestUtils 代理配置。

    :param proxy: 代理地址
    :return: requests 代理配置
    """
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def load_request_utils_class() -> type:
    """
    加载 MoviePilot RequestUtils，仓库外运行时返回兼容实现。

    :return: RequestUtils 类
    """
    try:
        root = Path(__file__).resolve().parents[3]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from app.adapters.network.http import RequestUtils  # pylint: disable=import-outside-toplevel

        return RequestUtils
    except Exception:
        return FallbackRequestUtils


class FallbackResponse:
    """提供与 requests.Response 相近的最小响应接口。"""

    def __init__(self, status_code: int, content: bytes):
        """初始化响应内容。"""
        self.status_code = status_code
        self.content = content
        self.text = content.decode("utf-8", errors="replace")

    def close(self) -> None:
        """关闭响应，兼容 requests.Response 接口。"""
        return None


class FallbackRequestUtils:
    """仓库外运行时使用的最小 HTTP 客户端。"""

    def __init__(
        self,
        headers: Optional[dict[str, str]] = None,
        proxies: Optional[dict[str, str]] = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        """初始化请求配置。"""
        self.headers = headers or {}
        self.proxies = proxies or {}
        self.timeout = timeout

    def request(
        self,
        method: str,
        url: str,
        data: Optional[bytes] = None,
        raise_exception: bool = False,
        **_: Any,
    ) -> Optional[FallbackResponse]:
        """
        发送 HTTP 请求。

        :param method: HTTP 方法
        :param url: 请求地址
        :param data: 请求体
        :param raise_exception: 是否抛出请求异常
        :return: 响应对象或 None
        """
        import urllib.request  # pylint: disable=import-outside-toplevel

        handlers = []
        if self.proxies:
            handlers.append(urllib.request.ProxyHandler(self.proxies))
        opener = urllib.request.build_opener(*handlers)
        request = urllib.request.Request(
            url,
            data=data,
            headers=self.headers,
            method=method.upper(),
        )
        try:
            with opener.open(request, timeout=self.timeout) as response:
                return FallbackResponse(response.status, response.read())
        except urllib.error.HTTPError as err:
            content = err.read()
            if raise_exception:
                err.response = FallbackResponse(err.code, content)
                raise
            return FallbackResponse(err.code, content)
        except urllib.error.URLError:
            if raise_exception:
                raise
            return None


def parse_github_error(raw: str) -> str:
    """
    从 GitHub 错误响应中提取可读消息。

    :param raw: 原始响应体
    :return: 错误消息
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(data, dict):
        return str(data.get("message") or raw)
    return raw


def resolve_token(repo: str, explicit_token: str = "") -> str:
    """
    解析 GitHub token，优先使用显式参数、仓库专属 token、环境变量与 MoviePilot settings。

    :param repo: owner/repo
    :param explicit_token: 命令行传入的 token
    :return: token 字符串
    """
    if explicit_token:
        return explicit_token
    settings = load_moviepilot_settings()
    repo_token = repo_token_from_settings(settings, repo)
    if repo_token:
        return repo_token
    settings_token = str(getattr(settings, "GITHUB_TOKEN", "") or "").strip() if settings else ""
    if settings_token:
        return settings_token
    for name in TOKEN_ENV_NAMES:
        token = os.environ.get(name, "").strip()
        if token:
            return token
    return ""


def repo_token_from_settings(settings: Any, repo: str) -> str:
    """
    从 MoviePilot settings.REPO_GITHUB_TOKEN 中读取仓库专属 token。

    :param settings: MoviePilot settings 对象
    :param repo: owner/repo
    :return: token 字符串
    """
    if not settings:
        return ""
    raw = str(getattr(settings, "REPO_GITHUB_TOKEN", "") or "")
    repo_lower = repo.lower()
    for item in raw.split(","):
        repo_part, sep, token = item.partition(":")
        if sep and repo_part.strip().lower() == repo_lower and token.strip():
            return token.strip()
    return ""


def load_moviepilot_settings() -> Any:
    """
    尝试加载 MoviePilot settings，脚本脱离项目运行时返回 None。

    :return: settings 对象或 None
    """
    try:
        root = Path(__file__).resolve().parents[3]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from app.runtime.config import settings  # pylint: disable=import-outside-toplevel

        return settings
    except Exception:
        return None


def resolve_proxy(explicit_proxy: str = "") -> str:
    """
    解析 GitHub 请求代理地址。

    :param explicit_proxy: 命令行传入代理
    :return: 代理地址
    """
    if explicit_proxy:
        return explicit_proxy
    settings = load_moviepilot_settings()
    if settings:
        proxy_host = str(getattr(settings, "PROXY_HOST", "") or "").strip()
        if proxy_host:
            return proxy_host
    return ""


def resolve_local_repo(local_repo: str, plugin_id: str, package_version: str) -> Path:
    """
    解析本地插件仓库目录。

    :param local_repo: 显式本地仓库路径
    :param plugin_id: 插件 ID
    :param package_version: 插件布局版本
    :return: 本地仓库目录
    """
    if local_repo:
        return Path(local_repo).expanduser().resolve()
    settings = load_moviepilot_settings()
    raw_paths = str(getattr(settings, "PLUGIN_LOCAL_REPO_PATHS", "") or "") if settings else ""
    layout = resolve_layout(package_version, Path.cwd(), plugin_id)
    matches: list[Path] = []
    for item in raw_paths.split(","):
        if not item.strip():
            continue
        path = Path(item.strip()).expanduser()
        if not path.is_absolute() and settings:
            path = Path(getattr(settings, "ROOT_PATH")) / path
        path = path.resolve()
        if plugin_dir(path, layout, plugin_id).exists():
            matches.append(path)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        joined = ", ".join(str(path) for path in matches)
        raise ValueError(f"多个本地插件仓库包含 {plugin_id}：{joined}，请使用 --local-repo 指定")
    raise ValueError("未找到本地插件仓库，请使用 --local-repo 指定")


def resolve_layout(package_version: str, local_repo: Path, plugin_id: str) -> Layout:
    """
    根据参数和本地文件推断插件仓库布局。

    :param package_version: v2、legacy 或 auto
    :param local_repo: 本地仓库目录
    :param plugin_id: 插件 ID
    :return: 布局描述
    """
    normalized = package_version.strip().lower()
    if normalized in PACKAGE_BY_VERSION:
        package_file, plugin_root = PACKAGE_BY_VERSION[normalized]
        return Layout(package_file=package_file, plugin_root=plugin_root)
    v2_dir = local_repo / "plugins.v2" / plugin_id.lower()
    legacy_dir = local_repo / "plugins" / plugin_id.lower()
    if (local_repo / "package.v2.json").exists() or v2_dir.exists():
        return Layout(package_file="package.v2.json", plugin_root="plugins.v2")
    if (local_repo / "package.json").exists() or legacy_dir.exists():
        return Layout(package_file="package.json", plugin_root="plugins")
    return Layout(package_file="package.v2.json", plugin_root="plugins.v2")


def plugin_dir(local_repo: Path, layout: Layout, plugin_id: str) -> Path:
    """
    返回插件本地目录。

    :param local_repo: 本地仓库目录
    :param layout: 仓库布局
    :param plugin_id: 插件 ID
    :return: 插件目录
    """
    return local_repo / layout.plugin_root / plugin_id.lower()


def remote_plugin_prefix(layout: Layout, plugin_id: str) -> str:
    """
    返回远端插件目录前缀。

    :param layout: 仓库布局
    :param plugin_id: 插件 ID
    :return: 远端目录路径
    """
    return f"{layout.plugin_root}/{plugin_id.lower()}"


def read_package_entry(local_repo: Path, layout: Layout, plugin_id: str) -> dict[str, Any]:
    """
    读取本地 package 中当前插件的元数据条目。

    :param local_repo: 本地仓库目录
    :param layout: 仓库布局
    :param plugin_id: 插件 ID
    :return: package 条目
    """
    package_path = local_repo / layout.package_file
    if not package_path.exists():
        raise FileNotFoundError(f"本地 package 文件不存在：{package_path}")
    package_data = json.loads(package_path.read_text(encoding="utf-8"))
    if not isinstance(package_data, dict):
        raise ValueError(f"本地 package 文件不是 JSON 对象：{package_path}")
    entry = package_data.get(plugin_id)
    if not isinstance(entry, dict):
        raise ValueError(f"{layout.package_file} 中未找到插件条目：{plugin_id}")
    return entry


def merge_package_content(
    remote_content: Optional[bytes],
    plugin_id: str,
    entry: dict[str, Any],
) -> bytes:
    """
    合并远端 package 内容，只更新当前插件条目。

    :param remote_content: 远端 package 原始内容
    :param plugin_id: 插件 ID
    :param entry: 当前插件 package 条目
    :return: 合并后的 package JSON 字节
    """
    if remote_content:
        try:
            package_data = json.loads(remote_content.decode("utf-8"))
        except json.JSONDecodeError as err:
            raise ValueError("远端 package 文件不是有效 JSON，拒绝覆盖") from err
        if not isinstance(package_data, dict):
            raise ValueError("远端 package 文件不是 JSON 对象，拒绝覆盖")
    else:
        package_data = {}
    package_data[plugin_id] = entry
    text = json.dumps(package_data, indent=2, ensure_ascii=False)
    return f"{text}\n".encode("utf-8")


def should_exclude(path: str, patterns: list[str], includes: list[str]) -> Optional[str]:
    """
    判断仓库相对路径是否应被排除。

    :param path: POSIX 相对路径
    :param patterns: 排除模式
    :param includes: 强制包含模式
    :return: 命中的排除模式，未排除时为 None
    """
    normalized = path.strip("/")
    for pattern in includes:
        if match_pattern(normalized, pattern):
            return None
    for pattern in patterns:
        if match_pattern(normalized, pattern):
            return pattern
    return None


def match_pattern(path: str, pattern: str) -> bool:
    """
    匹配路径忽略模式。

    :param path: POSIX 相对路径
    :param pattern: glob 模式
    :return: 是否命中
    """
    normalized = path.strip("/")
    raw = pattern.strip()
    if not raw:
        return False
    if raw.endswith("/"):
        prefix = raw.strip("/")
        return normalized == prefix or normalized.startswith(f"{prefix}/")
    if "/" not in raw:
        return any(fnmatch.fnmatch(part, raw) for part in normalized.split("/"))
    return fnmatch.fnmatch(normalized, raw.strip("/"))


def collect_local_files(
    local_repo: Path,
    layout: Layout,
    plugin_id: str,
    excludes: list[str],
    includes: list[str],
    allow_missing: bool = False,
) -> tuple[dict[str, FileState], dict[str, str]]:
    """
    收集准备发布的本地插件文件。

    :param local_repo: 本地仓库目录
    :param layout: 仓库布局
    :param plugin_id: 插件 ID
    :param excludes: 排除模式
    :param includes: 强制包含模式
    :param allow_missing: 插件目录不存在时是否返回空集合
    :return: 文件状态和被拒绝路径
    """
    source_dir = plugin_dir(local_repo, layout, plugin_id)
    if not source_dir.exists() or not source_dir.is_dir():
        if allow_missing:
            return {}, {}
        raise FileNotFoundError(f"插件目录不存在：{source_dir}")

    files: dict[str, FileState] = {}
    rejected: dict[str, str] = {}
    prefix = remote_plugin_prefix(layout, plugin_id)
    for file_path in sorted(source_dir.rglob("*")):
        if not file_path.is_file():
            continue
        relative = file_path.relative_to(source_dir).as_posix()
        reason = should_exclude(relative, excludes, includes)
        if reason:
            rejected[f"{prefix}/{relative}"] = reason
            continue
        remote_path = f"{prefix}/{relative}"
        files[remote_path] = FileState(
            path=remote_path,
            content=file_path.read_bytes(),
        )
    return files, rejected


def build_push_changes(
    local_files: dict[str, FileState],
    remote_files: dict[str, FileState],
    rejected: dict[str, str],
    delete_remote: bool,
) -> ChangeSet:
    """
    生成推送方向的差异。

    :param local_files: 本地文件
    :param remote_files: 远端文件
    :param rejected: 被安全规则拒绝的文件
    :param delete_remote: 是否删除远端多余文件
    :return: 差异集合
    """
    changes = ChangeSet(rejected=rejected)
    for path, local_state in local_files.items():
        remote_state = remote_files.get(path)
        if remote_state is None:
            changes.create.append(path)
        elif local_state.sha256 != remote_state.sha256:
            changes.update.append(path)
        else:
            changes.same.append(path)
    if delete_remote:
        for path in remote_files:
            if path not in local_files:
                changes.delete.append(path)
    return changes


def build_pull_changes(
    local_files: dict[str, FileState],
    remote_files: dict[str, FileState],
    rejected: dict[str, str],
    force: bool,
) -> ChangeSet:
    """
    生成拉取方向的差异。

    :param local_files: 本地文件
    :param remote_files: 远端文件
    :param rejected: 被安全规则保护的本地文件
    :param force: 是否允许覆盖冲突
    :return: 差异集合
    """
    changes = ChangeSet(rejected=rejected)
    for path, remote_state in remote_files.items():
        local_state = local_files.get(path)
        if local_state is None:
            changes.create.append(path)
        elif local_state.sha256 == remote_state.sha256:
            changes.same.append(path)
        elif force:
            changes.update.append(path)
        else:
            changes.conflicts.append(path)
    return changes


def compare_package(
    client: GitHubClient,
    layout: Layout,
    plugin_id: str,
    entry: dict[str, Any],
) -> tuple[Optional[FileState], bytes, str]:
    """
    比较 package 文件并返回远端状态、合并内容与差异类型。

    :param client: GitHub 客户端
    :param layout: 仓库布局
    :param plugin_id: 插件 ID
    :param entry: 本地 package 条目
    :return: 远端状态、合并内容、差异类型
    """
    remote_package = client.get_file(layout.package_file)
    merged_content = merge_package_content(
        remote_package.content if remote_package else None,
        plugin_id,
        entry,
    )
    if remote_package is None:
        change_type = "create"
    elif hashlib.sha256(merged_content).hexdigest() != remote_package.sha256:
        change_type = "update"
    else:
        change_type = "same"
    return remote_package, merged_content, change_type


def load_remote_files_for_push(
    context: dict[str, Any],
    args: argparse.Namespace,
) -> tuple[dict[str, FileState], Optional[FileState], bool]:
    """
    读取推送所需的远端文件，允许显式自动建仓时把缺失仓库视为空仓库。

    :param context: 运行上下文
    :param args: 命令行参数
    :return: 远端插件文件、远端 package 文件、仓库是否已存在
    """
    client: GitHubClient = context["client"]
    repo_exists = client.repo_exists()
    if not repo_exists and not args.create_repo_if_missing:
        raise GitHubError(
            404,
            "目标仓库不存在。请先创建仓库，或在用户明确同意后使用 --create-repo-if-missing。",
        )
    if not repo_exists:
        return {}, None, False
    return (
        client.list_files(context["remote_prefix"]),
        client.get_file(context["layout"].package_file),
        True,
    )


def preview(args: argparse.Namespace) -> dict[str, Any]:
    """
    预览本地插件与远端仓库的差异。

    :param args: 命令行参数
    :return: 预览结果
    """
    context = build_context(args)
    local_files, rejected = collect_local_files(
        context["local_repo"],
        context["layout"],
        context["plugin_id"],
        context["excludes"],
        context["includes"],
        allow_missing=True,
    )
    remote_files = context["client"].list_files(context["remote_prefix"])
    entry = read_package_entry(context["local_repo"], context["layout"], context["plugin_id"])
    _, _, package_change = compare_package(
        context["client"],
        context["layout"],
        context["plugin_id"],
        entry,
    )
    changes = build_push_changes(local_files, remote_files, rejected, args.delete_remote)
    return result_payload(context, changes, package_change=package_change)


def push(args: argparse.Namespace) -> dict[str, Any]:
    """
    推送本地插件变更到 GitHub。

    :param args: 命令行参数
    :return: 推送结果
    """
    context = build_context(args, require_token=not args.dry_run)
    client: GitHubClient = context["client"]
    local_files, rejected = collect_local_files(
        context["local_repo"],
        context["layout"],
        context["plugin_id"],
        context["excludes"],
        context["includes"],
    )
    entry = read_package_entry(context["local_repo"], context["layout"], context["plugin_id"])
    remote_files, remote_package, repo_existed = load_remote_files_for_push(context, args)
    package_content = merge_package_content(
        remote_package.content if remote_package else None,
        context["plugin_id"],
        entry,
    )
    if remote_package is None:
        package_change = "create"
    elif hashlib.sha256(package_content).hexdigest() != remote_package.sha256:
        package_change = "update"
    else:
        package_change = "same"
    changes = build_push_changes(local_files, remote_files, rejected, args.delete_remote)
    payload = result_payload(context, changes, package_change=package_change)
    payload["repo_existed"] = repo_existed
    if args.dry_run:
        payload["dry_run"] = True
        return payload

    if not repo_existed:
        client.create_repo(private=args.private)
    message = args.message or f"Publish {context['plugin_id']}"
    applied: dict[str, list[str]] = {"create": [], "update": [], "delete": []}
    if package_change in {"create", "update"}:
        client.put_file(
            context["layout"].package_file,
            package_content,
            message,
            remote_package.sha if remote_package else None,
        )
        applied[package_change].append(context["layout"].package_file)
    for path in changes.create:
        client.put_file(path, local_files[path].content, message, None)
        applied["create"].append(path)
    for path in changes.update:
        client.put_file(path, local_files[path].content, message, remote_files[path].sha)
        applied["update"].append(path)
    for path in changes.delete:
        remote_sha = remote_files[path].sha
        if remote_sha:
            client.delete_file(path, message, remote_sha)
            applied["delete"].append(path)
    payload["applied"] = applied
    payload["message"] = "插件已推送到 GitHub 仓库。"
    return payload


def create_repo(args: argparse.Namespace) -> dict[str, Any]:
    """
    创建 GitHub 仓库，默认创建公开仓库。

    :param args: 命令行参数
    :return: 创建结果
    """
    context = build_context(args, require_token=not args.dry_run)
    client: GitHubClient = context["client"]
    if args.dry_run:
        return {
            "success": True,
            "repo": context["repo"],
            "created": False,
            "private": args.private,
            "dry_run": True,
            "message": "dry-run 未访问 GitHub，未创建仓库。",
        }
    if client.repo_exists():
        return {
            "success": True,
            "repo": context["repo"],
            "created": False,
            "private": args.private,
            "message": "目标 GitHub 仓库已存在。",
        }
    data = client.create_repo(private=args.private)
    return {
        "success": True,
        "repo": context["repo"],
        "created": True,
        "private": bool(data.get("private")) if isinstance(data, dict) else args.private,
        "url": data.get("html_url") if isinstance(data, dict) else None,
        "message": "目标 GitHub 仓库已创建。",
    }


def pull(args: argparse.Namespace) -> dict[str, Any]:
    """
    从 GitHub 拉取插件文件到本地仓库。

    :param args: 命令行参数
    :return: 拉取结果
    """
    context = build_context(args)
    client: GitHubClient = context["client"]
    local_files, rejected = collect_local_files(
        context["local_repo"],
        context["layout"],
        context["plugin_id"],
        context["excludes"],
        context["includes"],
    )
    remote_files = client.list_files(context["remote_prefix"])
    changes = build_pull_changes(local_files, remote_files, rejected, args.force)
    package_file = client.get_file(context["layout"].package_file)
    package_change = "missing"
    package_entry = None
    if package_file:
        package_data = json.loads(package_file.content.decode("utf-8"))
        if isinstance(package_data, dict):
            package_entry = package_data.get(context["plugin_id"])
            local_entry = None
            try:
                local_entry = read_package_entry(
                    context["local_repo"],
                    context["layout"],
                    context["plugin_id"],
                )
            except (FileNotFoundError, ValueError, json.JSONDecodeError):
                local_entry = None
            package_change = "same" if package_entry == local_entry else "update"
    payload = result_payload(context, changes, package_change=package_change)
    if changes.conflicts and not args.force:
        payload["message"] = "本地存在未发布改动，已拒绝覆盖。确认后可使用 --force。"
        return payload
    if args.dry_run:
        payload["dry_run"] = True
        return payload

    target_dir = plugin_dir(context["local_repo"], context["layout"], context["plugin_id"])
    for path in changes.create + changes.update:
        relative = PurePosixPath(path).relative_to(context["remote_prefix"]).as_posix()
        target = target_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(remote_files[path].content)
    if package_entry is not None:
        write_local_package_entry(
            context["local_repo"],
            context["layout"],
            context["plugin_id"],
            package_entry,
        )
    payload["message"] = "远端插件已同步到本地仓库。"
    return payload


def write_local_package_entry(
    local_repo: Path,
    layout: Layout,
    plugin_id: str,
    entry: dict[str, Any],
) -> None:
    """
    写入本地 package 中的插件条目。

    :param local_repo: 本地仓库目录
    :param layout: 仓库布局
    :param plugin_id: 插件 ID
    :param entry: 插件 package 条目
    """
    package_path = local_repo / layout.package_file
    if package_path.exists():
        package_data = json.loads(package_path.read_text(encoding="utf-8"))
        if not isinstance(package_data, dict):
            raise ValueError(f"本地 package 文件不是 JSON 对象：{package_path}")
    else:
        package_data = {}
        package_path.parent.mkdir(parents=True, exist_ok=True)
    package_data[plugin_id] = entry
    package_path.write_text(
        f"{json.dumps(package_data, indent=2, ensure_ascii=False)}\n",
        encoding="utf-8",
    )


def result_payload(
    context: dict[str, Any],
    changes: ChangeSet,
    package_change: str,
) -> dict[str, Any]:
    """
    生成统一 JSON 输出。

    :param context: 运行上下文
    :param changes: 文件差异
    :param package_change: package 文件差异类型
    :return: 结果对象
    """
    return {
        "repo": context["repo"],
        "branch": context["branch"],
        "plugin_id": context["plugin_id"],
        "local_repo": str(context["local_repo"]),
        "layout": context["layout"].version,
        "package_file": context["layout"].package_file,
        "plugin_prefix": context["remote_prefix"],
        "package_change": package_change,
        "changes": changes.to_dict(),
    }


def build_context(args: argparse.Namespace, require_token: bool = False) -> dict[str, Any]:
    """
    根据命令行参数构建运行上下文。

    :param args: 命令行参数
    :param require_token: 是否要求可写 token
    :return: 运行上下文
    """
    repo = normalize_repo(args.repo)
    plugin_id = getattr(args, "plugin_id", "") or ""
    package_version = getattr(args, "package_version", "auto") or "auto"
    if args.command in COMMANDS_REQUIRE_LOCAL_PLUGIN:
        local_repo = resolve_local_repo(args.local_repo, plugin_id, package_version)
        layout = resolve_layout(package_version, local_repo, plugin_id)
    else:
        local_repo = Path.cwd()
        layout = Layout(package_file="package.v2.json", plugin_root="plugins.v2")
    token = resolve_token(repo, args.token)
    if require_token and not token:
        raise ValueError("未配置 GitHub token，无法写入仓库")
    branch = args.branch or DEFAULT_BRANCH
    client = GitHubClient(
        repo=repo,
        token=token,
        branch=branch,
        api_base=args.api_base,
        timeout=args.timeout,
        proxy=resolve_proxy(args.proxy),
    )
    excludes = list(DEFAULT_EXCLUDES) + list(args.exclude or [])
    includes = list(args.include or [])
    return {
        "repo": repo,
        "branch": branch,
        "local_repo": local_repo,
        "layout": layout,
        "plugin_id": plugin_id,
        "remote_prefix": remote_plugin_prefix(layout, plugin_id) if plugin_id else "",
        "client": client,
        "excludes": excludes,
        "includes": includes,
    }


def print_json(payload: dict[str, Any]) -> None:
    """
    打印 JSON 输出。

    :param payload: 输出对象
    """
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    """
    构造命令行参数解析器。

    :return: argparse 解析器
    """
    parser = argparse.ArgumentParser(
        description="发布和同步 MoviePilot 本地插件到 GitHub 仓库",
    )
    parser.add_argument("command", choices=("create-repo", "preview", "push", "pull"))
    parser.add_argument("--repo", required=True, help="GitHub 仓库，格式 owner/repo")
    parser.add_argument("--plugin-id", default="", help="插件类名 ID")
    parser.add_argument("--local-repo", default="", help="本地插件仓库目录")
    parser.add_argument("--package-version", default="auto", help="auto、v2 或 legacy")
    parser.add_argument("--branch", default=DEFAULT_BRANCH, help="目标分支")
    parser.add_argument("--token", default="", help="GitHub token，默认读取配置或环境变量")
    parser.add_argument("--message", default="", help="提交消息")
    parser.add_argument("--api-base", default=GITHUB_API_BASE, help="GitHub API 地址")
    parser.add_argument("--proxy", default="", help="HTTP/HTTPS 代理")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="请求超时时间")
    parser.add_argument("--include", action="append", default=[], help="强制包含 glob")
    parser.add_argument("--exclude", action="append", default=[], help="额外排除 glob")
    parser.add_argument("--delete-remote", action="store_true", help="推送时删除远端多余文件")
    parser.add_argument(
        "--create-repo-if-missing",
        action="store_true",
        help="推送时在目标仓库不存在的情况下自动创建公开仓库",
    )
    parser.add_argument("--private", action="store_true", help="创建仓库时使用私有可见性")
    parser.add_argument("--force", action="store_true", help="拉取时允许覆盖本地冲突")
    parser.add_argument("--dry-run", action="store_true", help="只输出计划，不写入")
    return parser


def main() -> int:
    """
    执行命令行入口。

    :return: 进程退出码
    """
    parser = build_parser()
    args = parser.parse_args()
    if args.command in COMMANDS_REQUIRE_LOCAL_PLUGIN and not args.plugin_id:
        print_json({"success": False, "message": "preview、push、pull 必须提供 --plugin-id"})
        return 1
    try:
        if args.command == "create-repo":
            payload = create_repo(args)
        elif args.command == "preview":
            payload = preview(args)
        elif args.command == "push":
            payload = push(args)
        else:
            payload = pull(args)
    except (GitHubError, OSError, ValueError, json.JSONDecodeError) as err:
        print_json({"success": False, "message": str(err)})
        return 1
    if "success" not in payload:
        payload["success"] = not payload.get("changes", {}).get("conflicts")
    print_json(payload)
    return 0 if payload["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
