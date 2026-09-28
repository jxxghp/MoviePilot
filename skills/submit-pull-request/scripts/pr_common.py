"""submit-pull-request Skill 的 Git、GitHub 和路径安全共享逻辑。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Optional


def _find_repo_root() -> Path:
    """从当前工作目录或 Skill 路径定位 MoviePilot 源码根目录。"""
    script_path = Path(__file__).resolve()
    candidates = [Path.cwd().resolve(), *Path.cwd().resolve().parents]
    candidates.extend([script_path.parent, *script_path.parents])
    for candidate in candidates:
        if (candidate / "app" / "runtime" / "config.py").is_file():
            return candidate
    return script_path.parents[3]


SCRIPT_ROOT = _find_repo_root()
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from app.adapters.network.http import RequestUtils  # noqa: E402
from app.runtime.config import settings  # noqa: E402
from app.runtime.settings import get_runtime_setting  # noqa: E402

GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
DEFAULT_TIMEOUT_SECONDS = 30
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_CHANGED_FILES = 200

_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9._/-]+$")
_TOKEN_PATTERN = re.compile(r"(?i)(bearer|token)\s+[^\s,;]+")
_SSH_REPO_PATTERN = re.compile(r"^(?:[^@]+@)?([^:]+):([^/]+/[^/]+?)(?:\.git)?$")

_REJECTED_PATH_PARTS = {
    ".git",
    ".env",
    "config",
    "data",
    "cache",
    "logs",
    "tmp",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
}
_REJECTED_SUFFIXES = {
    ".db",
    ".sqlite",
    ".sqlite3",
    ".log",
    ".key",
    ".pem",
    ".p12",
    ".pfx",
    ".secret",
}


class PullRequestError(RuntimeError):
    """PR 发布流程遇到的可向 Agent 返回的稳定错误。"""

    def __init__(self, reason: str, message: str, *, status: int = 0) -> None:
        """保存机器可判断的原因、用户可读消息和可选 HTTP 状态。"""
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.status = status


class GitHubApiError(PullRequestError):
    """GitHub API 返回非成功状态或无法解析响应。"""


@dataclass(frozen=True)
class Change:
    """表示一个待提交文件的最终内容和 Git 工作树摘要。"""

    path: str
    source_path: Path
    operation: str
    before_sha256: Optional[str]
    after_sha256: Optional[str]
    size_bytes: int
    mode: str


def result_payload(**payload: Any) -> str:
    """将脚本结果序列化为稳定 JSON，供 Agent 解析。"""
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def redact_text(value: Any) -> str:
    """去除错误信息中可能出现的 Authorization 凭据。"""
    text = str(value or "")
    return _TOKEN_PATTERN.sub(r"\1 <REDACTED>", text)


def normalize_repo(repo: str) -> str:
    """规范化并校验 GitHub owner/repo 标识。"""
    value = (repo or "").strip().removesuffix(".git")
    if value.startswith(("http://", "https://")):
        parsed = urllib.parse.urlparse(value)
        if parsed.netloc.lower() not in {"github.com", "www.github.com"}:
            raise ValueError("目标仓库必须位于 GitHub")
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if len(parts) >= 2:
            value = f"{parts[0]}/{parts[1]}"
    if not _REPO_PATTERN.fullmatch(value):
        raise ValueError("目标仓库必须是 owner/repo 或 GitHub URL")
    return value


def normalize_branch(branch: str) -> str:
    """校验分支名，拒绝 ref 前缀、空段和路径穿越。"""
    value = (branch or "").strip()
    if value.startswith("refs/") or not _BRANCH_PATTERN.fullmatch(value):
        raise ValueError("分支名包含不支持的字符")
    if value.startswith("/") or value.endswith("/") or ".." in value or "//" in value:
        raise ValueError("分支名不能包含空段或连续点")
    return value


def normalize_relative_path(path: str) -> str:
    """将文件路径规范化为仓库内 POSIX 相对路径。"""
    raw_value = str(path or "").replace("\\", "/")
    if not raw_value or raw_value.startswith("/") or re.match(r"^[A-Za-z]:/", raw_value):
        raise ValueError(f"文件路径不在源目录内: {path}")
    value = raw_value.strip("/")
    pure_path = PurePosixPath(value)
    if not value or value == "." or pure_path.is_absolute() or ".." in pure_path.parts:
        raise ValueError(f"文件路径不在源目录内: {path}")
    return pure_path.as_posix()


def is_rejected_path(path: str) -> bool:
    """判断路径是否指向运行时状态、缓存或明显敏感文件。"""
    normalized = normalize_relative_path(path)
    parts = set(PurePosixPath(normalized).parts)
    if parts & _REJECTED_PATH_PARTS:
        return True
    return PurePosixPath(normalized).suffix.lower() in _REJECTED_SUFFIXES


def sha256_bytes(content: bytes) -> str:
    """返回原始字节的 SHA-256 摘要。"""
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: Path) -> str:
    """按块计算文件摘要，避免把大文件一次性载入内存。"""
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def runtime_dir() -> Path:
    """返回普通 Agent 用户可读写的 Skill 工作目录，不把草稿写入源码仓库。"""
    directory = Path(get_runtime_setting("CONFIG_PATH")) / "agent" / "submit-pull-request"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def write_json_file(path: Path, payload: dict[str, Any]) -> None:
    """以原子替换方式写入 Skill 运行状态 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_json_file(path: Path, *, reason: str = "invalid_state") -> dict[str, Any]:
    """读取并校验 Skill 运行状态 JSON。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PullRequestError(reason, f"无法读取运行状态文件: {path}") from error
    if not isinstance(payload, dict):
        raise PullRequestError(reason, f"运行状态文件不是 JSON 对象: {path}")
    return payload


def _run_git(
    root: Path,
    *arguments: str,
    env: Optional[dict[str, str]] = None,
) -> subprocess.CompletedProcess[bytes]:
    """运行不经过 shell 的 Git 命令，避免用户输入触发命令注入。"""
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as error:
        raise PullRequestError("git_unavailable", "当前环境没有可用的 Git 命令。") from error


def _git_output(root: Path, *arguments: str, env: Optional[dict[str, str]] = None) -> bytes:
    """运行 Git 并在失败时返回不含凭据的稳定错误。"""
    result = _run_git(root, *arguments, env=env)
    if result.returncode != 0:
        detail = redact_text(result.stderr.decode("utf-8", errors="replace"))[:500]
        raise PullRequestError(
            "git_command_failed",
            f"Git 命令失败: {arguments[0]}{(': ' + detail) if detail else ''}",
        )
    return result.stdout


def run_git(
    root: Path,
    *arguments: str,
    env: Optional[dict[str, str]] = None,
) -> subprocess.CompletedProcess[bytes]:
    """运行 Git 并返回完整结果，供 clone、提交和推送脚本复用。"""
    return _run_git(root, *arguments, env=env)


def git_output(root: Path, *arguments: str, env: Optional[dict[str, str]] = None) -> bytes:
    """运行 Git 并要求命令成功，供需要稳定失败原因的脚本复用。"""
    return _git_output(root, *arguments, env=env)


def find_git_root(source_root: Path) -> Optional[Path]:
    """返回源目录对应的 Git 根目录；没有仓库时返回 None。"""
    result = _run_git(source_root, "rev-parse", "--show-toplevel")
    if result.returncode != 0:
        return None
    try:
        root = Path(result.stdout.decode("utf-8").strip()).resolve()
    except OSError:
        return None
    return root if root.is_dir() else None


def require_git_root(source_root: Path) -> Path:
    """要求源目录本身是 clone 的 Git 根目录。"""
    source_root = source_root.expanduser().resolve()
    if not source_root.is_dir():
        raise PullRequestError("invalid_source_root", f"源目录不存在或不是目录: {source_root}")
    git_root = find_git_root(source_root)
    if git_root is None:
        raise PullRequestError("git_required", "PR 提交必须基于先前 git clone 的仓库目录。")
    if git_root != source_root:
        raise PullRequestError(
            "source_root_must_be_git_root",
            f"请把 clone 的仓库根目录作为 source_root: {git_root}",
        )
    return git_root


def _git_sha256(root: Path, path: str) -> Optional[str]:
    """读取 HEAD 中的文件内容并计算摘要，用于记录变更前版本。"""
    result = _run_git(root, "show", f"HEAD:{path}")
    if result.returncode != 0:
        return None
    return sha256_bytes(result.stdout)


def _file_mode(path: Path) -> str:
    """返回普通文件或可执行文件对应的 Git tree mode。"""
    return "100755" if path.is_file() and path.stat().st_mode & 0o111 else "100644"


def _git_changes(root: Path) -> list[dict[str, Any]]:
    """收集 HEAD 到当前工作树的变更，包括未跟踪文件。"""
    result = _run_git(root, "diff", "--name-status", "--no-renames", "-z", "HEAD", "--")
    if result.returncode != 0:
        raise PullRequestError("git_status_failed", "无法读取当前 Git 工作树变更。")

    changes: dict[str, str] = {}
    tokens = result.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    index = 0
    while index + 1 < len(tokens):
        status, path = tokens[index], tokens[index + 1]
        index += 2
        if status and path:
            changes[normalize_relative_path(path)] = status[0]

    untracked = _run_git(root, "ls-files", "--others", "--exclude-standard", "-z")
    if untracked.returncode != 0:
        raise PullRequestError("git_status_failed", "无法读取未跟踪文件列表。")
    for path in untracked.stdout.decode("utf-8", errors="surrogateescape").split("\0"):
        if path:
            changes[normalize_relative_path(path)] = "A"

    result_changes = []
    for path, status in sorted(changes.items()):
        if is_rejected_path(path):
            raise PullRequestError("sensitive_path", f"拒绝提交运行时或敏感路径: {path}")
        local_path = root / path
        if local_path.is_symlink():
            raise PullRequestError("unsupported_change", f"拒绝提交符号链接文件: {path}")
        source_path = local_path.resolve(strict=False)
        if not _is_within(source_path, root):
            raise PullRequestError("path_outside_root", f"文件路径超出源目录: {path}")
        exists = source_path.is_file()
        if status == "D" or not exists:
            if status != "D":
                raise PullRequestError("unsupported_change", f"变更不是普通文件: {path}")
            operation = "delete"
        else:
            operation = "create" if status == "A" else "update"
        result_changes.append(
            {
                "path": path,
                "source_path": str(source_path),
                "operation": operation,
                "before_sha256": None if operation == "create" else _git_sha256(root, path),
                "after_sha256": sha256_file(source_path) if exists else None,
                "size_bytes": source_path.stat().st_size if exists else 0,
                "mode": _file_mode(source_path),
            }
        )
    return result_changes


def _is_within(path: Path, root: Path) -> bool:
    """判断解析后的路径是否仍位于指定根目录。"""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def git_current_branch(root: Path) -> str:
    """读取当前 clone 的分支名，拒绝 detached HEAD。"""
    branch = _git_output(root, "symbolic-ref", "--quiet", "--short", "HEAD").decode().strip()
    if not branch:
        raise PullRequestError("detached_head", "当前 clone 处于 detached HEAD，不能安全提交 PR。")
    try:
        return normalize_branch(branch)
    except ValueError as error:
        raise PullRequestError("invalid_branch", str(error)) from error


def git_remote_url(root: Path, remote: str = "origin") -> str:
    """读取指定 Git remote URL。"""
    return _git_output(root, "remote", "get-url", remote).decode().strip()


def remote_repo_from_url(remote_url: str) -> str:
    """从 HTTPS、SSH 或 scp 风格 remote URL 提取 GitHub 仓库名。"""
    value = (remote_url or "").strip()
    if value.startswith(("http://", "https://", "ssh://")):
        parsed = urllib.parse.urlparse(value)
        if parsed.hostname and parsed.hostname.lower() not in {"github.com", "www.github.com"}:
            raise PullRequestError("unsupported_remote", "Git remote 必须指向 GitHub。")
        return normalize_repo(parsed.path.strip("/"))
    match = _SSH_REPO_PATTERN.fullmatch(value)
    if not match or match.group(1).lower() not in {"github.com", "www.github.com"}:
        raise PullRequestError("unsupported_remote", "无法确认 Git remote 的 GitHub 仓库。")
    return normalize_repo(match.group(2))


def collect_changes(source_root: Path) -> tuple[str, list[dict[str, Any]]]:
    """只从先前 clone 的 Git 工作树收集代码变更。"""
    git_root = require_git_root(source_root)
    changes = _git_changes(git_root)
    if not changes:
        raise PullRequestError("no_changes", "没有检测到可提交的 Git 工作树变更。")
    if len(changes) > MAX_CHANGED_FILES:
        raise PullRequestError("too_many_files", f"单次 PR 最多允许 {MAX_CHANGED_FILES} 个文件。")
    return "git", changes


def change_fingerprint(changes: list[dict[str, Any]]) -> str:
    """根据路径、操作、最终摘要和文件模式生成幂等变更指纹。"""
    stable = [
        {
            "path": item["path"],
            "operation": item["operation"],
            "after_sha256": item.get("after_sha256"),
            "mode": item.get("mode", "100644"),
        }
        for item in sorted(changes, key=lambda value: value["path"])
    ]
    return hashlib.sha256(
        json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_branch_name(target_repo: str, base: str) -> str:
    """生成不会覆盖远端已有分支的 Agent 工作分支名。"""
    seed = f"{target_repo}:{base}:{time.time_ns()}:{secrets.token_hex(8)}".encode()
    return normalize_branch(f"agent/moviepilot/{hashlib.sha256(seed).hexdigest()[:16]}")


def load_github_headers(repo: str) -> tuple[dict[str, str], Any]:
    """读取仓库专属或全局 GitHub 请求头，不返回明文 Token。"""
    headers: dict[str, str] = {}
    proxy: Any = None
    try:
        headers = dict(settings.REPO_GITHUB_HEADERS(repo=repo))
        proxy = settings.PROXY
    except Exception:
        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        proxy = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    has_authorization = any(key.lower() == "authorization" and value for key, value in headers.items())
    if not has_authorization:
        token = (
            os.environ.get("MOVIEPILOT_GITHUB_TOKEN")
            or os.environ.get("GITHUB_TOKEN")
            or os.environ.get("GH_TOKEN")
            or ""
        ).strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
    if proxy is None:
        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        proxy = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    headers.setdefault("Accept", "application/vnd.github+json")
    headers.setdefault("User-Agent", "MoviePilot-Agent-PR")
    headers.setdefault("X-GitHub-Api-Version", GITHUB_API_VERSION)
    return headers, proxy


def has_github_auth(headers: dict[str, str]) -> bool:
    """判断请求头是否包含认证信息，不解析或输出 Token。"""
    return any(key.lower() == "authorization" and value for key, value in headers.items())


def github_token_from_headers(headers: dict[str, str]) -> str:
    """从内部 GitHub Authorization 请求头提取 Git HTTPS 所需的令牌。"""
    authorization = next(
        (value for key, value in headers.items() if key.lower() == "authorization"),
        "",
    )
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() not in {"bearer", "token"} or not token.strip():
        raise PullRequestError("unsupported_token", "当前 GitHub Token 格式不能用于 Git HTTPS 推送。")
    return token.strip()


@contextmanager
def git_auth_environment(headers: dict[str, str]) -> Iterator[dict[str, str]]:
    """通过临时 askpass 和进程环境向 Git 提供令牌，避免令牌出现在命令行。"""
    token = github_token_from_headers(headers)
    with tempfile.TemporaryDirectory(prefix="git-askpass-", dir=runtime_dir()) as directory:
        askpass = Path(directory) / "askpass.sh"
        askpass.write_text(
            "#!/bin/sh\n"
            'case "${1:-}" in\n'
            "  *[Uu]sername*) printf '%s\\n' 'x-access-token' ;;\n"
            "  *) printf '%s\\n' \"${MOVIEPILOT_GIT_TOKEN}\" ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        askpass.chmod(0o700)
        environment = os.environ.copy()
        environment.update(
            {
                "GIT_ASKPASS": str(askpass),
                "GIT_TERMINAL_PROMPT": "0",
                "MOVIEPILOT_GIT_TOKEN": token,
            }
        )
        yield environment


def github_clone_url(repo: str) -> str:
    """返回不嵌入令牌的 GitHub HTTPS clone URL。"""
    return f"https://github.com/{normalize_repo(repo)}.git"


def _quote_path(value: str) -> str:
    """编码 GitHub API 路径中的单个或多个层级。"""
    return "/".join(urllib.parse.quote(part, safe="") for part in value.split("/"))


class GitHubClient:
    """通过 MoviePilot RequestUtils 调用 GitHub REST API。"""

    def __init__(
        self,
        *,
        headers: dict[str, str],
        proxies: Any = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """保存 HTTP 配置；Token 仅保留在请求头内。"""
        self.headers = headers
        self.proxies = proxies
        self.timeout = timeout

    def request(self, method: str, path: str, payload: Optional[dict[str, Any]] = None) -> Any:
        """发起一次 JSON 请求并将 GitHub 错误转换为稳定异常。"""
        body = None
        headers = dict(self.headers)
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        response = None
        try:
            response = RequestUtils(
                proxies=self.proxies,
                headers=headers,
                timeout=self.timeout,
            ).request(
                method=method.lower(),
                url=f"{GITHUB_API_BASE}{path}",
                data=body,
                raise_exception=False,
            )
            if response is None:
                raise GitHubApiError("github_unavailable", "GitHub API 未返回响应。")
            raw = response.content or b""
            if response.status_code >= 400:
                message = raw.decode("utf-8", errors="replace")[:500]
                raise GitHubApiError(
                    "github_api_error",
                    f"GitHub API 返回 HTTP {response.status_code}: {redact_text(message)}",
                    status=response.status_code,
                )
            if not raw:
                return None
            try:
                return json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as error:
                raise GitHubApiError("invalid_github_response", "GitHub API 响应不是合法 JSON。") from error
        except GitHubApiError:
            raise
        except Exception as error:
            raise GitHubApiError("network_error", f"访问 GitHub API 失败: {redact_text(error)}") from error
        finally:
            if response is not None:
                response.close()

    def get_user(self) -> dict[str, Any]:
        """读取当前 Token 对应的 GitHub 用户。"""
        data = self.request("GET", "/user")
        if not isinstance(data, dict) or not data.get("login"):
            raise GitHubApiError("invalid_github_user", "无法确定 GitHub Token 对应的用户。")
        return data

    def get_repo(self, repo: str) -> dict[str, Any]:
        """读取仓库详情。"""
        data = self.request("GET", f"/repos/{normalize_repo(repo)}")
        if not isinstance(data, dict):
            raise GitHubApiError("invalid_github_repo", f"GitHub 仓库响应无效: {repo}")
        return data

    def get_ref(self, repo: str, branch: str) -> dict[str, Any]:
        """读取分支引用。"""
        branch = normalize_branch(branch)
        data = self.request(
            "GET",
            f"/repos/{normalize_repo(repo)}/git/ref/heads/{_quote_path(branch)}",
        )
        if not isinstance(data, dict) or not isinstance(data.get("object"), dict):
            raise GitHubApiError("invalid_github_ref", f"GitHub 分支响应无效: {repo}:{branch}")
        return data

    def create_fork(self, repo: str) -> dict[str, Any]:
        """请求为当前认证用户创建 Fork。"""
        data = self.request("POST", f"/repos/{normalize_repo(repo)}/forks", {"default_branch_only": True})
        if not isinstance(data, dict):
            raise GitHubApiError("invalid_fork_response", "GitHub Fork 响应无效。")
        return data

    def list_open_pulls(self, repo: str, *, head: str, base: str) -> list[dict[str, Any]]:
        """列出指定 head/base 的开放 PR，用于幂等重试。"""
        query = urllib.parse.urlencode({"state": "open", "head": head, "base": base, "per_page": "20"})
        data = self.request("GET", f"/repos/{normalize_repo(repo)}/pulls?{query}")
        return data if isinstance(data, list) else []

    def create_pull(self, repo: str, payload: dict[str, Any]) -> dict[str, Any]:
        """创建上游 Pull Request。"""
        data = self.request("POST", f"/repos/{normalize_repo(repo)}/pulls", payload)
        if not isinstance(data, dict) or not data.get("html_url"):
            raise GitHubApiError("invalid_pull_response", "GitHub PR 响应缺少 URL。")
        return data


def ensure_fork(client: GitHubClient, upstream_repo: str, login: str) -> tuple[str, bool]:
    """复用当前用户对目标仓库的已有 Fork，否则创建并等待可读。"""
    upstream = normalize_repo(upstream_repo)
    owner, name = upstream.split("/", 1)
    if owner.lower() == login.lower():
        repository = client.get_repo(upstream)
        permissions = repository.get("permissions") if isinstance(repository, dict) else None
        if isinstance(permissions, dict) and permissions.get("push") is False:
            raise PullRequestError("fork_permission", f"当前 Token 没有向目标仓库推送的权限: {upstream}")
        return upstream, False
    fork_repo = normalize_repo(f"{login}/{name}")
    try:
        fork_data = client.get_repo(fork_repo)
    except GitHubApiError as error:
        if error.status != 404:
            raise
        client.create_fork(upstream)
        deadline = time.monotonic() + 60
        fork_data = None
        while time.monotonic() < deadline:
            try:
                fork_data = client.get_repo(fork_repo)
                break
            except GitHubApiError as retry_error:
                if retry_error.status != 404:
                    raise
                time.sleep(1)
        if fork_data is None:
            raise PullRequestError("fork_timeout", f"Fork {fork_repo} 创建后未在规定时间内可用。")
        created = True
    else:
        created = False

    parent = fork_data.get("parent") if isinstance(fork_data, dict) else None
    if not isinstance(fork_data, dict) or fork_data.get("fork") is not True or not isinstance(parent, dict):
        raise PullRequestError("invalid_fork", f"{fork_repo} 存在，但不是目标仓库的 Fork。")
    try:
        parent_full_name = normalize_repo(str(parent.get("full_name") or ""))
    except ValueError as error:
        raise PullRequestError("invalid_fork", f"无法确认 {fork_repo} 的上游仓库。") from error
    if parent_full_name.lower() != upstream.lower():
        raise PullRequestError("wrong_fork_parent", f"{fork_repo} 的上游不是 {upstream}。")
    return fork_repo, created


def pull_head(target_repo: str, fork_repo: str, login: str, branch: str) -> str:
    """返回 GitHub 创建 PR 所需的 head 表达式。"""
    if normalize_repo(target_repo).lower() == normalize_repo(fork_repo).lower():
        return normalize_branch(branch)
    return f"{login}:{normalize_branch(branch)}"


def changes_from_payload(payload: dict[str, Any]) -> list[Change]:
    """读取并验证预览中的变更，确认 Git 工作树仍与预览一致。"""
    source_root = Path(str(payload.get("source_root") or "")).expanduser().resolve()
    changes_file = Path(str(payload.get("changes_file") or "")).expanduser().resolve()
    require_git_root(source_root)
    if not changes_file.is_file():
        raise PullRequestError("missing_changes", "PR 变更清单不存在，请重新准备预览。")
    try:
        data = json.loads(changes_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PullRequestError("invalid_changes", "PR 变更清单无法读取。") from error
    raw_changes = data.get("changes") if isinstance(data, dict) else None
    if not isinstance(raw_changes, list) or not raw_changes:
        raise PullRequestError("no_changes", "PR 变更清单为空。")

    result = []
    for raw in raw_changes:
        if not isinstance(raw, dict):
            raise PullRequestError("invalid_changes", "PR 变更清单包含无效条目。")
        path = normalize_relative_path(str(raw.get("path") or ""))
        local_path = source_root / path
        if local_path.is_symlink():
            raise PullRequestError("unsupported_change", f"拒绝提交符号链接文件: {path}")
        source_path = local_path.resolve(strict=False)
        if not _is_within(source_path, source_root):
            raise PullRequestError("path_outside_root", f"文件路径超出源目录: {path}")
        if is_rejected_path(path):
            raise PullRequestError("sensitive_path", f"拒绝提交运行时或敏感路径: {path}")
        operation = str(raw.get("operation") or "")
        if operation not in {"create", "update", "delete"}:
            raise PullRequestError("invalid_changes", f"文件操作无效: {path}")
        exists = source_path.is_file()
        after_sha256 = sha256_file(source_path) if exists else None
        if after_sha256 != raw.get("after_sha256"):
            raise PullRequestError("changes_modified", f"预览后文件发生变化: {path}")
        if operation != "delete" and not exists:
            raise PullRequestError("missing_file", f"待提交文件不存在: {path}")
        if operation == "delete" and exists:
            raise PullRequestError("delete_changed", f"待删除文件重新出现: {path}")
        size_bytes = source_path.stat().st_size if exists else 0
        if size_bytes > MAX_FILE_BYTES:
            raise PullRequestError("file_too_large", f"文件超过 {MAX_FILE_BYTES // 1024 // 1024} MiB: {path}")
        mode = _file_mode(source_path)
        if mode != raw.get("mode", "100644"):
            raise PullRequestError("changes_modified", f"预览后文件权限发生变化: {path}")
        result.append(
            Change(
                path=path,
                source_path=source_path,
                operation=operation,
                before_sha256=raw.get("before_sha256"),
                after_sha256=after_sha256,
                size_bytes=size_bytes,
                mode=mode,
            )
        )

    _, current_changes = collect_changes(source_root)
    expected_fingerprint = data.get("fingerprint") if isinstance(data, dict) else None
    current_fingerprint = change_fingerprint(current_changes)
    if expected_fingerprint != current_fingerprint:
        raise PullRequestError("changes_modified", "Git 工作树与已确认的 PR 预览不一致。")
    return result


__all__ = [
    "Change",
    "GitHubApiError",
    "GitHubClient",
    "MAX_CHANGED_FILES",
    "PullRequestError",
    "build_branch_name",
    "change_fingerprint",
    "changes_from_payload",
    "collect_changes",
    "ensure_fork",
    "find_git_root",
    "git_output",
    "git_auth_environment",
    "git_current_branch",
    "git_remote_url",
    "github_clone_url",
    "github_token_from_headers",
    "has_github_auth",
    "is_rejected_path",
    "load_github_headers",
    "normalize_branch",
    "normalize_repo",
    "pull_head",
    "read_json_file",
    "remote_repo_from_url",
    "require_git_root",
    "result_payload",
    "run_git",
    "runtime_dir",
    "write_json_file",
]
