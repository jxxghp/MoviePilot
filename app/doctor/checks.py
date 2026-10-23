"""
诊断本机配置、进程、日志和数据完整性，并按显式选项执行安全修复或深度探测。
"""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import re
import socket
import sqlite3
import sys
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import psutil

from app.adapters.system.backup.database import verify_database_backup
from app.adapters.system.backup.files import BackupFiles
from app.adapters.system.host import SystemUtils
from app.doctor.models import DoctorFinding, DoctorFindingStatus, DoctorReport, DoctorSeverity
from app.foundation.url import URL_QUERY_CREDENTIAL_PATTERN, URL_USERINFO_CREDENTIAL_PATTERN
from app.runtime.settings import get_runtime_setting, update_runtime_setting
from app.runtime.topology import process_topology_issue

CheckFunc = Callable[["DoctorRunnerProtocol"], None]

CORE_DEPENDENCIES = (
    "alembic",
    "cloakbrowser",
    "fastapi",
    "pydantic",
    "pydantic_core",
    "pydantic_settings",
    "sqlalchemy",
    "starlette",
    "uvicorn",
)
LOCAL_HOSTS = {"", "0.0.0.0", "::", "::1", "localhost"}
BACKEND_HEALTH_PATH = "/api/v1/system/global"
BACKEND_HEALTH_TOKEN = "moviepilot"
BACKEND_HEALTH_TIMEOUT = 0.5
LOG_ERROR_PATTERNS = (
    re.compile(r"\btraceback\b", re.IGNORECASE),
    re.compile(r"\b(error|critical|exception)\b", re.IGNORECASE),
    re.compile(r"加载插件.+出错"),
    re.compile(r"数据库更新失败"),
)
LOG_RECORD_PATTERN = re.compile(
    r"(?:【(?:DEBUG|INFO|WARNING|ERROR|CRITICAL)】|(?:DEBUG|INFO|WARNING|ERROR|CRITICAL):)"
)
LOG_TIMESTAMP_PATTERN = re.compile(
    r"(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2})"
)
LOG_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
LOG_LOOKBACK_HOURS = 24
CONSOLE_LOGGER_PATTERN = re.compile(r"\[([^\]]+)]")
PLUGIN_ERROR_PATTERNS = (
    re.compile(r"(?:^|\s-\s)plugin\.py\s+-\s", re.IGNORECASE),
    re.compile(r"插件.+(?:出错|失败|异常|错误)"),
)
SENSITIVE_PATTERNS = (
    re.compile(r"(?i)(api[_-]?token|token|password|secret|cookie)(\s*[:=]\s*)[^\s&]+"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    # 站点 RSS 与接口地址的凭据常以 passkey、sign 等查询参数或连接串口令出现，参数名本身需要保留供定位。
    URL_QUERY_CREDENTIAL_PATTERN,
    URL_USERINFO_CREDENTIAL_PATTERN,
)


def _runtime_path(key: str) -> Path:
    """
    读取诊断使用的路径配置，并拒绝违反运行配置契约的值。

    :param key: 已声明为 Path 的运行配置名称
    :return: 对应的路径对象
    :raises TypeError: 配置提供方返回非路径值
    """
    value = get_runtime_setting(key)
    if not isinstance(value, Path):
        raise TypeError(f"运行配置 {key} 必须是 Path")
    return value


def _backend_runtime_file() -> Path:
    return _runtime_path('TEMP_PATH') / "moviepilot.runtime.json"


def _frontend_runtime_file() -> Path:
    return _runtime_path('TEMP_PATH') / "moviepilot.frontend.runtime.json"


def _backend_stdio_log_file() -> Path:
    return _runtime_path('LOG_PATH') / "moviepilot.stdout.log"


def _backend_app_log_file() -> Path:
    return _runtime_path('LOG_PATH') / "moviepilot.log"


def _frontend_stdio_log_file() -> Path:
    return _runtime_path('LOG_PATH') / "moviepilot.frontend.stdout.log"


class DoctorRunnerProtocol:
    """
    诊断检查使用的运行器接口，避免检查项依赖具体运行器实现。

    :ivar fix: 是否允许执行检查项明确支持的安全修复
    :ivar deep: 是否允许执行深度检查，例如连接配置的数据库端口
    :ivar report: 当前收集诊断发现的报告
    """

    fix: bool
    deep: bool
    report: DoctorReport

    def add(
        self,
        *,
        finding_id: str,
        severity: DoctorSeverity,
        status: DoctorFindingStatus,
        title: str,
        detail: str,
        recommendation: str,
        fixable: bool = False,
        fixed: bool = False,
        affects_report_status: bool = True,
        context: Optional[dict[str, Any]] = None,
    ) -> DoctorFinding:
        """
        添加诊断发现。

        :param finding_id: 诊断项稳定标识
        :param severity: 诊断严重级别
        :param status: 单项诊断状态
        :param title: 诊断项标题
        :param detail: 诊断详情
        :param recommendation: 处理建议
        :param fixable: 是否支持 Doctor 自动修复
        :param fixed: 本次运行是否已修复
        :param affects_report_status: 是否参与整体报告状态聚合
        :param context: 可选结构化上下文
        :return: 新增的诊断发现
        """
        raise NotImplementedError


def default_checks() -> list[CheckFunc]:
    """
    返回按运行路径、配置、进程、依赖、数据及日志顺序排列的默认检查项。

    :return: 检查函数列表；每个函数接收运行器并向其报告追加发现
    """
    return [
        _check_runtime_paths,
        _check_config,
        _check_process_topology,
        _check_processes_and_ports,
        _check_dependencies,
        _check_database,
        _check_frontend_assets,
        _check_logs,
        _check_docker,
        _check_safe_mode,
    ]


def _mask_text(text: str) -> str:
    """
    遮盖日志中的令牌、密码和 Cookie 等凭据，避免诊断报告泄露原文。
    """
    masked = text
    for pattern in SENSITIVE_PATTERNS:
        if pattern.groups >= 2:
            masked = pattern.sub(r"\1\2<REDACTED>", masked)
        else:
            masked = pattern.sub("<REDACTED>", masked)
    return masked


def _check_process_topology(runner: DoctorRunnerProtocol) -> None:
    """
    诊断 API worker 配置是否会复制全功能控制面。
    """
    issue = process_topology_issue(
        workers=get_runtime_setting('API_WORKERS'),
        safe_mode=get_runtime_setting('MOVIEPILOT_SAFE_MODE'),
    )
    context = {
        "api_workers": get_runtime_setting('API_WORKERS'),
        "safe_mode": get_runtime_setting('MOVIEPILOT_SAFE_MODE'),
    }
    if issue:
        runner.add(
            finding_id="startup.process_topology",
            severity=DoctorSeverity.Error,
            status=DoctorFindingStatus.Failed,
            title="进程拓扑不受支持",
            detail=issue,
            recommendation="将 API_WORKERS 设为 1 后重启 MoviePilot。",
            context=context,
        )
        return
    if get_runtime_setting('API_WORKERS') != 1:
        runner.add(
            finding_id="startup.process_topology",
            severity=DoctorSeverity.Warn,
            status=DoctorFindingStatus.Degraded,
            title="安全模式临时使用多 worker",
            detail="安全模式不会启动插件、调度器、监控器和工作流，因此当前不会复制完整控制面。",
            recommendation="故障排除后恢复 API_WORKERS=1，再退出安全模式。",
            context=context,
        )
        return
    runner.add(
        finding_id="startup.process_topology",
        severity=DoctorSeverity.Info,
        status=DoctorFindingStatus.Ok,
        title="进程拓扑受支持",
        detail="API_WORKERS=1，插件和后台任务只会启动一份。",
        recommendation="保持单 worker；扩容前需要先拆分 API 数据面和控制面。",
        context=context,
    )


def _read_json(path: Path) -> Optional[dict[str, Any]]:
    """
    读取运行时 JSON 对象；文件缺失、无法读取、内容损坏或根值不是对象时返回 None。
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _runtime_process(runtime: Optional[dict[str, Any]]) -> Optional[psutil.Process]:
    """
    按进程号和创建时间核对运行时记录，防止复用的进程号被误认。

    创建时间允许两秒偏差；记录不完整、进程终止或无权限查询时返回 None。
    """
    runtime = runtime or {}
    pid = runtime.get("pid")
    create_time = runtime.get("create_time")
    if not pid or create_time is None:
        return None
    try:
        process = psutil.Process(int(pid))
        if abs(process.create_time() - float(create_time)) > 2:
            return None
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return None
        return process
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, ValueError):
        return None


def _process_description(process: psutil.Process) -> str:
    """
    生成包含进程号、名称及前四段命令行的描述；无权限读取的部分使用占位值。
    """
    try:
        name = process.name()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        name = "unknown"
    try:
        command = " ".join(process.cmdline()[:4])
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        command = ""
    suffix = f" {command}" if command else ""
    return f"PID {process.pid} ({name}){suffix}"


def _process_name_and_command(process: psutil.Process) -> tuple[str, str]:
    """
    读取小写进程名与完整命令行；进程消失或无权限读取的部分返回空字符串。
    """
    try:
        name = process.name().lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        name = ""
    try:
        command = " ".join(process.cmdline()).lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        command = ""
    return name, command


def _is_expected_port_process(name: str, process: psutil.Process) -> bool:
    """
    通过命令行和进程名判断端口占用者是否符合前端或后端的启动特征。
    """
    process_name, command = _process_name_and_command(process)
    if name == "backend":
        return "app/main.py" in command or "-m app.main" in command or "uvicorn" in command
    if name == "frontend":
        return (
            "nginx" in process_name
            or "service.js" in command
            or "node" in process_name
        )
    return False


def _port_occupants(port: int) -> list[psutil.Process]:
    """
    列出监听指定网络端口的进程并按进程号去重；无权限枚举时返回空列表。
    """
    occupants: dict[int, psutil.Process] = {}
    try:
        connections = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, OSError):
        return []
    for conn in connections:
        local = conn.laddr
        if not local or getattr(local, "port", None) != port:
            continue
        if conn.status != psutil.CONN_LISTEN:
            continue
        if not conn.pid:
            continue
        try:
            occupants[conn.pid] = psutil.Process(conn.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return list(occupants.values())


def _client_host(host: Optional[str]) -> str:
    """
    把本机通配监听地址转换为 IPv4 回环地址，其他主机名保持去空白后的值。
    """
    host = (host or "").strip()
    return "127.0.0.1" if host in LOCAL_HOSTS else host


def _can_connect(host: str, port: int, timeout: float = 1.0) -> tuple[bool, str]:
    """
    尝试建立并立即关闭 TCP 连接，返回连通结果与失败原因。

    :param host: 配置的主机名，本机通配地址按回环地址连接
    :param port: 目标端口
    :param timeout: 建立连接的超时秒数
    :return: 是否连通及错误文本；成功时错误文本为空
    """
    try:
        with socket.create_connection((_client_host(host), int(port)), timeout=timeout):
            return True, ""
    except OSError as err:
        return False, str(err)


def _is_moviepilot_backend_payload(payload: Any) -> bool:
    """
    判断本地健康接口响应是否来自 MoviePilot 后端。
    """
    if not isinstance(payload, dict) or payload.get("success") is False:
        return False
    data = payload.get("data")
    if not isinstance(data, dict):
        return False
    return bool(data.get("BACKEND_VERSION"))


def _backend_health_payload(port: int, timeout: float = BACKEND_HEALTH_TIMEOUT) -> Optional[dict[str, Any]]:
    """
    读取本机后端健康接口响应，用于识别非 CLI 管理的 MoviePilot 进程。
    """
    query = urlencode({"token": BACKEND_HEALTH_TOKEN})
    url = f"http://{_client_host(get_runtime_setting('HOST'))}:{port}{BACKEND_HEALTH_PATH}?{query}"
    request = Request(url=url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return None
            raw = response.read().decode("utf-8", errors="replace")
    except (HTTPError, URLError):
        return None
    except OSError:
        return None

    try:
        payload = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return None
    return payload if _is_moviepilot_backend_payload(payload) else None


def _tail_lines(path: Path, max_lines: int = 120, max_bytes: int = 256 * 1024) -> list[str]:
    """
    在字节数和行数限制内读取日志尾部，并遮盖敏感信息。

    文件无法读取时返回空列表；解码遇到非法字节时使用替代字符。
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as file_obj:
            if size > max_bytes:
                file_obj.seek(size - max_bytes)
            text = file_obj.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return list(deque((_mask_text(line) for line in text.splitlines()), maxlen=max_lines))


def _parse_log_timestamp(line: str) -> Optional[datetime]:
    """
    从日志行前 96 个字符提取秒级时间；未匹配或日期无效时返回 None。
    """
    match = LOG_TIMESTAMP_PATTERN.search(line[:96])
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), LOG_TIMESTAMP_FORMAT)
    except ValueError:
        return None


def _recent_log_lines(
    lines: list[str],
    now: Optional[datetime] = None,
) -> list[str]:
    """
    按日志记录边界保留诊断时间窗内的日志。

    没有可识别时间戳时保留原始输入；有时间戳时，续行跟随上一条记录决定是否保留。

    :param lines: 按原始顺序读取的日志行
    :param now: 计算时间窗的当前时间；省略时使用本机当前时间
    :return: 最近 24 小时记录及其续行，或无法识别时间时的完整输入
    """
    if not lines:
        return []
    timestamps = [_parse_log_timestamp(line) for line in lines]
    if not any(timestamps):
        return lines

    cutoff = (now or datetime.now()) - timedelta(hours=LOG_LOOKBACK_HOURS)
    recent: list[str] = []
    include_record = False
    for line, timestamp in zip(lines, timestamps):
        if timestamp is not None:
            include_record = timestamp >= cutoff
        if include_record:
            recent.append(line)
    return recent


def _error_fingerprint(line: str) -> str:
    """
    生成跨主日志、控制台镜像和插件独立日志可比较的错误指纹。
    """
    normalized = LOG_TIMESTAMP_PATTERN.sub("<time>", line.strip())
    if " - " in normalized:
        normalized = normalized.rsplit(" - ", 1)[-1]
    normalized = re.sub(
        r"^(?:【(?:ERROR|CRITICAL|WARNING)】|(?:ERROR|CRITICAL|WARNING):)\s*",
        "",
        normalized,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", normalized).strip().lower()


def _aggregate_log_entries(
    entries: list[tuple[Path, str]],
    max_matches: int = 12,
) -> tuple[list[str], list[str]]:
    """
    合并跨日志的重复错误，并返回详情行和来源文件。
    """
    unique_entries: dict[str, dict[str, Any]] = {}
    log_files: list[str] = []
    for path, line in entries:
        path_text = str(path)
        if path_text not in log_files:
            log_files.append(path_text)
        fingerprint = _error_fingerprint(line)
        if fingerprint not in unique_entries:
            unique_entries[fingerprint] = {"line": line, "sources": []}
        sources = unique_entries[fingerprint]["sources"]
        if path.name not in sources:
            sources.append(path.name)

    details = [
        f"[{', '.join(item['sources'])}] {item['line']}"
        for item in list(unique_entries.values())[-max_matches:]
    ]
    return details, log_files


def _find_error_lines(lines: list[str], max_matches: int = 12) -> list[str]:
    """
    从近期日志中提取错误关键词命中的行。
    """
    matches: list[str] = []
    for line in lines:
        if any(pattern.search(line) for pattern in LOG_ERROR_PATTERNS):
            matches.append(line)
    return matches[-max_matches:]


def _partition_error_lines(
    lines: list[str],
    plugin_logger_names: set[str],
    max_matches: int = 12,
) -> tuple[list[str], list[str]]:
    """
    将主日志错误线索拆分为核心错误和插件子系统错误。

    :param lines: 近期日志行
    :param plugin_logger_names: 已发现的插件控制台 logger 名称
    :param max_matches: 每类最多保留的错误行数
    :return: 核心错误行和插件错误行
    """
    core_matches: list[str] = []
    plugin_matches: list[str] = []
    plugin_context = False
    for line in lines:
        if LOG_RECORD_PATTERN.search(line):
            logger_match = CONSOLE_LOGGER_PATTERN.search(line)
            console_logger = logger_match.group(1).strip().lower() if logger_match else ""
            plugin_context = (
                console_logger in plugin_logger_names
                or any(pattern.search(line) for pattern in PLUGIN_ERROR_PATTERNS)
            )
        if not any(pattern.search(line) for pattern in LOG_ERROR_PATTERNS):
            continue
        if plugin_context or any(pattern.search(line) for pattern in PLUGIN_ERROR_PATTERNS):
            plugin_matches.append(line)
        else:
            core_matches.append(line)
    return core_matches[-max_matches:], plugin_matches[-max_matches:]


def _frontend_dir() -> Path:
    """
    优先使用程序目录下已存在的 public 目录，否则按绝对或相对配置解析前端路径。
    """
    root = _runtime_path('ROOT_PATH')
    root_public = root / "public"
    configured = Path(get_runtime_setting('FRONTEND_PATH'))
    if root_public.exists():
        return root_public
    if configured.is_absolute():
        return configured
    return root / configured


def _unlink_if_requested(runner: DoctorRunnerProtocol, path: Path) -> bool:
    """
    仅在启用修复时删除指定文件；未启用或删除失败均返回 False。
    """
    if not runner.fix:
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


def _check_runtime_paths(runner: DoctorRunnerProtocol) -> None:
    """
    记录当前程序、配置、日志和解释器路径，帮助定位配置目录选择错误。
    """
    runner.add(
        finding_id="runtime.paths",
        severity=DoctorSeverity.Info,
        status=DoctorFindingStatus.Ok,
        title="运行路径已识别",
        detail=(
            f"程序目录：{get_runtime_setting('ROOT_PATH')}；配置目录：{get_runtime_setting('CONFIG_PATH')}；"
            f"日志目录：{get_runtime_setting('LOG_PATH')}；Python：{sys.executable}"
        ),
        recommendation="如需切换配置目录，请使用 CONFIG_DIR 或本地 CLI 的 --config-dir 参数。",
        context={
            "root_path": str(get_runtime_setting('ROOT_PATH')),
            "config_path": str(get_runtime_setting('CONFIG_PATH')),
            "log_path": str(get_runtime_setting('LOG_PATH')),
            "python": sys.executable,
        },
    )


def _check_config(runner: DoctorRunnerProtocol) -> None:
    """
    检查令牌、端口和代理配置，仅在允许且没有环境变量覆盖时尝试修复令牌。

    诊断详情不包含令牌原文；代理格式异常作为警告，不阻止其他检查。
    """
    token = (get_runtime_setting('API_TOKEN') or "").strip()
    if len(token) < 16:
        fixed = False
        detail = "API_TOKEN 未设置或长度小于 16 个字符，后端鉴权和本地工具调用可能不可用。"
        if runner.fix and "API_TOKEN" not in os.environ:
            result, message = update_runtime_setting("API_TOKEN", token)
            fixed = result is True
            if message:
                detail = f"{detail} {message}"
        runner.add(
            finding_id="config.api_token_invalid",
            severity=DoctorSeverity.Error if not fixed else DoctorSeverity.Info,
            status=DoctorFindingStatus.Fixed if fixed else DoctorFindingStatus.Failed,
            title="API_TOKEN 不可用",
            detail=detail,
            recommendation=(
                "执行 `moviepilot doctor --fix` 自动生成安全 token，或使用 "
                "`moviepilot config set API_TOKEN <token>` 手动设置。"
            ),
            fixable="API_TOKEN" not in os.environ,
            fixed=fixed,
        )
    else:
        runner.add(
            finding_id="config.api_token",
            severity=DoctorSeverity.Info,
            status=DoctorFindingStatus.Ok,
            title="API_TOKEN 已配置",
            detail="API_TOKEN 长度满足本地工具和后端鉴权要求，报告不会输出 token 原文。",
            recommendation="无需处理。",
        )

    if get_runtime_setting('PORT') == get_runtime_setting('NGINX_PORT'):
        runner.add(
            finding_id="config.port_same",
            severity=DoctorSeverity.Error,
            status=DoctorFindingStatus.Failed,
            title="前后端端口冲突",
            detail=f"PORT 与 NGINX_PORT 都设置为 {get_runtime_setting('PORT')}。",
            recommendation="将 PORT 或 NGINX_PORT 调整为不同端口后重启服务。",
        )
    else:
        runner.add(
            finding_id="config.ports",
            severity=DoctorSeverity.Info,
            status=DoctorFindingStatus.Ok,
            title="前后端端口配置不同",
            detail=f"后端端口 PORT={get_runtime_setting('PORT')}；前端端口 NGINX_PORT={get_runtime_setting('NGINX_PORT')}。",
            recommendation="无需处理。",
        )

    proxy_host = (get_runtime_setting('PROXY_HOST') or "").strip()
    if proxy_host and not re.match(r"^(https?|socks5h?)://", proxy_host, re.IGNORECASE):
        runner.add(
            finding_id="config.proxy_format",
            severity=DoctorSeverity.Warn,
            status=DoctorFindingStatus.Degraded,
            title="代理地址格式可能不完整",
            detail=f"PROXY_HOST={proxy_host} 未包含 http:// 或 https:// 或 socks5:// 或 socks5h:// 前缀。",
            recommendation="如果外部访问异常，请把 PROXY_HOST 调整为完整 URL。",
        )


def _check_runtime_file(
    runner: DoctorRunnerProtocol,
    *,
    name: str,
    path: Path,
    port: int,
) -> Optional[psutil.Process]:
    """
    核对运行时文件与进程身份，按修复选项清理过期文件并记录结果。

    :return: 与记录匹配且仍在运行的进程；不存在或无法确认时返回 None
    """
    runtime = _read_json(path)
    process = _runtime_process(runtime)
    if runtime and not process:
        fixed = _unlink_if_requested(runner, path)
        runner.add(
            finding_id=f"runtime.{name}_stale",
            severity=DoctorSeverity.Warn if not fixed else DoctorSeverity.Info,
            status=DoctorFindingStatus.Fixed if fixed else DoctorFindingStatus.Degraded,
            title=f"{name} 运行时文件已过期",
            detail=f"{path} 指向的进程不存在或已不是原进程。",
            recommendation="执行 `moviepilot doctor --fix` 清理过期运行时文件后再重试启动。",
            fixable=True,
            fixed=fixed,
            context={"runtime_file": str(path)},
        )
    if process:
        runner.add(
            finding_id=f"process.{name}_managed",
            severity=DoctorSeverity.Info,
            status=DoctorFindingStatus.Ok,
            title=f"{name} 进程正在运行",
            detail=_process_description(process),
            recommendation="如服务不可访问，请继续查看端口和日志诊断项。",
            context={"pid": process.pid, "port": port},
        )
    return process


def _check_port(
    runner: DoctorRunnerProtocol,
    *,
    name: str,
    port: int,
    managed_process: Optional[psutil.Process],
) -> None:
    """
    区分受管服务、非命令行托管服务和其他端口占用者，并记录诊断发现。

    无法从进程特征识别后端时，再通过本机健康接口确认，避免误报端口冲突。
    """
    occupants = _port_occupants(port)
    if not occupants:
        runner.add(
            finding_id=f"port.{name}_not_listening",
            severity=DoctorSeverity.Warn,
            status=DoctorFindingStatus.Degraded,
            title=f"{name} 端口未监听",
            detail=f"本机未检测到进程监听端口 {port}。",
            recommendation="如果服务应当正在运行，请查看启动日志；如果尚未启动，可忽略。",
            context={"port": port},
        )
        return

    descriptions = [_process_description(process) for process in occupants]
    if managed_process and any(process.pid == managed_process.pid for process in occupants):
        runner.add(
            finding_id=f"port.{name}_listening",
            severity=DoctorSeverity.Info,
            status=DoctorFindingStatus.Ok,
            title=f"{name} 端口监听正常",
            detail=f"端口 {port} 由 MoviePilot 管理进程监听：{'; '.join(descriptions)}",
            recommendation="无需处理。",
            context={"port": port, "pids": [process.pid for process in occupants]},
        )
        return

    expected_processes = [process for process in occupants if _is_expected_port_process(name, process)]
    if expected_processes:
        runner.add(
            finding_id=f"port.{name}_listening_unmanaged",
            severity=DoctorSeverity.Info,
            status=DoctorFindingStatus.Ok,
            title=f"{name} 端口由 MoviePilot 相关进程监听",
            detail=f"端口 {port} 监听进程：{'; '.join(_process_description(process) for process in expected_processes)}",
            recommendation="如果这是 Docker 或非 CLI 管理启动方式，可忽略 runtime 文件缺失。",
            context={"port": port, "pids": [process.pid for process in expected_processes]},
        )
        return

    if name == "backend":
        health_payload = _backend_health_payload(port)
        if health_payload:
            runner.add(
                finding_id=f"port.{name}_listening_unmanaged",
                severity=DoctorSeverity.Info,
                status=DoctorFindingStatus.Ok,
                title=f"{name} 端口由 MoviePilot 后端监听",
                detail=f"端口 {port} 健康接口响应正常；监听进程：{'; '.join(descriptions)}",
                recommendation="Docker 或非 CLI 管理启动方式下，后端端口被当前服务监听属于正常状态。",
                context={
                    "port": port,
                    "pids": [process.pid for process in occupants],
                    "backend_version": health_payload.get("data", {}).get("BACKEND_VERSION"),
                },
            )
            return

    runner.add(
        finding_id=f"port.{name}_occupied",
        severity=DoctorSeverity.Error,
        status=DoctorFindingStatus.Failed,
        title=f"{name} 端口被其他进程占用",
        detail=f"端口 {port} 当前监听进程：{'; '.join(descriptions)}",
        recommendation="停止占用进程，或修改 MoviePilot 的端口配置后重启。",
        context={"port": port, "pids": [process.pid for process in occupants]},
    )


def _check_processes_and_ports(runner: DoctorRunnerProtocol) -> None:
    """
    分别核对前后端运行时记录与监听端口，不启动或终止进程。
    """
    backend_process = _check_runtime_file(
        runner,
        name="backend",
        path=_backend_runtime_file(),
        port=int(get_runtime_setting('PORT')),
    )
    frontend_process = _check_runtime_file(
        runner,
        name="frontend",
        path=_frontend_runtime_file(),
        port=int(get_runtime_setting('NGINX_PORT')),
    )
    _check_port(runner, name="backend", port=int(get_runtime_setting('PORT')), managed_process=backend_process)
    _check_port(runner, name="frontend", port=int(get_runtime_setting('NGINX_PORT')), managed_process=frontend_process)


def _check_dependencies(runner: DoctorRunnerProtocol) -> None:
    """
    检查核心 Python 模块是否可被导入系统定位，并报告缺失项。
    """
    missing = [name for name in CORE_DEPENDENCIES if importlib.util.find_spec(name) is None]
    if missing:
        runner.add(
            finding_id="dependencies.core_missing",
            severity=DoctorSeverity.Error,
            status=DoctorFindingStatus.Failed,
            title="核心 Python 依赖缺失",
            detail=f"无法导入：{', '.join(missing)}。",
            recommendation="本地环境执行 `moviepilot install deps`；Docker 环境建议重新拉取或重建镜像。",
            context={"missing": missing},
        )
        return
    runner.add(
        finding_id="dependencies.core",
        severity=DoctorSeverity.Info,
        status=DoctorFindingStatus.Ok,
        title="核心 Python 依赖可导入",
        detail=f"已检查：{', '.join(CORE_DEPENDENCIES)}。",
        recommendation="无需处理。",
    )


def _check_sqlite_database(runner: DoctorRunnerProtocol) -> None:
    """
    以只读连接执行 SQLite 完整性检查，不创建、修复或删除数据库。
    """
    db_file = get_runtime_setting('CONFIG_PATH') / "user.db"
    if not db_file.exists():
        runner.add(
            finding_id="database.sqlite_missing",
            severity=DoctorSeverity.Warn,
            status=DoctorFindingStatus.Degraded,
            title="SQLite 数据库文件不存在",
            detail=f"未找到 {db_file}。",
            recommendation="首次启动会自动初始化数据库；如不是首次安装，请确认 CONFIG_DIR 是否指向正确目录。",
            context={"database": str(db_file)},
        )
        return
    try:
        connection = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=3)
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()
        finally:
            connection.close()
    except sqlite3.Error as err:
        runner.add(
            finding_id="database.sqlite_open_failed",
            severity=DoctorSeverity.Error,
            status=DoctorFindingStatus.Failed,
            title="SQLite 数据库无法打开",
            detail=str(err),
            recommendation="确认配置目录权限和磁盘状态；不要直接删除数据库，必要时先备份再处理。",
            context={"database": str(db_file)},
        )
        return

    if not result or result[0] != "ok":
        runner.add(
            finding_id="database.sqlite_integrity_failed",
            severity=DoctorSeverity.Error,
            status=DoctorFindingStatus.Failed,
            title="SQLite 完整性检查失败",
            detail=str(result[0] if result else "无检查结果"),
            recommendation="先备份 user.db，再根据 SQLite integrity_check 输出处理或恢复备份。",
            context={"database": str(db_file)},
        )
        return

    runner.add(
        finding_id="database.sqlite",
        severity=DoctorSeverity.Info,
        status=DoctorFindingStatus.Ok,
        title="SQLite 数据库可读",
        detail=f"{db_file} 可打开且 integrity_check 返回 ok。",
        recommendation="无需处理。",
        context={"database": str(db_file)},
    )


def _check_postgresql_database(runner: DoctorRunnerProtocol) -> None:
    """
    检查 PostgreSQL 必需配置，仅在深度模式下探测 TCP 端口。

    默认不连接数据库；套接字模式或未配置 TCP 端口时跳过探测，不核验账号权限。
    """
    missing = []
    for key in ("DB_POSTGRESQL_HOST", "DB_POSTGRESQL_DATABASE", "DB_POSTGRESQL_USERNAME"):
        if not str(get_runtime_setting(key, "") or "").strip():
            missing.append(key)
    if missing:
        runner.add(
            finding_id="database.postgresql_config_incomplete",
            severity=DoctorSeverity.Error,
            status=DoctorFindingStatus.Failed,
            title="PostgreSQL 配置不完整",
            detail=f"缺少配置：{', '.join(missing)}。",
            recommendation="补齐 PostgreSQL 主机、库名和用户名后重启。",
            context={"missing": missing},
        )
        return

    if not runner.deep:
        runner.add(
            finding_id="database.postgresql_config",
            severity=DoctorSeverity.Info,
            status=DoctorFindingStatus.Skipped,
            title="PostgreSQL 配置已具备基本字段",
            detail="默认离线模式不主动连接 PostgreSQL；可使用 `moviepilot doctor --deep` 做 TCP 连通性探测。",
            recommendation="如启动日志提示数据库连接失败，请检查 PostgreSQL 服务、网络和账号权限。",
        )
        return

    host = get_runtime_setting('DB_POSTGRESQL_HOST')
    port = get_runtime_setting('DB_POSTGRESQL_PORT')
    if get_runtime_setting('DB_POSTGRESQL_SOCKET_MODE') or not port:
        runner.add(
            finding_id="database.postgresql_deep_skipped",
            severity=DoctorSeverity.Info,
            status=DoctorFindingStatus.Skipped,
            title="PostgreSQL 深度探测已跳过",
            detail="当前使用 Unix Socket 或未配置 TCP 端口，doctor 不直接打开数据库连接。",
            recommendation="如需验证账号权限，请使用 PostgreSQL 客户端在宿主环境单独测试。",
        )
        return
    ok, detail = _can_connect(host, int(port), timeout=2.0)
    runner.add(
        finding_id="database.postgresql_tcp",
        severity=DoctorSeverity.Info if ok else DoctorSeverity.Error,
        status=DoctorFindingStatus.Ok if ok else DoctorFindingStatus.Failed,
        title="PostgreSQL TCP 端口可连接" if ok else "PostgreSQL TCP 端口不可连接",
        detail=f"{get_runtime_setting('DB_POSTGRESQL_TARGET')} {detail}".strip(),
        recommendation="不可连接时请检查数据库服务、容器网络、端口映射和防火墙。",
    )


def _check_database(runner: DoctorRunnerProtocol) -> None:
    """
    按当前数据库类型执行检查，再离线校验受管备份，不进行还原。
    """
    if get_runtime_setting('DB_TYPE').lower() == "postgresql":
        _check_postgresql_database(runner)
    else:
        _check_sqlite_database(runner)
    _check_database_backups(runner)


def _check_database_backups(runner: DoctorRunnerProtocol) -> None:
    """
    列举并离线校验与当前数据库类型匹配的受管备份。
    """
    db_type = "postgresql" if get_runtime_setting('DB_TYPE').lower() == "postgresql" else "sqlite"
    try:
        paths = BackupFiles(get_runtime_setting('DATABASE_BACKUP_PATH')).list()
    except OSError as error:
        runner.add(
            finding_id="database.backup_recovery",
            severity=DoctorSeverity.Error,
            status=DoctorFindingStatus.Failed,
            title="数据库备份目录无法读取",
            detail=str(error),
            recommendation="检查数据库备份目录是否存在且当前用户具有读取权限。",
            context={"db_type": db_type},
        )
        return

    matching = [path for path in paths if BackupFiles.database_type(path.name) == db_type]
    mismatched = [path.name for path in paths if path not in matching]
    if not paths:
        runner.add(
            finding_id="database.backup_recovery",
            severity=DoctorSeverity.Info,
            status=DoctorFindingStatus.Skipped,
            title="未找到受管数据库备份",
            detail=f"当前数据库类型为 {db_type}，备份目录中没有正式备份文件。",
            recommendation="可执行 `moviepilot database backup` 创建一次可校验备份。",
            affects_report_status=False,
            context={"db_type": db_type, "backups": []},
        )
        return

    if not matching:
        runner.add(
            finding_id="database.backup_recovery",
            severity=DoctorSeverity.Warn,
            status=DoctorFindingStatus.Degraded,
            title="没有匹配当前数据库类型的备份",
            detail=f"当前数据库类型为 {db_type}，仅找到其他类型备份：{', '.join(mismatched)}。",
            recommendation="确认数据库类型配置，或执行 `moviepilot database backup` 创建当前类型备份。",
            affects_report_status=False,
            context={"db_type": db_type, "backups": [], "mismatched": mismatched},
        )
        return

    backups = []
    for path in matching:
        try:
            size = path.stat().st_size
            verification = verify_database_backup(path, db_type=db_type)
            valid = verification.valid
            method = verification.method
            detail = verification.detail
        except (OSError, RuntimeError, ValueError) as error:
            size = None
            valid = False
            method = "unavailable"
            detail = str(error)
        backups.append(
            {
                "name": path.name,
                "db_type": db_type,
                "size": size,
                "valid": valid,
                "method": method,
                "detail": detail,
            }
        )

    valid_backups = [backup for backup in backups if backup["valid"]]
    context = {
        "db_type": db_type,
        "backups": backups,
        "mismatched": mismatched,
    }
    if valid_backups:
        newest = valid_backups[0]
        command = f"moviepilot database restore {newest['name']} --confirm"
        runner.add(
            finding_id="database.backup_recovery",
            severity=DoctorSeverity.Info,
            status=DoctorFindingStatus.Ok,
            title="存在可还原的数据库备份",
            detail=f"已校验 {len(backups)} 个 {db_type} 备份，其中 {len(valid_backups)} 个可用。",
            recommendation=f"需要恢复时先停止 MoviePilot，再执行 `{command}`。",
            context={**context, "restore_command": command},
        )
        return

    failures = "; ".join(
        f"{backup['name']}: {backup['detail'] or backup['method']}"
        for backup in backups
    )
    runner.add(
        finding_id="database.backup_recovery",
        severity=DoctorSeverity.Error,
        status=DoctorFindingStatus.Failed,
        title="数据库备份均未通过校验",
        detail=(
            f"已检查 {len(backups)} 个 {db_type} 备份，没有可直接还原的文件。"
            f"校验结果：{failures}"
        ),
        recommendation="根据校验详情处理备份文件或 PostgreSQL client，再重新运行 doctor。",
        affects_report_status=False,
        context=context,
    )


def _check_frontend_assets(runner: DoctorRunnerProtocol) -> None:
    """
    检查前端版本文件与服务入口或静态首页是否存在，不下载安装资源。
    """
    frontend_dir = _frontend_dir()
    required = [frontend_dir / "version.txt"]
    service_file = frontend_dir / "service.js"
    index_file = frontend_dir / "index.html"
    if service_file.exists():
        required.append(service_file)
    else:
        required.append(index_file)
    missing = [path for path in required if not path.exists()]
    if missing:
        runner.add(
            finding_id="frontend.assets_missing",
            severity=DoctorSeverity.Error,
            status=DoctorFindingStatus.Failed,
            title="前端资源缺失",
            detail=f"缺少文件：{', '.join(str(path) for path in missing)}。",
            recommendation="本地环境执行 `moviepilot install frontend`；Docker 环境建议重新拉取或重建镜像。",
            context={"frontend_dir": str(frontend_dir), "missing": [str(path) for path in missing]},
        )
        return

    version = ""
    try:
        version = (frontend_dir / "version.txt").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        version = "unknown"
    runner.add(
        finding_id="frontend.assets",
        severity=DoctorSeverity.Info,
        status=DoctorFindingStatus.Ok,
        title="前端资源存在",
        detail=f"前端目录：{frontend_dir}；版本：{version or 'unknown'}。",
        recommendation="无需处理。",
        context={"frontend_dir": str(frontend_dir), "version": version},
    )


def _check_logs(runner: DoctorRunnerProtocol) -> None:
    """
    扫描近期日志，并区分核心运行异常与插件扩展异常。
    """
    log_files = [
        _backend_app_log_file(),
        _backend_stdio_log_file(),
        _frontend_stdio_log_file(),
    ]
    plugin_log_dir = get_runtime_setting('LOG_PATH') / "plugins"
    plugin_logger_names: set[str] = set()
    if plugin_log_dir.exists():
        plugin_log_files = sorted(plugin_log_dir.rglob("*.log"))
        plugin_logger_names = {path.stem.lower() for path in plugin_log_files}
        log_files.extend(plugin_log_files[:20])

    found_any = False
    entries: dict[str, list[tuple[Path, str]]] = {
        "core": [],
        "plugin": [],
    }
    for path in log_files:
        if not path.exists() or not path.is_file():
            continue
        found_any = True
        lines = _recent_log_lines(_tail_lines(path))
        is_plugin_log = plugin_log_dir in path.parents
        if is_plugin_log:
            scoped_errors = [(True, _find_error_lines(lines))]
        else:
            core_errors, plugin_errors = _partition_error_lines(
                lines,
                plugin_logger_names,
            )
            scoped_errors = [(False, core_errors), (True, plugin_errors)]
        for is_plugin_error, errors in scoped_errors:
            component = "plugin" if is_plugin_error else "core"
            entries[component].extend((path, error) for error in errors)

    if not found_any:
        runner.add(
            finding_id="logs.none",
            severity=DoctorSeverity.Warn,
            status=DoctorFindingStatus.Degraded,
            title="未找到运行日志",
            detail=f"{get_runtime_setting('LOG_PATH')} 下没有可读取的 MoviePilot 日志。",
            recommendation="如果服务尚未启动过可忽略；否则请确认 CONFIG_DIR 和日志目录权限。",
        )
        return

    core_first_path = entries["core"][0][0] if entries["core"] else None
    for component in ("core", "plugin"):
        component_entries = entries[component]
        if not component_entries:
            continue
        first_path = component_entries[0][0]
        finding_suffix = (
            "plugin_errors"
            if component == "plugin" and first_path == core_first_path
            else "recent_errors"
        )
        detail_lines, source_files = _aggregate_log_entries(component_entries)
        runner.add(
            finding_id=f"logs.{first_path.stem}.{finding_suffix}",
            severity=DoctorSeverity.Warn,
            status=DoctorFindingStatus.Degraded,
            title="最近日志存在插件异常" if component == "plugin" else "最近日志存在错误线索",
            detail="\n".join(detail_lines),
            recommendation=(
                "可使用安全模式启动后检查插件配置。"
                if component == "plugin"
                else "结合前后的启动日志定位异常；必要时执行 `moviepilot doctor --json` 交给 Agent 或 Issue 流程。"
            ),
            affects_report_status=component == "core",
            context={
                "log_file": str(first_path),
                "log_files": source_files,
                "matches": len(component_entries),
                "unique_matches": len(detail_lines),
                "component": component,
                "lookback_hours": LOG_LOOKBACK_HOURS,
            },
        )

    if not entries["core"]:
        runner.add(
            finding_id="logs.recent",
            severity=DoctorSeverity.Info,
            status=DoctorFindingStatus.Ok,
            title="最近日志未发现明显错误关键词",
            detail=(
                f"已扫描 {get_runtime_setting('LOG_PATH')} 下最近 {LOG_LOOKBACK_HOURS} 小时的主日志、"
                "启动日志和插件日志；插件扩展告警不参与核心健康状态。"
            ),
            recommendation="如果问题仍存在，请结合具体操作时间扩大日志范围排查。",
        )


def _check_docker(runner: DoctorRunnerProtocol) -> None:
    """
    检查容器配置挂载、解释器及命令行入口是否齐备；非容器环境记录为跳过。
    """
    if not SystemUtils.is_docker():
        runner.add(
            finding_id="docker.environment",
            severity=DoctorSeverity.Info,
            status=DoctorFindingStatus.Skipped,
            title="当前不是 Docker 环境",
            detail=f"平台：{platform.system()} {platform.release()}。",
            recommendation="本地源码模式可直接使用 `moviepilot doctor`。",
        )
        return

    issues = []
    if not Path("/config").exists():
        issues.append("/config 不存在")
    venv_path = Path(os.getenv("VENV_PATH", "/opt/venv")) / "bin" / "python3"
    if not venv_path.exists():
        issues.append(f"{venv_path} 不存在")
    command_path = Path("/usr/local/bin/moviepilot")
    if not command_path.exists():
        issues.append("/usr/local/bin/moviepilot 不存在")

    if issues:
        runner.add(
            finding_id="docker.runtime_incomplete",
            severity=DoctorSeverity.Error,
            status=DoctorFindingStatus.Failed,
            title="Docker 诊断入口不完整",
            detail="；".join(issues),
            recommendation="重新构建镜像，或使用 `python -m app.cli doctor` 作为临时入口。",
        )
        return

    runner.add(
        finding_id="docker.runtime",
        severity=DoctorSeverity.Info,
        status=DoctorFindingStatus.Ok,
        title="Docker 诊断入口可用",
        detail=(
            f"CONFIG_DIR={get_runtime_setting('CONFIG_PATH')}；VENV_PATH={os.getenv('VENV_PATH', '/opt/venv')}；"
            "supervisor 托管 Nginx 和后端进程"
        ),
        recommendation="后端异常由 supervisor 自动拉起；启动失败时可通过 `docker exec <container> moviepilot doctor` 诊断。",
    )


def _check_safe_mode(runner: DoctorRunnerProtocol) -> None:
    """
    报告安全模式是否启用及后台能力的限制，不更改当前启动模式。
    """
    if get_runtime_setting('MOVIEPILOT_SAFE_MODE'):
        runner.add(
            finding_id="startup.safe_mode",
            severity=DoctorSeverity.Warn,
            status=DoctorFindingStatus.Degraded,
            title="当前处于安全模式",
            detail="本次启动会跳过插件、调度器、监控、命令和工作流等后台扩展能力。",
            recommendation="修复异常插件或配置后，移除 MOVIEPILOT_SAFE_MODE 或改用普通 `moviepilot start`。",
        )
    else:
        runner.add(
            finding_id="startup.safe_mode_off",
            severity=DoctorSeverity.Info,
            status=DoctorFindingStatus.Ok,
            title="安全模式未启用",
            detail="本次运行会按正常流程加载插件和后台任务。",
            recommendation="若插件或后台任务导致无法启动，可使用 `moviepilot start --safe` 或设置 MOVIEPILOT_SAFE_MODE=true。",
        )
