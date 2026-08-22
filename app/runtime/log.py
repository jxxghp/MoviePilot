from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import os
import queue
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Protocol, Tuple

import click
from pydantic import BaseModel, ConfigDict

class LogConfigModel(BaseModel):
    """描述日志级别、格式和文件写入策略。"""

    model_config = ConfigDict(extra="ignore")

    CONFIG_DIR: Optional[str] = None
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"
    LOG_MAX_FILE_SIZE: int = 5
    LOG_BACKUP_COUNT: int = 10
    LOG_CONSOLE_FORMAT: str = (
        "%(leveltext)s[%(name)s] %(asctime)s [%(correlation_id)s] %(message)s"
    )
    LOG_FILE_FORMAT: str = (
        "【%(levelname)s】%(asctime)s [%(correlation_id)s] - %(message)s"
    )
    ASYNC_FILE_QUEUE_SIZE: int = 1000
    ASYNC_FILE_WORKERS: int = 2
    BATCH_WRITE_SIZE: int = 50
    WRITE_TIMEOUT: float = 3.0

    @property
    def LOG_MAX_FILE_SIZE_BYTES(self) -> int:
        """将日志文件大小从 MB 转换为字节。"""
        return self.LOG_MAX_FILE_SIZE * 1024 * 1024


class LogSettings(LogConfigModel):
    """保存当前进程已经生效的日志策略。"""


class LogEntry:
    """表示一条等待基础设施写入的文件日志。"""

    def __init__(
        self,
        level: str,
        message: str,
        file_path: Path,
        timestamp: datetime | None = None,
    ) -> None:
        """记录日志级别、格式化文本和目标文件。"""
        self.level = level
        self.message = message
        self.file_path = file_path
        self.timestamp = timestamp or datetime.now()
        self.correlation_id = _get_log_correlation_id()


class LogWriter(Protocol):
    """平台日志门面使用的文件写入端口。"""

    def write_log(self, level: str, message: str, file_path: Path) -> None:
        """将一条日志写入指定文件。"""

    def shutdown(self) -> None:
        """排空待写日志并释放写入资源。"""


log_settings = LogSettings()
_correlation_id_provider: Callable[[], str | None] = lambda: None


def configure_correlation_id_provider(provider: Callable[[], str | None]) -> None:
    """由组合根注入日志关联 ID 读取端口，保持日志模块为依赖叶节点。"""
    global _correlation_id_provider
    _correlation_id_provider = provider


def _get_log_correlation_id() -> str:
    """读取当前关联 ID；未装配或无请求上下文时返回稳定占位符。"""
    return _correlation_id_provider() or "-"

# 插件实例日志等级允许的取值，需与 app.db.models.pluginconfig.LOG_LEVELS 保持一致；
# runtime 层不允许依赖 db 层（见 tests/test_architecture_dependencies.py 的包级矩阵），
# 这里维护一份独立副本，仅用于本模块内部的防御性校验。
LOG_LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")

# 插件实例无法辨识时，日志落入该插件目录下的兜底子目录，而不是静默丢弃
UNATTRIBUTED_INSTANCE_ID = "_unattributed"
# 插件实例日志目录下的固定文件名；目录本身已经区分插件与实例。公开给日志下载/
# 流式/诊断等读取端，避免各处各自约定文件名
PLUGIN_LOG_FILENAME = "plugin.log"


def _current_global_log_level() -> int:
    """返回当前全局日志策略对应的标准库日志级别。"""
    if log_settings.DEBUG:
        return logging.DEBUG
    return getattr(logging, log_settings.LOG_LEVEL.upper(), logging.INFO)


@dataclass(frozen=True)
class _PluginLevelOverride:
    """一个插件实例的日志等级覆盖。"""

    level: int
    level_name: str
    expires_at: Optional[float]


# 宿主在初始化、事件分发、定时任务等自己控制的调用点用它绑定当前插件实例；
# 插件自建的原生线程不会继承这里绑定的取值（contextvars 只在同一协程/任务链内传播），
# 这类线程内的日志按插件级栈回溯归入该插件的兜底目录，不属于本机制的覆盖范围。
_current_plugin_instance: "contextvars.ContextVar[Optional[Tuple[str, str]]]" = (
    contextvars.ContextVar("current_plugin_instance", default=None)
)

# 插件实例日志目录解析器：由启动组合根注入，避免本模块反向依赖 app.plugins
PluginInstanceLogDirResolver = Callable[[str, str], Path]
_plugin_log_dir_resolver: Optional[PluginInstanceLogDirResolver] = None
_plugin_log_dir_cache: Dict[Tuple[str, str], Path] = {}
_plugin_log_dir_cache_lock = threading.RLock()

# 插件实例日志等级覆盖缓存：等级来自数据库，避免每条日志都查库；写入源是日志控制 API
# （配置变更时直接调用 set/clear，立即生效）和启动组合根（进程重启后从数据库预热）；
# 覆盖的过期回落在读取时惰性判定并清理，不额外起后台线程扫描
_plugin_level_overrides: Dict[Tuple[str, str], _PluginLevelOverride] = {}
_plugin_level_lock = threading.RLock()
# 快速闸阈值：取全局等级与全部未过期实例覆盖中最宽松（数值最小）的一档；
# 只有通过这一闸门的日志才会继续做代价更高的栈回溯和按实例二次过滤
_plugin_level_floor: int = logging.INFO


def _recompute_plugin_level_floor_locked() -> None:
    """在已持有 `_plugin_level_lock` 的前提下清理过期覆盖并刷新快速闸阈值。"""
    global _plugin_level_floor
    now = time.time()
    expired = [
        key
        for key, entry in _plugin_level_overrides.items()
        if entry.expires_at is not None and entry.expires_at <= now
    ]
    for key in expired:
        del _plugin_level_overrides[key]
    levels = [entry.level for entry in _plugin_level_overrides.values()]
    _plugin_level_floor = min([_current_global_log_level(), *levels])


def refresh_plugin_level_floor() -> None:
    """在全局日志等级变化后刷新快速闸阈值，供 `configure_log_settings` 调用。"""
    with _plugin_level_lock:
        _recompute_plugin_level_floor_locked()


def set_plugin_instance_log_level(
    plugin_id: str,
    instance_id: str,
    level: str,
    expires_at: Optional[datetime] = None,
) -> None:
    """
    设置插件实例的日志等级覆盖，写入进程内缓存并立即生效。

    只维护运行期生效状态；把覆盖持久化到数据库是调用方（日志控制 API、启动组合根
    的缓存预热）的职责，本函数不做任何数据库读写。
    :param plugin_id: 插件标识
    :param instance_id: 实例标识
    :param level: 目标等级，取值须在 LOG_LEVELS 内
    :param expires_at: 覆盖失效时间，None 表示不过期
    :raises ValueError: level 不是受支持的等级名
    """
    normalized = (level or "").strip().upper()
    if normalized not in LOG_LEVELS:
        raise ValueError(f"不支持的日志等级：{level}")
    entry = _PluginLevelOverride(
        level=getattr(logging, normalized),
        level_name=normalized,
        expires_at=expires_at.timestamp() if expires_at else None,
    )
    with _plugin_level_lock:
        _plugin_level_overrides[(plugin_id, instance_id)] = entry
        _recompute_plugin_level_floor_locked()


def clear_plugin_instance_log_level(plugin_id: str, instance_id: str) -> None:
    """
    清除插件实例的日志等级覆盖，运行期立即回落全局等级。
    :param plugin_id: 插件标识
    :param instance_id: 实例标识
    """
    with _plugin_level_lock:
        _plugin_level_overrides.pop((plugin_id, instance_id), None)
        _recompute_plugin_level_floor_locked()


def get_plugin_instance_log_level_override(
    plugin_id: str, instance_id: str
) -> Optional[Tuple[str, Optional[datetime]]]:
    """
    返回插件实例当前缓存的原始等级覆盖设置，未设置或已过期时为 None。
    :param plugin_id: 插件标识
    :param instance_id: 实例标识
    :return: `(等级名, 失效时间)`；失效时间为 None 表示不过期
    """
    with _plugin_level_lock:
        entry = _plugin_level_overrides.get((plugin_id, instance_id))
        if entry is None:
            return None
        if entry.expires_at is not None and entry.expires_at <= time.time():
            del _plugin_level_overrides[(plugin_id, instance_id)]
            _recompute_plugin_level_floor_locked()
            return None
        expires_dt = (
            datetime.fromtimestamp(entry.expires_at) if entry.expires_at else None
        )
        return entry.level_name, expires_dt


def get_effective_plugin_instance_log_level(plugin_id: str, instance_id: str) -> str:
    """
    返回插件实例当前生效的日志等级名，覆盖过期时回落全局等级。
    :param plugin_id: 插件标识
    :param instance_id: 实例标识
    :return: 等级名，如 "DEBUG"
    """
    override = get_plugin_instance_log_level_override(plugin_id, instance_id)
    if override is not None:
        return override[0]
    return "DEBUG" if log_settings.DEBUG else log_settings.LOG_LEVEL.upper()


def _effective_instance_level_int(plugin_id: str, instance_id: Optional[str]) -> int:
    """
    返回插件实例过滤日志时实际使用的等级整数，供 `LoggerManager.logger` 二次过滤。

    实例无法辨识（`instance_id` 为空或等于 `UNATTRIBUTED_INSTANCE_ID`）时没有覆盖
    可用，直接回落全局等级。
    """
    if not instance_id or instance_id == UNATTRIBUTED_INSTANCE_ID:
        return _current_global_log_level()
    with _plugin_level_lock:
        entry = _plugin_level_overrides.get((plugin_id, instance_id))
        if entry is None:
            return _current_global_log_level()
        if entry.expires_at is not None and entry.expires_at <= time.time():
            del _plugin_level_overrides[(plugin_id, instance_id)]
            _recompute_plugin_level_floor_locked()
            return _current_global_log_level()
        return entry.level


def configure_plugin_log_dir_resolver(resolver: PluginInstanceLogDirResolver) -> None:
    """
    由启动组合根注入插件实例日志目录解析器。

    runtime/log 保持依赖叶节点，不直接导入 app.plugins；日志目录推导（含插件持久化
    路径的分段校验）由组合根经 `app.plugins.plugin_instance_path` 提供。装配新的解析器
    时清空既有缓存，避免复用装配前解析出的目录。
    :param resolver: 按 (插件标识, 实例标识) 返回该实例日志目录的函数
    """
    global _plugin_log_dir_resolver
    with _plugin_log_dir_cache_lock:
        _plugin_log_dir_resolver = resolver
        _plugin_log_dir_cache.clear()


def get_plugin_instance_log_dir(plugin_id: str, instance_id: str) -> Optional[Path]:
    """
    返回插件实例的日志目录，解析结果按 (插件标识, 实例标识) 缓存到进程生命周期内
    （目录位置在进程运行期间不会变化，无需失效）。
    :param plugin_id: 插件标识
    :param instance_id: 实例标识，传 `UNATTRIBUTED_INSTANCE_ID` 取该插件的兜底目录
    :return: 日志目录绝对路径；解析器未装配或解析出错时为 None
    """
    key = (plugin_id, instance_id)
    with _plugin_log_dir_cache_lock:
        cached = _plugin_log_dir_cache.get(key)
        if cached is not None:
            return cached
        resolver = _plugin_log_dir_resolver
    if resolver is None:
        return None
    try:
        resolved = resolver(plugin_id, instance_id)
    except Exception as err:
        print(f"插件实例日志目录解析失败 {plugin_id}/{instance_id}：{err}")
        return None
    with _plugin_log_dir_cache_lock:
        _plugin_log_dir_cache[key] = resolved
    return resolved


@contextmanager
def bind_plugin_instance(plugin_id: str, instance_id: str):
    """
    在宿主自己控制的调用点（初始化、事件分发、定时任务……）内绑定当前插件实例。

    绑定只在当前协程/任务链内生效；插件自建的原生线程不继承这个绑定。
    :param plugin_id: 插件标识
    :param instance_id: 实例标识
    """
    token = _current_plugin_instance.set((plugin_id, instance_id))
    try:
        yield
    finally:
        _current_plugin_instance.reset(token)


def wrap_for_plugin_instance(func: Callable, plugin_id: str, instance_id: str) -> Callable:
    """
    包装一个插件回调，使其执行期间的日志路由到指定实例。

    用于回调在注册时被捕获、稍后才由宿主（如调度器）调用的场景，例如插件定时服务；
    同步/异步函数各自返回同型包装，`inspect.iscoroutinefunction` 等自省结果不变。
    :param func: 插件提供的原始回调，通常是插件实例的绑定方法
    :param plugin_id: 插件标识
    :param instance_id: 实例标识
    :return: 包装后的可调用对象
    """
    if asyncio.iscoroutinefunction(func):
        @functools.wraps(func)
        async def _async_wrapped(*args: Any, **kwargs: Any) -> Any:
            with bind_plugin_instance(plugin_id, instance_id):
                return await func(*args, **kwargs)

        return _async_wrapped

    @functools.wraps(func)
    def _sync_wrapped(*args: Any, **kwargs: Any) -> Any:
        with bind_plugin_instance(plugin_id, instance_id):
            return func(*args, **kwargs)

    return _sync_wrapped


class NonBlockingFileHandler:
    """使用后台队列和滚动文件处理器写入业务日志。"""

    _instance = None
    _lock = threading.Lock()
    _stop_sentinel = object()

    def __new__(cls):
        """返回进程内唯一的文件写入器。"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        """初始化文件处理器缓存、异步队列和后台写线程。"""
        if hasattr(self, "_initialized"):
            return
        self._initialized = True
        self._state_lock = threading.RLock()
        self._handlers_lock = threading.Lock()
        self._rotating_handlers: dict[Path, RotatingFileHandler] = {}
        self._write_queue = queue.Queue(maxsize=log_settings.ASYNC_FILE_QUEUE_SIZE)
        self._executor = ThreadPoolExecutor(
            max_workers=log_settings.ASYNC_FILE_WORKERS,
            thread_name_prefix="LogWriter",
        )
        self._running = True
        self._write_thread = threading.Thread(
            target=self._batch_writer,
            daemon=True,
            name="LogBatchWriter",
        )
        self._write_thread.start()

    def _get_rotating_handler(self, file_path: Path) -> RotatingFileHandler:
        """按目标路径复用滚动文件处理器。"""
        with self._handlers_lock:
            handler = self._rotating_handlers.get(file_path)
            if handler:
                return handler
            file_path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                filename=str(file_path),
                maxBytes=log_settings.LOG_MAX_FILE_SIZE_BYTES,
                backupCount=log_settings.LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter(log_settings.LOG_FILE_FORMAT))
            self._rotating_handlers[file_path] = handler
            return handler

    def write_log(self, level: str, message: str, file_path: Path) -> None:
        """根据当前线程是否运行事件循环选择异步或同步文件写入。"""
        entry = LogEntry(level, message, file_path)
        if self._is_in_event_loop():
            self._write_non_blocking(entry)
            return
        with self._state_lock:
            if self._running:
                self._write_sync(entry)

    @staticmethod
    def _is_in_event_loop() -> bool:
        """判断当前线程是否正在运行 asyncio 事件循环。"""
        try:
            return asyncio.get_running_loop() is not None
        except RuntimeError:
            return False

    def _write_non_blocking(self, entry: LogEntry) -> bool:
        """将协程环境产生的日志无阻塞地放入写队列。"""
        with self._state_lock:
            if not self._running:
                return False
            try:
                self._write_queue.put_nowait(entry)
            except queue.Full:
                self._executor.submit(self._write_sync, entry)
            return True

    def _write_sync(self, entry: LogEntry) -> None:
        """立即将单条日志写入滚动文件。"""
        try:
            handler = self._get_rotating_handler(entry.file_path)
            handler.handle(self._to_record(entry))
        except Exception as err:
            print(f"日志写入失败 {entry.file_path}: {err}")
            print(f"【{entry.level.upper()}】{entry.timestamp} - {entry.message}")

    @staticmethod
    def _to_record(entry: LogEntry) -> logging.LogRecord:
        """把日志条目转换为标准库日志记录。"""
        record = logging.LogRecord(
            name="",
            level=getattr(logging, entry.level.upper(), logging.INFO),
            pathname="",
            lineno=0,
            msg=entry.message,
            args=(),
            exc_info=None,
            created=entry.timestamp.timestamp(),
        )
        record.correlation_id = entry.correlation_id
        return record

    def _batch_writer(self) -> None:
        """持续收集队列日志，并在停止哨兵后排空已有批次。"""
        while True:
            try:
                batch = []
                should_stop = False
                end_time = time.time() + log_settings.WRITE_TIMEOUT
                while (
                    len(batch) < log_settings.BATCH_WRITE_SIZE
                    and time.time() < end_time
                ):
                    try:
                        remaining_time = max(0, end_time - time.time())
                        entry = self._write_queue.get(timeout=remaining_time)
                        if entry is self._stop_sentinel:
                            should_stop = True
                            break
                        batch.append(entry)
                    except queue.Empty:
                        break
                if batch:
                    self._write_batch(batch)
                if should_stop:
                    break
            except Exception as err:
                print(f"批量写入线程错误: {err}")
                time.sleep(0.1)

    def _write_batch(self, batch: list[LogEntry]) -> None:
        """按目标文件分组写入一批日志。"""
        file_groups: dict[Path, list[LogEntry]] = {}
        for entry in batch:
            file_groups.setdefault(entry.file_path, []).append(entry)
        for file_path, entries in file_groups.items():
            try:
                handler = self._get_rotating_handler(file_path)
                for entry in entries:
                    handler.handle(self._to_record(entry))
            except Exception as err:
                print(f"批量写入失败 {file_path}: {err}")
                for entry in entries:
                    self._write_sync(entry)

    def shutdown(self) -> None:
        """停止接收新日志，排空队列并关闭线程池和文件处理器。"""
        with self._state_lock:
            if not self._running:
                return
            self._running = False
            if self._write_thread.is_alive():
                self._write_queue.put(self._stop_sentinel)
        if self._write_thread.is_alive():
            self._write_thread.join()
        self._executor.shutdown(wait=True)
        for handler in self._rotating_handlers.values():
            handler.flush()
            handler.close()
        self._rotating_handlers.clear()

_LEVEL_NAME_COLORS = {
    logging.DEBUG: lambda level_name: click.style(str(level_name), fg="cyan"),
    logging.INFO: lambda level_name: click.style(str(level_name), fg="green"),
    logging.WARNING: lambda level_name: click.style(str(level_name), fg="yellow"),
    logging.ERROR: lambda level_name: click.style(str(level_name), fg="red"),
    logging.CRITICAL: lambda level_name: click.style(
        str(level_name), fg="bright_red"
    ),
}


class CustomFormatter(logging.Formatter):
    """为控制台日志级别添加颜色和对齐文本。"""

    def format(self, record: logging.LogRecord) -> str:
        """格式化一条控制台日志记录。"""
        separator = " " * max(8 - len(record.levelname), 0)
        colorizer = _LEVEL_NAME_COLORS.get(record.levelno, str)
        record.leveltext = colorizer(record.levelname + ":") + separator
        record.correlation_id = _get_log_correlation_id()
        return super().format(record)


class LoggerManager:
    """
    管理进程级日志策略、控制台输出和插件日志路由。

    文件 I/O 由启动层注入的 :class:`LogWriter` 完成；在写入器装配之前产生的
    少量启动日志会暂存，装配完成后只补写文件，不重复输出到控制台。
    """

    _loggers: Dict[str, logging.Logger] = {}
    _default_log_file = Path("moviepilot.log")
    _lock = threading.RLock()
    _writer: Optional[LogWriter] = None
    _log_path: Optional[Path] = None
    _pending_file_logs: deque[tuple[str, str, Path]] = deque(maxlen=1000)

    def get_logger(self, name: str) -> logging.Logger:
        """返回使用当前控制台策略的命名标准库日志器。"""
        logfile = Path(f"{name}.log")
        return self._get_console_logger(logfile)

    @staticmethod
    def _get_caller() -> tuple[str, Optional[str]]:
        """
        识别日志调用文件和插件来源。

        插件调用宿主公共方法时，调用栈中仍保留插件帧，因此日志继续进入该插件
        的独立文件，而不是混入主程序日志。
        """
        caller_name = None
        plugin_name = None
        try:
            frame = sys._getframe(3)  # noqa: SLF001
        except (AttributeError, ValueError):
            return "log.py", None

        while frame:
            filepath = Path(frame.f_code.co_filename)
            parts = filepath.parts
            if not caller_name:
                caller_name = parts[-2] if parts[-1] == "__init__.py" and len(parts) >= 2 else parts[-1]
            if "app" in parts:
                if not plugin_name and "plugins" in parts:
                    try:
                        plugins_index = parts.index("plugins")
                        # 插件源码必须位于 plugins 目录之下的子目录里（plugins/<插件目录>/...），
                        # 至少还需两段（子目录名 + 文件名）；否则命中的是 app/plugins/__init__.py
                        # 这个宿主文件本身，不能算作插件日志
                        if plugins_index + 2 < len(parts):
                            plugin_name = parts[plugins_index + 1]
                            break
                    except ValueError:
                        pass
                if "main.py" in parts:
                    break
            elif len(parts) != 1:
                break
            frame = frame.f_back
        return caller_name or "log.py", plugin_name

    @staticmethod
    def _resolve_plugin_instance(stack_plugin_name: Optional[str]) -> tuple[Optional[str], Optional[str]]:
        """
        定位当前日志所属的插件与实例。

        优先采信宿主在受控调用点绑定的 ContextVar（能精确到实例）；未绑定时退回
        栈回溯识别出的插件名（只能定位到插件，定位不到具体实例）。
        :param stack_plugin_name: `_get_caller` 栈回溯识别出的插件名
        :return: `(插件标识, 实例标识)`；均为 None 表示宿主日志，实例为 None 表示
            插件已定位但实例未定位（栈回溯场景）
        """
        ctx = _current_plugin_instance.get()
        if ctx is not None:
            return ctx
        if stack_plugin_name:
            return stack_plugin_name, None
        return None, None

    @staticmethod
    def _resolve_plugin_logfile(plugin_id: str, instance_id: Optional[str]) -> Path:
        """
        定位插件日志的目标文件。

        实例目录解析器已装配时，日志按实例落到 ``<实例目录>/logs/plugin.log``
        （实例未定位时落到该插件的 `UNATTRIBUTED_INSTANCE_ID` 兜底目录，不静默丢弃）；
        解析器未装配（如未经过启动组合根装配的孤立场景）时退回旧版扁平布局
        ``plugins/<插件标识>.log``，全部实例共用同一个文件。
        :param plugin_id: 插件标识
        :param instance_id: 实例标识，None 表示未定位到具体实例
        :return: 相对或绝对的目标日志文件路径
        """
        target_instance = instance_id or UNATTRIBUTED_INSTANCE_ID
        log_dir = get_plugin_instance_log_dir(plugin_id, target_instance)
        if log_dir is not None:
            return log_dir / PLUGIN_LOG_FILENAME
        return Path("plugins") / f"{plugin_id}.log"

    @classmethod
    def _setup_console_logger(cls, logfile: Path) -> logging.Logger:
        """创建只负责控制台输出的标准库日志器。"""
        logger_name = str(logfile.with_suffix(""))
        configured_logger = logging.getLogger(logger_name)
        configured_logger.setLevel(cls._get_log_level())
        configured_logger.handlers.clear()
        if os.getenv("MOVIEPILOT_DISABLE_CONSOLE_LOG") != "1":
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(
                CustomFormatter(log_settings.LOG_CONSOLE_FORMAT)
            )
            configured_logger.addHandler(console_handler)
        configured_logger.propagate = False
        return configured_logger

    @classmethod
    def _get_console_logger(cls, logfile: Path) -> logging.Logger:
        """按日志文件语境复用控制台日志器。"""
        logger_key = str(logfile)
        with cls._lock:
            configured_logger = cls._loggers.get(logger_key)
            if not configured_logger:
                configured_logger = cls._setup_console_logger(logfile)
                cls._loggers[logger_key] = configured_logger
            return configured_logger

    @classmethod
    def configure_writer(cls, writer: LogWriter, log_path: Path) -> None:
        """装配文件写入器，并补写装配前暂存的启动日志。"""
        with cls._lock:
            previous_writer = cls._writer
            cls._writer = writer
            cls._log_path = Path(log_path)
            pending = list(cls._pending_file_logs)
            cls._pending_file_logs.clear()
        if previous_writer and previous_writer is not writer:
            previous_writer.shutdown()
        for level, message, logfile in pending:
            writer.write_log(level, message, Path(log_path) / logfile)

    def update_loggers(self) -> None:
        """让已创建的控制台日志器应用最新级别和格式，并刷新插件日志快速闸阈值。"""
        with self._lock:
            for configured_logger in self._loggers.values():
                for handler in configured_logger.handlers:
                    if isinstance(handler, logging.StreamHandler):
                        handler.setFormatter(
                            CustomFormatter(log_settings.LOG_CONSOLE_FORMAT)
                        )
                configured_logger.setLevel(self._get_log_level())
        refresh_plugin_level_floor()

    @staticmethod
    def _get_log_level() -> int:
        """返回当前日志策略对应的标准库日志级别。"""
        return _current_global_log_level()

    @classmethod
    def _write_file_log(cls, level: str, message: str, logfile: Path) -> None:
        """写入文件，或在启动装配完成前暂存日志。"""
        with cls._lock:
            writer = cls._writer
            log_path = cls._log_path
            if not writer or not log_path:
                cls._pending_file_logs.append((level, message, logfile))
                return
        writer.write_log(level, message, log_path / logfile)

    def logger(self, method: str, msg: str, *args: Any, **kwargs: Any) -> None:
        """按调用来源路由并输出一条日志。

        等级过滤分两级：先用快速闸（全局等级与全部实例覆盖中最宽松的一档）挡掉绝大多数
        被丢弃的日志，不为它们支付栈回溯的代价；通过快速闸后才回溯定位调用来源，
        再按命中的插件实例等级做一次精确过滤。这样只有确有实例开了更详细等级时，
        才会有日志需要多付这次栈回溯成本。
        """
        method_level = getattr(logging, method.upper(), logging.INFO)
        if method_level < _plugin_level_floor:
            return

        caller_name, stack_plugin_name = self._get_caller()
        plugin_id, instance_id = self._resolve_plugin_instance(stack_plugin_name)
        if plugin_id and method_level < _effective_instance_level_int(plugin_id, instance_id):
            return

        formatted_msg = f"{caller_name} - {msg}"
        if args:
            try:
                formatted_msg = formatted_msg % args
            except (TypeError, ValueError):
                formatted_msg = f"{formatted_msg} {' '.join(str(arg) for arg in args)}"

        logfile = (
            self._resolve_plugin_logfile(plugin_id, instance_id)
            if plugin_id
            else self._default_log_file
        )
        self._write_file_log(method.upper(), formatted_msg, logfile)

        configured_logger = self._get_console_logger(logfile)
        log_method = getattr(configured_logger, method, configured_logger.info)
        log_method(formatted_msg, **kwargs)

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """输出 INFO 日志。"""
        self.logger("info", msg, *args, **kwargs)

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """输出 DEBUG 日志。"""
        self.logger("debug", msg, *args, **kwargs)

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """输出 WARNING 日志。"""
        self.logger("warning", msg, *args, **kwargs)

    def warn(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """兼容历史调用并输出 WARNING 日志。"""
        self.warning(msg, *args, **kwargs)

    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """输出 ERROR 日志。"""
        self.logger("error", msg, *args, **kwargs)

    def critical(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """输出 CRITICAL 日志。"""
        self.logger("critical", msg, *args, **kwargs)

    @classmethod
    def shutdown(cls) -> None:
        """断开并关闭当前文件写入器。"""
        with cls._lock:
            writer = cls._writer
            cls._writer = None
            cls._log_path = None
        if writer:
            writer.shutdown()


logger = LoggerManager()


def configure_log_settings(source: object) -> None:
    """从完整系统配置同步日志相关字段并刷新控制台策略。"""
    for field_name in LogConfigModel.model_fields:
        if hasattr(source, field_name):
            setattr(log_settings, field_name, getattr(source, field_name))
    logger.update_loggers()


def configure_log_writer(writer: LogWriter, log_path: Path) -> None:
    """把基础设施文件写入器装配到平台日志门面。"""
    LoggerManager.configure_writer(writer=writer, log_path=log_path)
