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
from urllib.request import ProxyHandler, Request, build_opener, urlopen

import click
import psutil

from app.core.config import Settings, settings
from app.helper.system import SystemHelper
from version import APP_VERSION

BACKEND_RUNTIME_FILE = settings.TEMP_PATH / "moviepilot.runtime.json"
BACKEND_STDIO_LOG_FILE = settings.LOG_PATH / "moviepilot.stdout.log"
BACKEND_APP_LOG_FILE = settings.LOG_PATH / "moviepilot.log"
FRONTEND_RUNTIME_FILE = settings.TEMP_PATH / "moviepilot.frontend.runtime.json"
FRONTEND_STDIO_LOG_FILE = settings.LOG_PATH / "moviepilot.frontend.stdout.log"
FRONTEND_DIR = settings.ROOT_PATH / "public"
FRONTEND_SERVICE_FILE = FRONTEND_DIR / "service.js"
FRONTEND_VERSION_FILE = FRONTEND_DIR / "version.txt"
HEALTH_PATH = "/api/v1/system/global"
HEALTH_TOKEN = "moviepilot"
FRONTEND_HEALTH_PATH = "/version.txt"
BACKEND_RELEASES_API = "https://api.github.com/repos/jxxghp/MoviePilot/releases"
LOCAL_HOSTS = {"0.0.0.0", "::", "::1", "", "localhost"}
MANAGED_ACTIVE_STATES = {"running", "starting"}
AUTO_UPDATE_ENABLED_VALUES = {"true", "release", "dev"}
MASKED_FIELDS = {
    "API_TOKEN",
    "DB_POSTGRESQL_PASSWORD",
    "RESOURCE_SECRET_KEY",
    "SECRET_KEY",
    "SUPERUSER_PASSWORD",
}
MASKED_SUFFIXES = ("_TOKEN", "_PASSWORD", "_SECRET", "_API_KEY")
CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


def _repo_root() -> Path:
    return settings.ROOT_PATH


def _read_json_file(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
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
    host = runtime.get("host") or settings.HOST
    port = runtime.get("port") or settings.PORT
    return f"http://{_client_host(host)}:{port}"


def _frontend_base_url(runtime: Optional[Dict[str, Any]] = None) -> str:
    runtime = runtime or _frontend_runtime() or {}
    host = runtime.get("host") or settings.HOST
    port = runtime.get("port") or settings.NGINX_PORT
    return f"http://{_client_host(host)}:{port}"


def _runtime_api_token(runtime: Optional[Dict[str, Any]] = None) -> str:
    runtime = runtime or _backend_runtime() or {}
    return runtime.get("api_token") or settings.API_TOKEN


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
        raw = exc.read().decode("utf-8", errors="ignore")
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


def _backend_health(runtime: Optional[Dict[str, Any]] = None, timeout: float = 2.0) -> tuple[bool, Optional[Dict[str, Any]]]:
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


def _frontend_health(runtime: Optional[Dict[str, Any]] = None, timeout: float = 2.0) -> tuple[bool, Optional[Dict[str, Any]]]:
    runtime = runtime or _frontend_runtime() or {}
    url = f"{_frontend_base_url(runtime)}{FRONTEND_HEALTH_PATH}"
    request = Request(url=url, headers={"Accept": "text/plain"}, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="ignore").strip()
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


def _release_sort_key(tag: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", tag))


def _github_api_json(url: str, *, repo: str) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "MoviePilot-CLI",
    }
    headers.update(settings.REPO_GITHUB_HEADERS(repo))
    opener = build_opener(ProxyHandler(settings.PROXY or {}))
    request = Request(url=url, headers=headers, method="GET")

    try:
        with opener.open(request, timeout=10.0) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"访问 GitHub API 失败（HTTP {exc.code}）: {detail or url}") from exc
    except URLError as exc:
        raise RuntimeError(f"访问 GitHub API 失败：{exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"GitHub API 返回了无法解析的响应：{url}") from exc


def _latest_release_tag(url: str, *, repo: str, prefix: str) -> Optional[str]:
    payload = _github_api_json(url, repo=repo)
    if not isinstance(payload, list):
        raise RuntimeError(f"GitHub API 返回格式异常：{url}")

    matched_tags = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        tag_name = str(item.get("tag_name") or "").strip()
        if tag_name.startswith(f"{prefix}."):
            matched_tags.append(tag_name)

    if not matched_tags:
        return None
    return sorted(matched_tags, key=_release_sort_key)[-1]


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
    one_shot_mode = SystemHelper.consume_one_shot_update_mode()
    if one_shot_mode:
        return one_shot_mode
    return SystemHelper.get_auto_update_mode()


def _resolve_auto_update_targets(mode: str) -> Optional[str]:
    backend_prefix = _release_prefix(APP_VERSION)

    if mode == "dev":
        current_branch = _git_current_branch()
        backend_ref = "latest"
        if not current_branch or current_branch == "HEAD":
            # 从 release 模式切回 dev 时，detached HEAD 需要一个明确分支。
            backend_ref = backend_prefix
    else:
        backend_ref = _latest_release_tag(
            BACKEND_RELEASES_API,
            repo="jxxghp/MoviePilot",
            prefix=backend_prefix,
        )
    return backend_ref


def _best_effort_auto_update() -> None:
    mode = _auto_update_mode()
    if mode not in AUTO_UPDATE_ENABLED_VALUES:
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
        str(settings.CONFIG_PATH),
    ]

    update_env = os.environ.copy()
    if settings.PROXY_HOST:
        update_env.setdefault("http_proxy", settings.PROXY_HOST)
        update_env.setdefault("https_proxy", settings.PROXY_HOST)
        update_env.setdefault("HTTP_PROXY", settings.PROXY_HOST)
        update_env.setdefault("HTTPS_PROXY", settings.PROXY_HOST)
    if settings.GITHUB_TOKEN:
        update_env.setdefault("GITHUB_TOKEN", settings.GITHUB_TOKEN)

    click.echo(f"检测到 MOVIEPILOT_AUTO_UPDATE={mode}，启动前执行本地自动更新")
    result = subprocess.run(
        update_command,
        cwd=str(_repo_root()),
        env=update_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="ignore",
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


def _managed_backend_status() -> tuple[str, Optional[Dict[str, Any]], Optional[psutil.Process], Optional[Dict[str, Any]]]:
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


def _managed_frontend_status() -> tuple[str, Optional[Dict[str, Any]], Optional[psutil.Process], Optional[Dict[str, Any]]]:
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
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        return [line.rstrip("\n") for line in deque(handle, maxlen=count)]


def _follow_file(path: Path) -> None:
    if not path.exists():
        raise click.ClickException(f"日志文件不存在：{path}")

    with path.open("r", encoding="utf-8", errors="ignore") as handle:
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
        if name == "explanation":
            continue
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


def _parse_key_value_pairs(items: Iterable[str]) -> Dict[str, str]:
    payload: Dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise click.ClickException(f"参数必须是 key=value 形式：{item}")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise click.ClickException(f"参数名不能为空：{item}")
        payload[key] = value
    return payload


def _ensure_local_api_token() -> bool:
    if settings.API_TOKEN and len(str(settings.API_TOKEN).strip()) >= 16:
        return False

    result, message = settings.update_setting("API_TOKEN", settings.API_TOKEN or "")
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


def _spawn_backend_process() -> subprocess.Popen:
    return _spawn_process(
        [sys.executable, "-m", "app.main"],
        cwd=_repo_root(),
        log_file=None,
        env={
            **os.environ,
            "PYTHONUNBUFFERED": "1",
            "MOVIEPILOT_DISABLE_CONSOLE_LOG": "1",
            "MOVIEPILOT_STDIO_LOG_FILE": str(BACKEND_STDIO_LOG_FILE),
            "MOVIEPILOT_STDIO_LOG_MAX_BYTES": str(
                max(int(settings.LOG_MAX_FILE_SIZE or 0), 1) * 1024 * 1024
            ),
            "MOVIEPILOT_STDIO_LOG_BACKUP_COUNT": str(
                max(int(settings.LOG_BACKUP_COUNT or 0), 0)
            ),
        },
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
            "NGINX_PORT": str(settings.NGINX_PORT),
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

    raise click.ClickException(f"后端进程已启动，但在 {timeout} 秒内未通过健康检查，请执行 `moviepilot logs --stdio` 查看启动日志")


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

    raise click.ClickException(f"前端进程已启动，但在 {timeout} 秒内未通过健康检查，请执行 `moviepilot logs --frontend` 查看前端日志")


def _start_backend_service(timeout: int) -> Dict[str, Any]:
    state, runtime, process, health_payload = _managed_backend_status()
    if state in {"running", "starting"} and runtime and process:
        return {"status": state, "runtime": runtime, "process": process, "health": health_payload, "started": False}
    if state == "running-unmanaged":
        raise click.ClickException("检测到本地端口上已有 MoviePilot 后端正在运行，但不是由当前 CLI 管理，请先手动停止它")

    _ensure_local_api_token()
    _clear_json_file(BACKEND_RUNTIME_FILE)
    process = _spawn_backend_process()
    ps_process = psutil.Process(process.pid)
    runtime = {
        "pid": process.pid,
        "create_time": ps_process.create_time(),
        "host": settings.HOST,
        "port": settings.PORT,
        "api_token": settings.API_TOKEN,
        "started_at": int(time.time()),
        "python": sys.executable,
        "stdio_log": str(BACKEND_STDIO_LOG_FILE),
    }
    _write_json_file(BACKEND_RUNTIME_FILE, runtime)
    health_payload = _wait_until_backend_ready(runtime, timeout)
    return {"status": "running", "runtime": runtime, "process": ps_process, "health": health_payload, "started": True}


def _start_frontend_service(timeout: int, backend_port: int) -> Dict[str, Any]:
    state, runtime, process, health_payload = _managed_frontend_status()
    if state in {"running", "starting"} and runtime and process:
        return {"status": state, "runtime": runtime, "process": process, "health": health_payload, "started": False}
    if state == "running-unmanaged":
        raise click.ClickException("检测到本地端口上已有 MoviePilot 前端正在运行，但不是由当前 CLI 管理，请先手动停止它")

    _clear_json_file(FRONTEND_RUNTIME_FILE)
    process = _spawn_frontend_process(backend_port=backend_port)
    ps_process = psutil.Process(process.pid)
    runtime = {
        "pid": process.pid,
        "create_time": ps_process.create_time(),
        "host": settings.HOST,
        "port": settings.NGINX_PORT,
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
            raise click.ClickException(f"{component_name} 在 {timeout} 秒内没有退出，可重新执行 `moviepilot stop --force` 强制终止")
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
    if not FRONTEND_VERSION_FILE.exists():
        return None
    try:
        return FRONTEND_VERSION_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


@click.group(context_settings=CONTEXT_SETTINGS)
def cli() -> None:
    """MoviePilot 本地 CLI"""


@cli.command(context_settings=CONTEXT_SETTINGS)
@click.option("--timeout", default=60, show_default=True, help="等待后端与前端就绪的秒数")
def start(timeout: int) -> None:
    """后台启动本地 MoviePilot 前后端服务"""
    _ensure_frontend_not_running_alone(timeout=min(timeout, 15))
    backend_state, _, _, _ = _managed_backend_status()
    frontend_state, _, _, _ = _managed_frontend_status()
    if backend_state == "stopped" and frontend_state == "stopped":
        _best_effort_auto_update()

    backend_result = _start_backend_service(timeout=timeout)
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
    backend_version = ((backend_health.get("data") or {}) if isinstance(backend_health, dict) else {}).get("BACKEND_VERSION", APP_VERSION)
    frontend_version = ((frontend_result.get("health") or {}) if isinstance(frontend_result.get("health"), dict) else {}).get("version") or _installed_frontend_version() or "unknown"

    click.echo("MoviePilot 已启动" if backend_result.get("started") or frontend_result.get("started") else "MoviePilot 已在运行")
    click.echo(f"Backend PID: {backend_result['process'].pid}")
    click.echo(f"Backend URL: {_backend_base_url(backend_runtime)}")
    click.echo(f"Frontend PID: {frontend_result['process'].pid}")
    click.echo(f"Frontend URL: {_frontend_base_url(frontend_result['runtime'])}")
    click.echo(f"Backend Version: {backend_version}")
    click.echo(f"Frontend Version: {frontend_version}")


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
    frontend_result = _start_frontend_service(timeout=start_timeout, backend_port=int(backend_result["runtime"]["port"]))
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
        click.echo(f"  Version: {data.get('BACKEND_VERSION', APP_VERSION)}")
    else:
        data = (backend_health or {}).get("data") or {}
        click.echo(f"  {'running' if backend_state == 'running' else 'starting'}")
        click.echo(f"  PID: {backend_process.pid}")
        click.echo(f"  URL: {_backend_base_url(backend_runtime)}")
        click.echo(f"  Version: {data.get('BACKEND_VERSION', APP_VERSION)}")
        click.echo(f"  App Log: {BACKEND_APP_LOG_FILE}")
        click.echo(f"  Stdout Log: {BACKEND_STDIO_LOG_FILE}")

    click.echo("Frontend:")
    if frontend_state == "stopped":
        click.echo("  stopped")
        installed_frontend = _installed_frontend_version()
        if installed_frontend:
            click.echo(f"  Installed Version: {installed_frontend}")
    elif frontend_state == "running-unmanaged":
        frontend_version = ((frontend_health or {}).get("version") if isinstance(frontend_health, dict) else None) or _installed_frontend_version() or "unknown"
        click.echo("  running (unmanaged)")
        click.echo(f"  URL: {_frontend_base_url()}")
        click.echo(f"  Version: {frontend_version}")
    else:
        frontend_version = ((frontend_health or {}).get("version") if isinstance(frontend_health, dict) else None) or _installed_frontend_version() or "unknown"
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


@cli.group(context_settings=CONTEXT_SETTINGS)
def config() -> None:
    """查看或修改本地配置"""


@config.command("path", context_settings=CONTEXT_SETTINGS)
def config_path() -> None:
    """显示配置路径"""
    click.echo(f"Config Dir: {settings.CONFIG_PATH}")
    click.echo(f"Env File: {settings.CONFIG_PATH / 'app.env'}")
    click.echo(f"Frontend Dir: {FRONTEND_DIR}")


@config.command("list", context_settings=CONTEXT_SETTINGS)
@click.option("--show-secrets", is_flag=True, help="显示敏感配置原文")
def config_list(show_secrets: bool) -> None:
    """列出当前配置"""
    values = settings.model_dump()
    for key in sorted(values):
        click.echo(f"{key}={_format_value(_mask_value(key, values[key], show_secrets))}")


@config.command("get", context_settings=CONTEXT_SETTINGS)
@click.argument("key")
def config_get(key: str) -> None:
    """读取单个配置项"""
    if key not in Settings.model_fields and not hasattr(settings, key):
        raise click.ClickException(f"配置项不存在：{key}")
    click.echo(_format_value(getattr(settings, key)))


@config.command("set", context_settings=CONTEXT_SETTINGS)
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str) -> None:
    """写入单个配置项"""
    result, message = settings.update_setting(key, value)
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
    if backend_state in {"running", "starting", "running-unmanaged"} or frontend_state in {"running", "starting", "running-unmanaged"}:
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
        current_value = getattr(settings, key, default_value)
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
    current_value = getattr(settings, key, default_value)
    click.echo(f"Key: {key}")
    click.echo(f"Type: {_annotation_name(field.annotation)}")
    click.echo(f"Default: {_format_value(_mask_value(key, default_value, show_secrets))}")
    click.echo(f"Current: {_format_value(_mask_value(key, current_value, show_secrets))}")
    click.echo(f"Env File: {settings.CONFIG_PATH / 'app.env'}")


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
    arguments = {"explanation": "CLI invocation"}
    arguments.update(_parse_key_value_pairs(args))
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
    """列出调度任务"""
    result = _call_tool(
        "query_schedulers",
        {"explanation": "List scheduler jobs from local CLI"},
        runtime=_backend_runtime(),
    )
    if isinstance(result, list):
        for item in result:
            click.echo(f"{item.get('id')}\t{item.get('status')}\t{item.get('next_run')}\t{item.get('name')}")
        return
    click.echo(result)


@scheduler.command("run", context_settings=CONTEXT_SETTINGS)
@click.argument("job_id")
def scheduler_run(job_id: str) -> None:
    """立即执行某个调度任务"""
    result = _call_tool(
        "run_scheduler",
        {
            "explanation": "Run a scheduler job from local CLI",
            "job_id": job_id,
        },
        runtime=_backend_runtime(),
    )
    if isinstance(result, (dict, list)):
        _print_json(result)
    else:
        click.echo(result)


@cli.command(context_settings=CONTEXT_SETTINGS)
def version() -> None:
    """显示版本信息"""
    click.echo(f"MoviePilot CLI: {APP_VERSION}")

    healthy_backend, payload = _backend_health(runtime=_backend_runtime())
    if healthy_backend:
        data = (payload or {}).get("data") or {}
        click.echo(f"Backend Service: {data.get('BACKEND_VERSION', APP_VERSION)}")
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
