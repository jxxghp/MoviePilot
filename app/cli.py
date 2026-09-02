import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, get_args, get_origin
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import click
import psutil

from app.application.backup import BackupArtifact
from app.runtime.config import Settings
from app.runtime.settings import (
    get_runtime_setting,
    has_runtime_setting,
    update_runtime_setting,
)
from app.runtime.state import SystemHelper
from app.runtime.version import get_app_version, get_frontend_version
from app.startup.composition.database import build_database_governance

BACKEND_RUNTIME_FILE = get_runtime_setting("TEMP_PATH") / "moviepilot.runtime.json"
BACKEND_STDIO_LOG_FILE = get_runtime_setting("LOG_PATH") / "moviepilot.stdout.log"
BACKEND_APP_LOG_FILE = get_runtime_setting("LOG_PATH") / "moviepilot.log"
FRONTEND_RUNTIME_FILE = get_runtime_setting("TEMP_PATH") / "moviepilot.frontend.runtime.json"
FRONTEND_STDIO_LOG_FILE = get_runtime_setting("LOG_PATH") / "moviepilot.frontend.stdout.log"
FRONTEND_DIR = get_runtime_setting("ROOT_PATH") / "public"
FRONTEND_SERVICE_FILE = FRONTEND_DIR / "service.js"
FRONTEND_VERSION_FILE = FRONTEND_DIR / "version.txt"
HEALTH_PATH = "/api/v1/system/global"
HEALTH_TOKEN = "moviepilot"
FRONTEND_HEALTH_PATH = "/version.txt"
LOCAL_HOSTS = {"0.0.0.0", "::", "::1", "", "localhost"}
MANAGED_ACTIVE_STATES = {"running", "starting"}
MASKED_FIELDS = {
    "API_TOKEN",
    "DB_POSTGRESQL_PASSWORD",
    "RESOURCE_SECRET_KEY",
    "SECRET_KEY",
    "SUPERUSER_PASSWORD",
}
MASKED_SUFFIXES = ("_TOKEN", "_PASSWORD", "_SECRET", "_API_KEY")
CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}
PREPARED_UPDATE_ROOT = get_runtime_setting("TEMP_PATH") / "moviepilot-update"
PREPARED_UPDATE_MANIFEST = PREPARED_UPDATE_ROOT / "install.json"
PREPARED_DOWNLOAD_MANIFEST = PREPARED_UPDATE_ROOT / "prepared.json"
PREPARED_UPDATE_STATE = PREPARED_UPDATE_ROOT / "state.json"


def _repo_root() -> Path:
    return get_runtime_setting("ROOT_PATH")


def _read_json_file(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_json_file(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _clear_json_file(path: Path) -> None:
    if path.exists():
        path.unlink()


def _get_process(runtime: Optional[Dict[str, Any]] = None) -> Optional[psutil.Process]:
    runtime = runtime or {}
    pid = runtime.get("pid")
    create_time = runtime.get("create_time")
    if not pid or create_time is None:
        return None

    try:
        process = psutil.Process(int(pid))
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        return None

    try:
        if abs(process.create_time() - float(create_time)) > 2:
            return None
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return None
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None

    return process


def _client_host(host: Optional[str]) -> str:
    host = (host or "").strip()
    if host in LOCAL_HOSTS:
        return "127.0.0.1"
    return host


def _backend_runtime() -> Optional[Dict[str, Any]]:
    return _read_json_file(BACKEND_RUNTIME_FILE)


def _frontend_runtime() -> Optional[Dict[str, Any]]:
    return _read_json_file(FRONTEND_RUNTIME_FILE)


def _backend_base_url(runtime: Optional[Dict[str, Any]] = None) -> str:
    runtime = runtime or _backend_runtime() or {}
    host = runtime.get("host") or get_runtime_setting("HOST")
    port = runtime.get("port") or get_runtime_setting("PORT")
    return f"http://{_client_host(host)}:{port}"


def _frontend_base_url(runtime: Optional[Dict[str, Any]] = None) -> str:
    runtime = runtime or _frontend_runtime() or {}
    host = runtime.get("host") or get_runtime_setting("HOST")
    port = runtime.get("port") or get_runtime_setting("NGINX_PORT")
    return f"http://{_client_host(host)}:{port}"


def _runtime_api_token(runtime: Optional[Dict[str, Any]] = None) -> str:
    runtime = runtime or _backend_runtime() or {}
    return runtime.get("api_token") or get_runtime_setting("API_TOKEN")


def _http_request(
    method: str,
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 5.0,
    runtime: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    url = f"{_backend_base_url(runtime)}{path}"
    if params:
        query = urlencode(params, doseq=True)
        url = f"{url}?{query}"

    body = None
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"

    request = Request(url=url, data=body, headers=request_headers, method=method.upper())
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return {
                "status": response.status,
                "json": json.loads(raw) if raw else None,
                "text": raw,
            }
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            data = None
        return {
            "status": exc.code,
            "json": data,
            "text": raw,
        }
    except URLError as exc:
        raise click.ClickException(f"无法连接到本地服务：{exc.reason}") from exc


def _backend_health(
    runtime: Optional[Dict[str, Any]] = None, timeout: float = 2.0
) -> tuple[bool, Optional[Dict[str, Any]]]:
    try:
        response = _http_request(
            "GET",
            HEALTH_PATH,
            params={"token": HEALTH_TOKEN},
            timeout=timeout,
            runtime=runtime,
        )
    except click.ClickException:
        return False, None

    payload = response.get("json")
    if response["status"] != 200 or not isinstance(payload, dict):
        return False, None
    if payload.get("success") is False:
        return False, payload
    return True, payload


def _frontend_health(
    runtime: Optional[Dict[str, Any]] = None, timeout: float = 2.0
) -> tuple[bool, Optional[Dict[str, Any]]]:
    runtime = runtime or _frontend_runtime() or {}
    url = f"{_frontend_base_url(runtime)}{FRONTEND_HEALTH_PATH}"
    request = Request(url=url, headers={"Accept": "text/plain"}, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace").strip()
            return response.status == 200, {"version": raw}
    except (HTTPError, URLError):
        return False, None


def _warn(message: str) -> None:
    click.secho(message, fg="yellow")


def _release_prefix(version: Optional[str]) -> str:
    """
    从版本号中提取主版本前缀，用于把本地自动更新限制在当前主版本线上。
    """
    matched = re.match(r"^(v\d+)", str(version or "").strip())
    return matched.group(1) if matched else "v2"


def _git_current_branch() -> Optional[str]:
    try:
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(_repo_root()),
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return branch or None


def _auto_update_mode() -> str:
    if SystemHelper.consume_one_shot_dev_update():
        return "dev"
    return str(get_runtime_setting("MOVIEPILOT_AUTO_UPDATE") or "").strip().lower()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mark_prepared_update_failed(message: str) -> None:
    state = _read_json_file(PREPARED_UPDATE_STATE) or {}
    state.update(
        {
            "state": "failed",
            "error": message,
            "can_update": True,
            "can_install": False,
        }
    )
    for item in state.get("updates", []):
        if isinstance(item, dict) and item.get("state") == "installing":
            item.update(
                {
                    "state": "failed",
                    "error": message,
                    "can_update": True,
                    "can_install": False,
                }
            )
    _write_json_file(PREPARED_UPDATE_STATE, state)
    _clear_json_file(PREPARED_UPDATE_MANIFEST)


def _prepared_update_targets(manifest: Dict[str, Any]) -> set[str]:
    """解析安装清单中的升级类型，并兼容旧版仅含主程序字段的清单。"""
    targets = {
        str(target)
        for target in manifest.get("targets", [])
        if str(target) in {"application", "resources"}
    }
    if not targets and manifest.get("backend_archive"):
        targets.add("application")
    return targets


def _validate_prepared_resource_files(manifest: Dict[str, Any]) -> Path:
    """校验完整资源包，并返回可供 local_setup 离线安装的目录。"""
    resource_files = manifest.get("resource_files")
    if not isinstance(resource_files, list) or not resource_files:
        raise RuntimeError("站点资源更新清单为空")
    resource_names = {
        str(resource.get("name") or "")
        for resource in resource_files
        if isinstance(resource, dict)
    }
    if "user.sites.v3.bin" not in resource_names or not any(
        name.startswith("sites.") for name in resource_names
    ):
        raise RuntimeError("站点资源更新清单不是完整资源包")
    resource_root = PREPARED_UPDATE_ROOT.resolve()
    resource_dir: Optional[Path] = None
    for resource in resource_files:
        if not isinstance(resource, dict):
            raise RuntimeError("站点资源更新清单格式无效")
        path = Path(str(resource.get("path") or ""))
        try:
            path.resolve().relative_to(resource_root)
        except ValueError as error:
            raise RuntimeError("站点资源文件路径不安全") from error
        if not path.is_file() or _file_sha256(path) != resource.get("sha256"):
            raise RuntimeError(f"站点资源文件校验失败：{resource.get('name')}")
        if resource_dir is None:
            resource_dir = path.parent
        elif path.parent != resource_dir:
            raise RuntimeError("站点资源文件必须位于同一完整资源目录")
    return resource_dir or PREPARED_UPDATE_ROOT / "resources"


def _consume_prepared_target(target: str) -> None:
    """从持久化下载清单移除已应用目标，保留另一类待安装制品。"""
    prepared = _read_json_file(PREPARED_DOWNLOAD_MANIFEST)
    if not prepared:
        return
    if target == "application":
        for key in (
            "version",
            "frontend_version",
            "backend_archive",
            "frontend_archive",
            "backend_sha256",
            "frontend_sha256",
        ):
            prepared.pop(key, None)
    elif target == "resources":
        for key in ("resource_package_version", "resource_files"):
            prepared.pop(key, None)
    else:
        raise ValueError(f"未知升级类型：{target}")
    targets = [
        value
        for value in prepared.get("targets", [])
        if value in {"application", "resources"} and value != target
    ]
    if targets:
        prepared["targets"] = targets
    else:
        prepared.pop("targets", None)
    if prepared.get("backend_archive") or prepared.get("resource_files"):
        _write_json_file(PREPARED_DOWNLOAD_MANIFEST, prepared)
    else:
        _clear_json_file(PREPARED_DOWNLOAD_MANIFEST)


def _local_update_env() -> dict[str, str]:
    """构造本地更新子进程使用的包缓存、代理和认证环境。"""
    update_env = os.environ.copy()
    package_cache_root = Path(
        update_env.get("PACKAGE_CACHE_ROOT", "").strip() or get_runtime_setting("PACKAGE_CACHE_PATH")
    )
    update_env.setdefault("PACKAGE_CACHE_ROOT", str(package_cache_root))
    update_env.setdefault("UV_CACHE_DIR", str(package_cache_root / "uv"))
    if get_runtime_setting("PIP_PROXY"):
        update_env["PIP_PROXY"] = get_runtime_setting("PIP_PROXY")
    if get_runtime_setting("PROXY_HOST"):
        update_env["PROXY_HOST"] = get_runtime_setting("PROXY_HOST")
        for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            update_env[key] = get_runtime_setting("PROXY_HOST")
    if get_runtime_setting("GITHUB_TOKEN"):
        update_env.setdefault("GITHUB_TOKEN", get_runtime_setting("GITHUB_TOKEN"))
    return update_env


def _apply_prepared_release_update() -> bool:
    """本地 CLI 重启时按清单顺序离线安装主程序和完整资源包。"""
    manifest = _read_json_file(PREPARED_UPDATE_MANIFEST)
    if not manifest:
        return False

    try:
        targets = _prepared_update_targets(manifest)
        if not targets:
            raise RuntimeError("更新包清单缺少升级类型")

        config_dir = str(get_runtime_setting("CONFIG_PATH"))
        if "application" in targets:
            version = str(manifest.get("version") or "").strip()
            frontend_version = str(manifest.get("frontend_version") or "").strip()
            backend_archive = Path(str(manifest.get("backend_archive") or ""))
            frontend_archive = Path(str(manifest.get("frontend_archive") or ""))
            if not version or not frontend_version:
                raise RuntimeError("更新包清单缺少主程序版本信息")
            if not backend_archive.is_file() or _file_sha256(backend_archive) != manifest.get("backend_sha256"):
                raise RuntimeError("后端更新包校验失败")
            if not frontend_archive.is_file() or _file_sha256(frontend_archive) != manifest.get("frontend_sha256"):
                raise RuntimeError("前端更新包校验失败")

            update_command = [
                sys.executable,
                str(_repo_root() / "scripts" / "local_setup.py"),
                "update",
                "all",
                "--ref",
                version,
                "--offline-backend",
                "--frontend-version",
                frontend_version,
                "--frontend-archive",
                str(frontend_archive),
                "--skip-resources",
                "--venv",
                str(_repo_root() / "venv"),
                "--config-dir",
                config_dir,
            ]
            click.echo(f"安装已下载并校验的 MoviePilot {version} 更新包")
            result = subprocess.run(
                update_command,
                cwd=str(_repo_root()),
                env=_local_update_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if result.returncode != 0:
                lines = [line for line in (result.stdout or "").splitlines() if line.strip()]
                detail = lines[-1] if lines else "未知错误"
                raise RuntimeError(detail)
            _consume_prepared_target("application")

        if "resources" in targets:
            resource_dir = _validate_prepared_resource_files(manifest)
            resource_command = [
                sys.executable,
                str(_repo_root() / "scripts" / "local_setup.py"),
                "install-resources",
                "--resource-dir",
                str(resource_dir),
                "--config-dir",
                config_dir,
            ]
            click.echo("安装已下载并校验的完整站点资源包")
            result = subprocess.run(
                resource_command,
                cwd=str(_repo_root()),
                env=_local_update_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if result.returncode != 0:
                lines = [line for line in (result.stdout or "").splitlines() if line.strip()]
                detail = lines[-1] if lines else "未知错误"
                raise RuntimeError(detail)
            _consume_prepared_target("resources")

        _clear_json_file(PREPARED_UPDATE_MANIFEST)
        click.echo("已下载的更新安装完成")
    except (OSError, RuntimeError, ValueError) as error:
        message = f"本地 Release 更新安装失败：{error}"
        _mark_prepared_update_failed(message)
        _warn(f"{message}，继续使用当前版本启动")
    return True


def _resolve_auto_update_targets(mode: str) -> Optional[str]:
    if mode != "dev":
        return None
    backend_prefix = _release_prefix(get_app_version())
    current_branch = _git_current_branch()
    backend_ref = "latest"
    if not current_branch or current_branch == "HEAD":
        # 从 release 模式切回 dev 时，detached HEAD 需要一个明确分支。
        backend_ref = backend_prefix
    return backend_ref


def _best_effort_auto_update() -> None:
    if _apply_prepared_release_update():
        return

    mode = _auto_update_mode()
    # Release 更新先在后台下载并经用户确认；这里只保留开发版分支跟踪。
    if mode != "dev":
        return

    try:
        backend_ref = _resolve_auto_update_targets(mode)
    except RuntimeError as exc:
        _warn(f"自动更新准备失败，继续使用当前版本启动：{exc}")
        return

    if not backend_ref:
        _warn("自动更新准备失败，未能解析当前主版本对应的远端版本，继续使用当前版本启动")
        return

    update_command = [
        sys.executable,
        str(_repo_root() / "scripts" / "local_setup.py"),
        "update",
        "all",
        "--ref",
        backend_ref,
        "--venv",
        str(_repo_root() / "venv"),
        "--config-dir",
        str(get_runtime_setting("CONFIG_PATH")),
    ]

    click.echo(f"检测到 MOVIEPILOT_AUTO_UPDATE={mode}，启动前执行本地自动更新")
    result = subprocess.run(
        update_command,
        cwd=str(_repo_root()),
        env=_local_update_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode == 0:
        click.echo("本地自动更新完成")
        return

    output_lines = [line for line in (result.stdout or "").splitlines() if line.strip()]
    tail = output_lines[-1] if output_lines else "未知错误"
    _warn(f"本地自动更新失败，继续使用当前版本启动：{tail}")


def _ensure_frontend_not_running_alone(timeout: int) -> None:
    """
    如果只检测到 CLI 管理的前端仍在运行，则先停掉它，再按统一顺序重启前后端。
    """
    backend_state, _, _, _ = _managed_backend_status()
    frontend_state, _, _, _ = _managed_frontend_status()
    if backend_state == "stopped" and frontend_state in MANAGED_ACTIVE_STATES:
        click.echo("检测到仅前端仍在运行，先停止前端后再整体启动")
        _stop_frontend_service(timeout=timeout, force=True)


def _managed_backend_status() -> tuple[
    str, Optional[Dict[str, Any]], Optional[psutil.Process], Optional[Dict[str, Any]]
]:
    runtime = _backend_runtime()
    process = _get_process(runtime)
    if process:
        healthy, health_payload = _backend_health(runtime=runtime)
        if healthy:
            return "running", runtime, process, health_payload
        return "starting", runtime, process, None

    if runtime:
        _clear_json_file(BACKEND_RUNTIME_FILE)

    healthy, health_payload = _backend_health()
    if healthy:
        return "running-unmanaged", None, None, health_payload
    return "stopped", None, None, None


def _managed_frontend_status() -> tuple[
    str, Optional[Dict[str, Any]], Optional[psutil.Process], Optional[Dict[str, Any]]
]:
    runtime = _frontend_runtime()
    process = _get_process(runtime)
    if process:
        healthy, health_payload = _frontend_health(runtime=runtime)
        if healthy:
            return "running", runtime, process, health_payload
        return "starting", runtime, process, None

    if runtime:
        _clear_json_file(FRONTEND_RUNTIME_FILE)

    healthy, health_payload = _frontend_health()
    if healthy:
        return "running-unmanaged", None, None, health_payload
    return "stopped", None, None, None


def _mask_value(key: str, value: Any, show_secrets: bool = False) -> Any:
    is_secret = key in MASKED_FIELDS or key.endswith(MASKED_SUFFIXES)
    if show_secrets or not is_secret:
        return value
    if value in (None, "", []):
        return value
    return "******"


def _format_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return "" if value is None else str(value)


def _field_default(field: Any) -> Any:
    default_factory = getattr(field, "default_factory", None)
    if default_factory is not None:
        try:
            return default_factory()
        except TypeError:
            return "(dynamic)"
    return getattr(field, "default", None)


def _annotation_name(annotation: Any) -> str:
    origin = get_origin(annotation)
    if origin is None:
        if hasattr(annotation, "__name__"):
            return annotation.__name__
        return str(annotation).replace("typing.", "")

    args = [arg for arg in get_args(annotation) if arg is not type(None)]
    if origin in {list, set, tuple}:
        inner = _annotation_name(args[0]) if args else "Any"
        return f"{origin.__name__}[{inner}]"
    if origin is dict:
        if len(args) >= 2:
            return f"dict[{_annotation_name(args[0])}, {_annotation_name(args[1])}]"
        return "dict"
    if str(origin).endswith("Union"):
        if len(args) == 1:
            return f"Optional[{_annotation_name(args[0])}]"
        return " | ".join(_annotation_name(arg) for arg in args)
    return str(annotation).replace("typing.", "")


def _tail_lines(path: Path, count: int) -> list[str]:
    if not path.exists():
        raise click.ClickException(f"日志文件不存在：{path}")
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        return [line.rstrip("\n") for line in deque(handle, maxlen=count)]


def _follow_file(path: Path) -> None:
    if not path.exists():
        raise click.ClickException(f"日志文件不存在：{path}")

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        handle.seek(0, os.SEEK_END)
        while True:
            line = handle.readline()
            if line:
                click.echo(line.rstrip("\n"))
                continue
            time.sleep(0.5)


def _print_json(value: Any) -> None:
    click.echo(json.dumps(value, ensure_ascii=False, indent=2))


def _parse_tool_result(result: Any) -> Any:
    if not isinstance(result, str):
        return result
    try:
        return json.loads(result)
    except json.JSONDecodeError:
        return result


def _tool_request_headers(runtime: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    api_token = _runtime_api_token(runtime)
    if not api_token:
        raise click.ClickException("本地配置中未找到 API_TOKEN，请先配置后再使用 tool/scheduler 命令")
    return {"X-API-KEY": api_token}


def _call_tool(tool_name: str, arguments: Dict[str, Any], runtime: Optional[Dict[str, Any]] = None) -> Any:
    response = _http_request(
        "POST",
        "/api/v1/mcp/tools/call",
        json_body={"tool_name": tool_name, "arguments": arguments},
        headers=_tool_request_headers(runtime),
        timeout=30.0,
        runtime=runtime,
    )
    payload = response.get("json") or {}
    if response["status"] not in {200, 201}:
        message = payload.get("error") or payload.get("detail") or response["text"] or "调用工具失败"
        raise click.ClickException(message)
    if not payload.get("success"):
        raise click.ClickException(payload.get("error") or "调用工具失败")
    return _parse_tool_result(payload.get("result"))


def _load_tool(tool_name: str, runtime: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    response = _http_request(
        "GET",
        f"/api/v1/mcp/tools/{tool_name}",
        headers=_tool_request_headers(runtime),
        timeout=10.0,
        runtime=runtime,
    )
    if response["status"] == 404:
        raise click.ClickException(f"工具不存在：{tool_name}")
    if response["status"] != 200 or not isinstance(response.get("json"), dict):
        raise click.ClickException(response["text"] or f"获取工具失败（HTTP {response['status']}）")
    return response["json"]


def _load_tools(runtime: Optional[Dict[str, Any]] = None) -> list[Dict[str, Any]]:
    response = _http_request(
        "GET",
        "/api/v1/mcp/tools",
        headers=_tool_request_headers(runtime),
        timeout=10.0,
        runtime=runtime,
    )
    if response["status"] != 200 or not isinstance(response.get("json"), list):
        raise click.ClickException(response["text"] or f"获取工具列表失败（HTTP {response['status']}）")
    return response["json"]


def _normalize_type(schema: Optional[Dict[str, Any]]) -> str:
    schema = schema or {}
    if schema.get("type"):
        return str(schema["type"])
    for item in schema.get("anyOf", []):
        if item and item.get("type") and item.get("type") != "null":
            return str(item["type"])
    return "string"


def _format_tool_detail(tool: Dict[str, Any]) -> None:
    click.echo(f"Command: {tool.get('name')}")
    click.echo(f"Description: {tool.get('description') or '(none)'}")
    click.echo("")

    properties = (tool.get("inputSchema") or {}).get("properties") or {}
    required = set((tool.get("inputSchema") or {}).get("required") or [])
    fields = []
    for name, schema in properties.items():
        fields.append(
            (
                f"{name}*" if name in required else name,
                _normalize_type(schema),
                schema.get("description") or "",
            )
        )

    if not fields:
        click.echo("Parameters: (none)")
    else:
        name_width = max(len(name) for name, _, _ in fields)
        type_width = max(len(field_type) for _, field_type, _ in fields)
        click.echo("Parameters:")
        for field_name, field_type, field_desc in fields:
            click.echo(f"  {field_name.ljust(name_width)}  {field_type.ljust(type_width)}  {field_desc}")


def _parse_tool_argument_value(value: str) -> Any:
    """解析工具参数中的结构化 JSON，同时保留普通字符串语义。"""
    stripped = value.strip()
    if (stripped.startswith("{") and stripped.endswith("}")) or (stripped.startswith("[") and stripped.endswith("]")):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError as error:
            raise click.ClickException(f"工具参数 JSON 格式错误：{error.msg}") from error
    if stripped in {"true", "false", "null"}:
        return json.loads(stripped)
    return value


def _parse_key_value_pairs(items: Iterable[str]) -> Dict[str, Any]:
    """解析 tool run 的 key=value 参数，并支持对象、数组和布尔 JSON。"""
    payload: Dict[str, Any] = {}
    for item in items:
        if "=" not in item:
            raise click.ClickException(f"参数必须是 key=value 形式：{item}")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise click.ClickException(f"参数名不能为空：{item}")
        payload[key] = _parse_tool_argument_value(value)
    return payload


def _unwrap_api_data(result: Any) -> Any:
    """从结构化 MoviePilot API 响应中提取 data。"""
    if not isinstance(result, dict) or "success" not in result:
        return result
    if not result.get("success"):
        raise click.ClickException(result.get("message") or "MoviePilot API 调用失败")
    return result.get("data")


def _ensure_local_api_token() -> bool:
    if get_runtime_setting("API_TOKEN") and len(str(get_runtime_setting("API_TOKEN")).strip()) >= 16:
        return False

    result, message = update_runtime_setting(
        "API_TOKEN", get_runtime_setting("API_TOKEN") or ""
    )
    if result is False:
        raise click.ClickException(message or "初始化 API_TOKEN 失败")
    return result is True


def _spawn_process(
    command: list[str],
    *,
    cwd: Path,
    log_file: Optional[Path],
    env: Optional[Dict[str, str]] = None,
) -> subprocess.Popen:
    kwargs: Dict[str, Any] = {
        "cwd": str(cwd),
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
        "env": env or os.environ.copy(),
    }
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_handle = log_file.open("a", encoding="utf-8")
        kwargs["stdout"] = log_handle
        kwargs["stderr"] = subprocess.STDOUT
    else:
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(command, **kwargs)


def _spawn_backend_process(*, safe: bool = False) -> subprocess.Popen:
    backend_env = {
        **os.environ,
        "PYTHONUNBUFFERED": "1",
        "MOVIEPILOT_DISABLE_CONSOLE_LOG": "1",
        "MOVIEPILOT_STDIO_LOG_FILE": str(BACKEND_STDIO_LOG_FILE),
        "MOVIEPILOT_STDIO_LOG_MAX_BYTES": str(max(int(get_runtime_setting("LOG_MAX_FILE_SIZE") or 0), 1) * 1024 * 1024),
        "MOVIEPILOT_STDIO_LOG_BACKUP_COUNT": str(max(int(get_runtime_setting("LOG_BACKUP_COUNT") or 0), 0)),
    }
    if safe:
        backend_env["MOVIEPILOT_SAFE_MODE"] = "true"

    return _spawn_process(
        [sys.executable, "-m", "app.main"],
        cwd=_repo_root(),
        log_file=None,
        env=backend_env,
    )


def _frontend_node_binary() -> Path:
    candidates = [
        _repo_root() / ".runtime" / "node" / "bin" / "node",
        _repo_root() / ".runtime" / "node" / "node.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate

    system_node = shutil.which("node")
    if system_node:
        return Path(system_node)

    raise click.ClickException("未找到可用的 Node 运行时，请先执行 `moviepilot install frontend` 或 `moviepilot setup`")


def _ensure_frontend_runtime() -> None:
    if not FRONTEND_SERVICE_FILE.exists():
        raise click.ClickException("未找到前端发布包，请先执行 `moviepilot install frontend` 或 `moviepilot setup`")
    if not (FRONTEND_DIR / "node_modules" / "express").exists():
        raise click.ClickException("前端运行依赖未安装，请重新执行 `moviepilot install frontend` 或 `moviepilot setup`")


def _spawn_frontend_process(backend_port: int) -> subprocess.Popen:
    _ensure_frontend_runtime()
    node_bin = _frontend_node_binary()
    return _spawn_process(
        [str(node_bin), str(FRONTEND_SERVICE_FILE)],
        cwd=FRONTEND_DIR,
        log_file=FRONTEND_STDIO_LOG_FILE,
        env={
            **os.environ,
            "PORT": str(backend_port),
            "NGINX_PORT": str(get_runtime_setting("NGINX_PORT")),
        },
    )


def _wait_until_backend_ready(runtime: Dict[str, Any], timeout: int) -> Dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        process = _get_process(runtime)
        if not process:
            lines = _tail_lines(BACKEND_STDIO_LOG_FILE, 20) if BACKEND_STDIO_LOG_FILE.exists() else []
            _clear_json_file(BACKEND_RUNTIME_FILE)
            detail = "\n".join(lines) if lines else "请查看后端日志文件排查问题。"
            raise click.ClickException(f"后端启动失败。\n{detail}")

        healthy, payload = _backend_health(runtime=runtime)
        if healthy:
            return payload or {}
        time.sleep(1)

    raise click.ClickException(
        f"后端进程已启动，但在 {timeout} 秒内未通过健康检查，请执行 `moviepilot logs --stdio` 查看启动日志"
    )


def _wait_until_frontend_ready(runtime: Dict[str, Any], timeout: int) -> Dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        process = _get_process(runtime)
        if not process:
            lines = _tail_lines(FRONTEND_STDIO_LOG_FILE, 20) if FRONTEND_STDIO_LOG_FILE.exists() else []
            _clear_json_file(FRONTEND_RUNTIME_FILE)
            detail = "\n".join(lines) if lines else "请查看前端日志文件排查问题。"
            raise click.ClickException(f"前端启动失败。\n{detail}")

        healthy, payload = _frontend_health(runtime=runtime)
        if healthy:
            return payload or {}
        time.sleep(1)

    raise click.ClickException(
        f"前端进程已启动，但在 {timeout} 秒内未通过健康检查，请执行 `moviepilot logs --frontend` 查看前端日志"
    )


def _start_backend_service(timeout: int, safe: bool = False) -> Dict[str, Any]:
    state, runtime, process, health_payload = _managed_backend_status()
    if state in {"running", "starting"} and runtime and process:
        return {"status": state, "runtime": runtime, "process": process, "health": health_payload, "started": False}
    if state == "running-unmanaged":
        raise click.ClickException(
            "检测到本地端口上已有 MoviePilot 后端正在运行，但不是由当前 CLI 管理，请先手动停止它"
        )

    _ensure_local_api_token()
    _clear_json_file(BACKEND_RUNTIME_FILE)
    process = _spawn_backend_process(safe=safe)
    ps_process = psutil.Process(process.pid)
    runtime = {
        "pid": process.pid,
        "create_time": ps_process.create_time(),
        "host": get_runtime_setting("HOST"),
        "port": get_runtime_setting("PORT"),
        "api_token": get_runtime_setting("API_TOKEN"),
        "started_at": int(time.time()),
        "python": sys.executable,
        "stdio_log": str(BACKEND_STDIO_LOG_FILE),
        "safe_mode": safe,
    }
    _write_json_file(BACKEND_RUNTIME_FILE, runtime)
    health_payload = _wait_until_backend_ready(runtime, timeout)
    return {"status": "running", "runtime": runtime, "process": ps_process, "health": health_payload, "started": True}


def _start_frontend_service(timeout: int, backend_port: int) -> Dict[str, Any]:
    state, runtime, process, health_payload = _managed_frontend_status()
    if state in {"running", "starting"} and runtime and process:
        return {"status": state, "runtime": runtime, "process": process, "health": health_payload, "started": False}
    if state == "running-unmanaged":
        raise click.ClickException(
            "检测到本地端口上已有 MoviePilot 前端正在运行，但不是由当前 CLI 管理，请先手动停止它"
        )

    _clear_json_file(FRONTEND_RUNTIME_FILE)
    process = _spawn_frontend_process(backend_port=backend_port)
    ps_process = psutil.Process(process.pid)
    runtime = {
        "pid": process.pid,
        "create_time": ps_process.create_time(),
        "host": get_runtime_setting("HOST"),
        "port": get_runtime_setting("NGINX_PORT"),
        "backend_port": backend_port,
        "started_at": int(time.time()),
        "node": str(_frontend_node_binary()),
        "stdio_log": str(FRONTEND_STDIO_LOG_FILE),
    }
    _write_json_file(FRONTEND_RUNTIME_FILE, runtime)
    health_payload = _wait_until_frontend_ready(runtime, timeout)
    return {"status": "running", "runtime": runtime, "process": ps_process, "health": health_payload, "started": True}


def _terminate_process(runtime_file: Path, timeout: int, force: bool, component_name: str) -> Dict[str, Any]:
    runtime = _read_json_file(runtime_file)
    process = _get_process(runtime)
    if not process:
        if runtime:
            _clear_json_file(runtime_file)
        return {"stopped": False}

    process.terminate()
    try:
        process.wait(timeout=timeout)
    except psutil.TimeoutExpired:
        if not force:
            raise click.ClickException(
                f"{component_name} 在 {timeout} 秒内没有退出，可重新执行 `moviepilot stop --force` 强制终止"
            )
        process.kill()
        process.wait(timeout=10)

    _clear_json_file(runtime_file)
    return {"stopped": True, "pid": process.pid}


def _stop_backend_service(timeout: int, force: bool) -> Dict[str, Any]:
    runtime = _backend_runtime()
    process = _get_process(runtime)
    if not process:
        if runtime:
            _clear_json_file(BACKEND_RUNTIME_FILE)
        healthy, _ = _backend_health()
        if healthy:
            raise click.ClickException("后端正在运行，但不是由当前 CLI 管理，出于安全原因未执行停止")
        return {"stopped": False}
    return _terminate_process(BACKEND_RUNTIME_FILE, timeout, force, "后端服务")


def _stop_frontend_service(timeout: int, force: bool) -> Dict[str, Any]:
    runtime = _frontend_runtime()
    process = _get_process(runtime)
    if not process:
        if runtime:
            _clear_json_file(FRONTEND_RUNTIME_FILE)
        healthy, _ = _frontend_health()
        if healthy:
            raise click.ClickException("前端正在运行，但不是由当前 CLI 管理，出于安全原因未执行停止")
        return {"stopped": False}
    return _terminate_process(FRONTEND_RUNTIME_FILE, timeout, force, "前端服务")


def _installed_frontend_version() -> Optional[str]:
    return get_frontend_version(fallback_to_declared=False)


@click.group(context_settings=CONTEXT_SETTINGS)
def cli() -> None:
    """MoviePilot 本地 CLI"""


def _format_backup_artifact(artifact: BackupArtifact) -> str:
    """将制品信息格式化为不含数据库凭据的单行 CLI 输出。"""
    return "\t".join(
        (
            artifact.name,
            artifact.db_type,
            artifact.created_at.isoformat(),
            str(artifact.size),
            str(artifact.path),
        )
    )


@cli.group(context_settings=CONTEXT_SETTINGS)
def database() -> None:
    """创建、列举、校验和离线还原本地数据库备份"""


@database.command("backup", context_settings=CONTEXT_SETTINGS)
def database_backup() -> None:
    """创建并验证一个在线数据库备份"""
    try:
        artifact = build_database_governance().create_backup()
    except Exception as error:
        raise click.ClickException(f"数据库备份失败：{error}") from error
    click.echo("name\tdb_type\tcreated_at\tsize\tpath")
    click.echo(_format_backup_artifact(artifact))


@database.command("list", context_settings=CONTEXT_SETTINGS)
def database_list() -> None:
    """列出当前受管目录中的正式数据库备份文件"""
    try:
        artifacts = build_database_governance().list_backups()
    except Exception as error:
        raise click.ClickException(f"数据库备份列表读取失败：{error}") from error
    if not artifacts:
        click.echo("暂无数据库备份")
        return
    click.echo("name\tdb_type\tcreated_at\tsize\tpath")
    for artifact in artifacts:
        click.echo(_format_backup_artifact(artifact))


@database.command("verify", context_settings=CONTEXT_SETTINGS)
@click.argument("name")
def database_verify(name: str) -> None:
    """按文件名重新校验一个数据库备份"""
    try:
        result = build_database_governance().verify_backup(name)
    except Exception as error:
        raise click.ClickException(f"数据库备份校验失败：{error}") from error
    if not result.valid:
        detail = f"：{result.detail}" if result.detail else ""
        raise click.ClickException(f"数据库备份校验未通过（{result.method}）{detail}")
    click.echo(f"数据库备份校验通过：name={name} method={result.method}")


@database.command("restore", context_settings=CONTEXT_SETTINGS)
@click.argument("name")
@click.option(
    "--confirm",
    is_flag=True,
    help="确认 MoviePilot 已停止，并允许覆盖当前数据库",
)
def database_restore(name: str, confirm: bool) -> None:
    """在 MoviePilot 停止运行时还原一个数据库备份"""
    if not confirm:
        raise click.ClickException("离线还原必须使用 --confirm 明确确认")
    try:
        artifact = build_database_governance().restore_backup(name)
    except Exception as error:
        raise click.ClickException(f"数据库还原失败：{error}") from error
    click.echo(f"数据库还原完成：{artifact.name}")


@cli.command(context_settings=CONTEXT_SETTINGS)
@click.option("--timeout", default=60, show_default=True, help="等待后端与前端就绪的秒数")
@click.option("--safe", is_flag=True, help="安全模式启动，仅保留核心 API，跳过插件和后台任务")
def start(timeout: int, safe: bool) -> None:
    """后台启动本地 MoviePilot 前后端服务"""
    _ensure_frontend_not_running_alone(timeout=min(timeout, 15))
    backend_state, _, _, _ = _managed_backend_status()
    frontend_state, _, _, _ = _managed_frontend_status()
    if backend_state == "stopped" and frontend_state == "stopped":
        _best_effort_auto_update()

    backend_result = _start_backend_service(timeout=timeout, safe=safe)
    backend_runtime = backend_result["runtime"]
    try:
        frontend_result = _start_frontend_service(timeout=timeout, backend_port=int(backend_runtime["port"]))
    except Exception:
        if backend_result.get("started"):
            try:
                _stop_backend_service(timeout=15, force=True)
            except click.ClickException:
                pass
        raise

    backend_health = backend_result.get("health") or {}
    backend_version = ((backend_health.get("data") or {}) if isinstance(backend_health, dict) else {}).get(
        "BACKEND_VERSION", get_app_version()
    )
    frontend_version = (
        ((frontend_result.get("health") or {}) if isinstance(frontend_result.get("health"), dict) else {}).get(
            "version"
        )
        or _installed_frontend_version()
        or "unknown"
    )

    click.echo(
        "MoviePilot 已启动"
        if backend_result.get("started") or frontend_result.get("started")
        else "MoviePilot 已在运行"
    )
    click.echo(f"Backend PID: {backend_result['process'].pid}")
    click.echo(f"Backend URL: {_backend_base_url(backend_runtime)}")
    click.echo(f"Frontend PID: {frontend_result['process'].pid}")
    click.echo(f"Frontend URL: {_frontend_base_url(frontend_result['runtime'])}")
    click.echo(f"Backend Version: {backend_version}")
    click.echo(f"Frontend Version: {frontend_version}")
    if safe or backend_runtime.get("safe_mode"):
        click.echo("Safe Mode: enabled")


@cli.command(context_settings=CONTEXT_SETTINGS)
@click.option("--timeout", default=30, show_default=True, help="等待服务退出的秒数")
@click.option("--force", is_flag=True, help="超时后强制结束进程")
def stop(timeout: int, force: bool) -> None:
    """停止本地 MoviePilot 前后端服务"""
    frontend_result = _stop_frontend_service(timeout=timeout, force=force)
    backend_result = _stop_backend_service(timeout=timeout, force=force)

    if not frontend_result.get("stopped") and not backend_result.get("stopped"):
        click.echo("MoviePilot 当前未运行")
        return
    if frontend_result.get("stopped"):
        click.echo(f"前端已停止 (PID: {frontend_result['pid']})")
    if backend_result.get("stopped"):
        click.echo(f"后端已停止 (PID: {backend_result['pid']})")


@cli.command(context_settings=CONTEXT_SETTINGS)
@click.option("--start-timeout", default=60, show_default=True, help="重启后等待服务就绪的秒数")
@click.option("--stop-timeout", default=30, show_default=True, help="停止服务时等待退出的秒数")
@click.option("--force", is_flag=True, help="停止超时后强制结束进程")
def restart(start_timeout: int, stop_timeout: int, force: bool) -> None:
    """重启本地 MoviePilot 前后端服务"""
    _stop_frontend_service(timeout=stop_timeout, force=force)
    _stop_backend_service(timeout=stop_timeout, force=force)
    _best_effort_auto_update()
    backend_result = _start_backend_service(timeout=start_timeout)
    frontend_result = _start_frontend_service(
        timeout=start_timeout, backend_port=int(backend_result["runtime"]["port"])
    )
    click.echo("MoviePilot 已重启")
    click.echo(f"Backend URL: {_backend_base_url(backend_result['runtime'])}")
    click.echo(f"Frontend URL: {_frontend_base_url(frontend_result['runtime'])}")


@cli.command(context_settings=CONTEXT_SETTINGS)
def status() -> None:
    """查看本地 MoviePilot 前后端服务状态"""
    backend_state, backend_runtime, backend_process, backend_health = _managed_backend_status()
    frontend_state, frontend_runtime, frontend_process, frontend_health = _managed_frontend_status()

    if backend_state == "stopped" and frontend_state == "stopped":
        click.echo("MoviePilot 未运行")
        installed_frontend = _installed_frontend_version()
        if installed_frontend:
            click.echo(f"已安装前端版本: {installed_frontend}")
        return

    click.echo("Backend:")
    if backend_state == "stopped":
        click.echo("  stopped")
    elif backend_state == "running-unmanaged":
        data = (backend_health or {}).get("data") or {}
        click.echo("  running (unmanaged)")
        click.echo(f"  URL: {_backend_base_url()}")
        click.echo(f"  Version: {data.get('BACKEND_VERSION', get_app_version())}")
    else:
        data = (backend_health or {}).get("data") or {}
        click.echo(f"  {'running' if backend_state == 'running' else 'starting'}")
        click.echo(f"  PID: {backend_process.pid}")
        click.echo(f"  URL: {_backend_base_url(backend_runtime)}")
        click.echo(f"  Version: {data.get('BACKEND_VERSION', get_app_version())}")
        click.echo(f"  App Log: {BACKEND_APP_LOG_FILE}")
        click.echo(f"  Stdout Log: {BACKEND_STDIO_LOG_FILE}")

    click.echo("Frontend:")
    if frontend_state == "stopped":
        click.echo("  stopped")
        installed_frontend = _installed_frontend_version()
        if installed_frontend:
            click.echo(f"  Installed Version: {installed_frontend}")
    elif frontend_state == "running-unmanaged":
        frontend_version = (
            ((frontend_health or {}).get("version") if isinstance(frontend_health, dict) else None)
            or _installed_frontend_version()
            or "unknown"
        )
        click.echo("  running (unmanaged)")
        click.echo(f"  URL: {_frontend_base_url()}")
        click.echo(f"  Version: {frontend_version}")
    else:
        frontend_version = (
            ((frontend_health or {}).get("version") if isinstance(frontend_health, dict) else None)
            or _installed_frontend_version()
            or "unknown"
        )
        click.echo(f"  {'running' if frontend_state == 'running' else 'starting'}")
        click.echo(f"  PID: {frontend_process.pid}")
        click.echo(f"  URL: {_frontend_base_url(frontend_runtime)}")
        click.echo(f"  Version: {frontend_version}")
        click.echo(f"  Stdout Log: {FRONTEND_STDIO_LOG_FILE}")


@cli.command(context_settings=CONTEXT_SETTINGS)
@click.option("--lines", default=50, show_default=True, help="显示末尾多少行")
@click.option("-f", "--follow", is_flag=True, help="持续跟随日志输出")
@click.option("--stdio", is_flag=True, help="查看后端启动标准输出日志而不是应用日志")
@click.option("--frontend", "frontend_log", is_flag=True, help="查看前端标准输出日志")
def logs(lines: int, follow: bool, stdio: bool, frontend_log: bool) -> None:
    """查看本地日志"""
    if stdio and frontend_log:
        raise click.ClickException("`--stdio` 与 `--frontend` 不能同时使用")

    if frontend_log:
        log_file = FRONTEND_STDIO_LOG_FILE
    elif stdio:
        log_file = BACKEND_STDIO_LOG_FILE
    else:
        log_file = BACKEND_APP_LOG_FILE

    for line in _tail_lines(log_file, lines):
        click.echo(line)
    if follow:
        _follow_file(log_file)


@cli.command(context_settings=CONTEXT_SETTINGS)
@click.option("--json", "json_output", is_flag=True, help="输出 JSON 报告")
@click.option("--fix", is_flag=True, help="执行白名单安全修复")
@click.option("--deep", is_flag=True, help="执行可能较慢的深度检查")
def doctor(json_output: bool, fix: bool, deep: bool) -> None:
    """离线诊断本地 MoviePilot 运行环境，插件日志告警不影响整体状态"""
    from app.doctor import run_doctor  # pylint: disable=no-name-in-module
    from app.doctor.formatters import format_json_report, format_text_report

    report = run_doctor(fix=fix, deep=deep)
    if json_output:
        click.echo(format_json_report(report))
    else:
        click.echo(format_text_report(report))
    raise click.exceptions.Exit(report.exit_code())


@cli.group(context_settings=CONTEXT_SETTINGS)
def config() -> None:
    """查看或修改本地配置"""


@config.command("path", context_settings=CONTEXT_SETTINGS)
def config_path() -> None:
    """显示配置路径"""
    config_path = get_runtime_setting("CONFIG_PATH")
    click.echo(f"Config Dir: {config_path}")
    click.echo(f"Env File: {config_path / 'app.env'}")
    click.echo(f"Frontend Dir: {FRONTEND_DIR}")


@config.command("list", context_settings=CONTEXT_SETTINGS)
@click.option("--show-secrets", is_flag=True, help="显示敏感配置原文")
def config_list(show_secrets: bool) -> None:
    """列出当前配置"""
    values = {key: get_runtime_setting(key) for key in Settings.model_fields}
    for key in sorted(values):
        click.echo(f"{key}={_format_value(_mask_value(key, values[key], show_secrets))}")


@config.command("get", context_settings=CONTEXT_SETTINGS)
@click.argument("key")
def config_get(key: str) -> None:
    """读取单个配置项"""
    setting_fields = Settings.model_fields.keys()
    if key not in setting_fields and not has_runtime_setting(key):
        raise click.ClickException(f"配置项不存在：{key}")
    click.echo(_format_value(get_runtime_setting(key)))


@config.command("set", context_settings=CONTEXT_SETTINGS)
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str) -> None:
    """写入单个配置项"""
    result, message = update_runtime_setting(key, value)
    if result is False:
        raise click.ClickException(message or f"配置项更新失败：{key}")
    if result is None:
        click.echo(f"{key} 未发生变化")
        return

    click.echo(f"{key} 已更新")
    if message:
        click.echo(message)

    backend_state, _, _, _ = _managed_backend_status()
    frontend_state, _, _, _ = _managed_frontend_status()
    if backend_state in {"running", "starting", "running-unmanaged"} or frontend_state in {
        "running",
        "starting",
        "running-unmanaged",
    }:
        click.echo("检测到服务正在运行，新配置将在重启前后端服务后生效")


@config.command("keys", context_settings=CONTEXT_SETTINGS)
@click.argument("pattern", required=False)
@click.option("--show-current", is_flag=True, help="同时显示当前值")
@click.option("--show-secrets", is_flag=True, help="显示敏感配置原文")
def config_keys(pattern: Optional[str], show_current: bool, show_secrets: bool) -> None:
    """列出所有可配置项及类型"""
    rows = []
    for key, field in Settings.model_fields.items():
        if pattern and pattern.lower() not in key.lower():
            continue
        default_value = _field_default(field)
        current_value = get_runtime_setting(key, default_value)
        rows.append(
            (
                key,
                _annotation_name(field.annotation),
                _format_value(_mask_value(key, default_value, show_secrets)),
                _format_value(_mask_value(key, current_value, show_secrets)),
            )
        )

    if not rows:
        raise click.ClickException("未找到匹配的配置项")

    key_width = max(len(row[0]) for row in rows)
    type_width = max(len(row[1]) for row in rows)
    for key, type_name, default_value, current_value in rows:
        line = f"{key.ljust(key_width)}  {type_name.ljust(type_width)}  default={default_value}"
        if show_current:
            line = f"{line}  current={current_value}"
        click.echo(line)


@config.command("describe", context_settings=CONTEXT_SETTINGS)
@click.argument("key")
@click.option("--show-secrets", is_flag=True, help="显示敏感配置原文")
def config_describe(key: str, show_secrets: bool) -> None:
    """显示单个配置项的类型、默认值和当前值"""
    field = Settings.model_fields.get(key)
    if not field:
        raise click.ClickException(f"配置项不存在：{key}")

    default_value = _field_default(field)
    current_value = get_runtime_setting(key, default_value)
    click.echo(f"Key: {key}")
    click.echo(f"Type: {_annotation_name(field.annotation)}")
    click.echo(f"Default: {_format_value(_mask_value(key, default_value, show_secrets))}")
    click.echo(f"Current: {_format_value(_mask_value(key, current_value, show_secrets))}")
    click.echo(f"Env File: {get_runtime_setting('CONFIG_PATH') / 'app.env'}")


@cli.group(context_settings=CONTEXT_SETTINGS)
def tool() -> None:
    """通过本地后端服务调用 MoviePilot 工具"""


@tool.command("list", context_settings=CONTEXT_SETTINGS)
def tool_list() -> None:
    """列出所有可用工具"""
    tools = _load_tools(runtime=_backend_runtime())
    for item in sorted(tools, key=lambda entry: entry.get("name", "")):
        click.echo(item.get("name"))


@tool.command("show", context_settings=CONTEXT_SETTINGS)
@click.argument("tool_name")
def tool_show(tool_name: str) -> None:
    """显示工具详情和参数"""
    tool_info = _load_tool(tool_name, runtime=_backend_runtime())
    _format_tool_detail(tool_info)


@tool.command("run", context_settings={**CONTEXT_SETTINGS, "ignore_unknown_options": True})
@click.argument("tool_name")
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def tool_run(tool_name: str, args: tuple[str, ...]) -> None:
    """运行指定工具"""
    arguments = _parse_key_value_pairs(args)
    result = _call_tool(tool_name, arguments, runtime=_backend_runtime())
    if isinstance(result, (dict, list)):
        _print_json(result)
    else:
        click.echo(result)


@cli.group(context_settings=CONTEXT_SETTINGS)
def scheduler() -> None:
    """查看或执行本地调度任务"""


@scheduler.command("list", context_settings=CONTEXT_SETTINGS)
def scheduler_list() -> None:
    """通过统一 API 网关列出调度任务。"""
    result = _unwrap_api_data(
        _call_tool(
            "moviepilot_api",
            {"operation_id": "scheduler.list"},
            runtime=_backend_runtime(),
        )
    )
    if isinstance(result, list):
        for item in result:
            click.echo(f"{item.get('id')}\t{item.get('status')}\t{item.get('next_run')}\t{item.get('name')}")
        return
    click.echo(result)


@scheduler.command("run", context_settings=CONTEXT_SETTINGS)
@click.argument("job_id")
def scheduler_run(job_id: str) -> None:
    """通过统一 API 网关立即执行某个调度任务。"""
    result = _unwrap_api_data(
        _call_tool(
            "moviepilot_api",
            {
                "operation_id": "scheduler.run",
                "query": {"job_id": job_id},
            },
            runtime=_backend_runtime(),
        )
    )
    if isinstance(result, (dict, list)):
        _print_json(result)
    else:
        click.echo(result)


@cli.command(context_settings=CONTEXT_SETTINGS)
def version() -> None:
    """显示版本信息"""
    click.echo(f"MoviePilot CLI: {get_app_version()}")

    healthy_backend, payload = _backend_health(runtime=_backend_runtime())
    if healthy_backend:
        data = (payload or {}).get("data") or {}
        click.echo(f"Backend Service: {data.get('BACKEND_VERSION', get_app_version())}")
    else:
        click.echo("Backend Service: not running")

    healthy_frontend, frontend_payload = _frontend_health(runtime=_frontend_runtime())
    if healthy_frontend:
        click.echo(f"Frontend Service: {(frontend_payload or {}).get('version') or 'unknown'}")
    else:
        click.echo("Frontend Service: not running")

    click.echo(f"Frontend Installed: {_installed_frontend_version() or 'not installed'}")


def main() -> None:
    cli(prog_name="moviepilot")


if __name__ == "__main__":
    main()
