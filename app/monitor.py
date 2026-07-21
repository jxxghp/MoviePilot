import json
import platform
import re
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Optional, Dict, List

from apscheduler.schedulers.background import BackgroundScheduler
from watchfiles import Change, DefaultFilter, watch

from app.chain import ChainBase
from app.chain.storage import StorageChain
from app.chain.transfer import TransferChain
from app.core.cache import TTLCache, FileCache
from app.core.config import settings
from app.db.transferhistory_oper import TransferHistoryOper
from app.helper.directory import DirectoryHelper
from app.helper.message import MessageHelper
from app.log import logger
from app.schemas import FileItem
from app.schemas.types import SystemConfigKey
from app.utils.mixins import ConfigReloadMixin
from app.utils.singleton import SingletonClass
from app.utils.system import SystemUtils

lock = Lock()
snapshot_lock = Lock()


class MonitorChain(ChainBase):
    pass


@dataclass(frozen=True)
class DirectoryChangeEvent:
    """
    目录文件变化事件，隔离底层 watchfiles 事件结构。
    """
    change_type: Change
    src_path: str
    is_directory: bool


class LocalDirectoryWatcher:
    """
    基于 watchfiles 的本地目录监控线程。
    """
    _HANDLE_CHANGES = {Change.added, Change.modified}

    def __init__(self, mon_path: Path, callback: Any, force_polling: Optional[bool] = None):
        """
        初始化本地目录监控。
        :param mon_path: 监控目录
        :param callback: 目录变化回调对象
        :param force_polling: 是否强制使用轮询模式，None 表示由 watchfiles 自动选择
        """
        self._watch_path = mon_path
        self._callback = callback
        self._force_polling = force_polling
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._watch_filter = DefaultFilter()

    @property
    def watch_path(self) -> Path:
        """
        获取监控目录。
        :return: 监控目录
        """
        return self._watch_path

    def start(self):
        """
        启动本地目录监控线程。
        """
        if not self._watch_path.exists():
            raise FileNotFoundError(f"监控目录不存在: {self._watch_path}")
        if not self._watch_path.is_dir():
            raise NotADirectoryError(f"监控路径不是目录: {self._watch_path}")
        if self.is_alive():
            logger.info(f"本地目录监控已在运行中: {self._watch_path}")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"MoviePilot-DirectoryWatcher-{self._watch_path.name}",
            daemon=True
        )
        self._thread.start()

    def stop(self):
        """
        请求停止本地目录监控线程。
        """
        self._stop_event.set()

    def join(self, timeout: Optional[float] = None):
        """
        等待本地目录监控线程退出。
        :param timeout: 最长等待秒数
        """
        if self._thread:
            self._thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        """
        判断监控线程是否仍在运行。
        :return: 线程存活状态
        """
        return bool(self._thread and self._thread.is_alive())

    def _run(self):
        """
        运行 watchfiles 主循环，并在快速模式不可用时回退到轮询。
        """
        try:
            self._run_watch(force_polling=self._force_polling)
        except Exception as err:
            if self._stop_event.is_set():
                return
            if self._force_polling is True:
                logger.error(f"本地目录监控发生错误: {self._watch_path} - {err}")
                logger.debug(traceback.format_exc())
                return
            logger.warn(f"快速模式监控 {self._watch_path} 失败，将自动切换到兼容模式: {err}")
            try:
                self._run_watch(force_polling=True)
            except Exception as fallback_err:
                if not self._stop_event.is_set():
                    logger.error(f"兼容模式监控 {self._watch_path} 仍然失败: {fallback_err}")
                    logger.debug(traceback.format_exc())

    def _run_watch(self, force_polling: Optional[bool]):
        """
        执行一次 watchfiles 监控循环。
        :param force_polling: 是否强制轮询
        """
        for changes in watch(
                str(self._watch_path),
                watch_filter=self._watch_filter,
                stop_event=self._stop_event,
                rust_timeout=1000,
                yield_on_timeout=True,
                force_polling=force_polling,
                recursive=True,
                ignore_permission_denied=True):
            if self._stop_event.is_set():
                break
            if not changes:
                continue
            self._handle_changes(changes)

    def _handle_changes(self, changes: set[tuple[Change, str]]):
        """
        将 watchfiles 原始变更转换为目录监控事件。
        :param changes: watchfiles 返回的变更集合
        """
        changes = self._expand_added_directories(changes)
        for change_type, path_str in sorted(changes, key=lambda item: item[1]):
            if change_type not in self._HANDLE_CHANGES:
                continue
            event_path = Path(path_str)
            event = self._build_event(change_type=change_type, event_path=event_path)
            if not event or event.is_directory:
                continue
            file_size = self._get_file_size(event_path)
            if file_size is None:
                continue
            text = self._change_text(change_type)
            try:
                self._callback.event_handler(
                    event=event,
                    text=text,
                    event_path=path_str,
                    file_size=file_size
                )
            except Exception as err:
                logger.error(f"处理本地目录监控事件失败: {path_str} - {err}")

    def _expand_added_directories(self, changes: set[tuple[Change, str]]) -> set[tuple[Change, str]]:
        """
        将整体移入监控范围的新增目录展开为内部文件事件。
        :param changes: watchfiles 返回的变更集合
        :return: 包含目录内新增文件的变更集合
        """
        expanded_changes = set(changes)
        for change_type, path_str in changes:
            if change_type != Change.added:
                continue
            event_path = Path(path_str)
            try:
                if not event_path.is_dir():
                    continue
                for nested_path in event_path.rglob("*"):
                    if not nested_path.is_file():
                        continue
                    nested_path_str = nested_path.as_posix()
                    if self._watch_filter(Change.added, nested_path_str):
                        expanded_changes.add((Change.added, nested_path_str))
            except OSError as err:
                logger.debug(f"扫描新增目录失败: {event_path} - {err}")
        return expanded_changes

    @staticmethod
    def _build_event(change_type: Change, event_path: Path) -> Optional[DirectoryChangeEvent]:
        """
        构建目录变化事件，路径已不存在时忽略。
        :param change_type: watchfiles 变化类型
        :param event_path: 变化路径
        :return: 目录变化事件
        """
        try:
            is_directory = event_path.is_dir()
        except OSError as err:
            logger.debug(f"读取目录监控事件路径失败: {event_path} - {err}")
            return None
        if not event_path.exists():
            return None
        return DirectoryChangeEvent(
            change_type=change_type,
            src_path=event_path.as_posix(),
            is_directory=is_directory
        )

    @staticmethod
    def _get_file_size(event_path: Path) -> Optional[int]:
        """
        读取事件文件大小，文件已消失时返回 None。
        :param event_path: 事件文件路径
        :return: 文件大小
        """
        try:
            return event_path.stat().st_size
        except OSError as err:
            logger.debug(f"读取目录监控文件大小失败: {event_path} - {err}")
            return None

    @staticmethod
    def _change_text(change_type: Change) -> str:
        """
        转换 watchfiles 事件类型为日志文案。
        :param change_type: watchfiles 变化类型
        :return: 事件描述
        """
        if change_type == Change.modified:
            return "修改"
        return "新增"


class Monitor(ConfigReloadMixin, metaclass=SingletonClass):
    """
    目录监控处理链，单例模式
    """
    CONFIG_WATCH = {SystemConfigKey.Directories.value}

    def __init__(self):
        super().__init__()
        # 本地目录监控服务
        self._watchers = []
        # 定时服务
        self._scheduler = None
        # 存储过照间隔（分钟）
        self._snapshot_interval = 5
        # TTL缓存，10秒钟有效
        self._cache = TTLCache(region="monitor", maxsize=1024, ttl=10)
        # 快照文件缓存
        self._snapshot_cache = FileCache(base=settings.CACHE_PATH / "snapshots")
        # 监控的文件扩展名
        self.all_exts = settings.RMT_MEDIAEXT + settings.RMT_SUBEXT + settings.RMT_AUDIOEXT
        # 启动目录监控和文件整理
        self.init()

    def on_config_changed(self):
        self.init()

    def get_reload_name(self):
        return "目录监控"

    def save_snapshot(self, storage: str, snapshot: Dict, file_count: int = 0,
                      last_snapshot_time: Optional[float] = None):
        """
        保存快照到文件缓存
        :param storage: 存储名称
        :param snapshot: 快照数据
        :param last_snapshot_time: 上次快照时间戳
        :param file_count: 文件数量，用于调整监控间隔
        """
        try:
            snapshot_time = max((item.get('modify_time', 0) for item in snapshot.values()), default=None)
            if snapshot_time is None:
                snapshot_time = last_snapshot_time or time.time()
            snapshot_data = {
                'timestamp': snapshot_time,
                'file_count': file_count,
                'snapshot': snapshot
            }
            # 使用FileCache保存快照数据
            cache_key = f"{storage}_snapshot"
            snapshot_json = json.dumps(snapshot_data, ensure_ascii=False, indent=2)
            self._snapshot_cache.set(cache_key, snapshot_json.encode('utf-8'), region="snapshots")
            logger.debug(f"快照已保存到缓存: {storage}")
        except Exception as e:
            logger.error(f"保存快照失败: {e}")

    def reset_snapshot(self, storage: str) -> bool:
        """
        重置快照，强制下次扫描时重新建立基准
        :param storage: 存储名称
        :return: 是否成功
        """
        try:
            cache_key = f"{storage}_snapshot"
            if self._snapshot_cache.exists(cache_key, region="snapshots"):
                self._snapshot_cache.delete(cache_key, region="snapshots")
                logger.info(f"快照已重置: {storage}")
                return True
            logger.debug(f"快照文件不存在，无需重置: {storage}")
            return True
        except Exception as e:
            logger.error(f"重置快照失败: {storage} - {e}")
            return False

    def force_full_scan(self, storage: str, mon_path: Path) -> bool:
        """
        强制全量扫描并处理所有文件（包括已存在的文件）
        :param storage: 存储名称
        :param mon_path: 监控路径
        :return: 是否成功
        """
        try:
            logger.info(f"开始强制全量扫描: {storage}:{mon_path}")

            # 生成快照
            new_snapshot = StorageChain().snapshot_storage(
                storage=storage,
                path=mon_path,
                last_snapshot_time=0  # 全量扫描，不使用增量
            )

            if new_snapshot is None:
                logger.warn(f"获取 {storage}:{mon_path} 快照失败")
                return False

            file_count = len(new_snapshot)
            logger.info(f"{storage}:{mon_path} 全量扫描完成，发现 {file_count} 个文件")

            # 处理所有文件
            processed_count = 0
            for file_path, file_info in new_snapshot.items():
                try:
                    if not self.__is_transfer_candidate_path(Path(file_path)):
                        continue
                    file_size = file_info.get('size', 0) if isinstance(file_info, dict) else file_info
                    if self.__handle_file(storage=storage, event_path=Path(file_path), file_size=file_size):
                        processed_count += 1
                except Exception as e:
                    logger.error(f"处理文件 {file_path} 失败: {e}")
                    continue

            logger.info(f"{storage}:{mon_path} 全量扫描完成，共处理 {processed_count}/{file_count} 个文件")

            # 保存快照
            self.save_snapshot(storage, new_snapshot, file_count)

            return True

        except Exception as e:
            logger.error(f"强制全量扫描失败: {storage}:{mon_path} - {e}")
            return False

    def load_snapshot(self, storage: str) -> Optional[Dict]:
        """
        从文件缓存加载快照
        :param storage: 存储名称
        :return: 快照数据或None
        """
        try:
            cache_key = f"{storage}_snapshot"
            snapshot_data = self._snapshot_cache.get(cache_key, region="snapshots")
            if snapshot_data:
                data = json.loads(snapshot_data.decode('utf-8'))
                logger.debug(f"成功加载快照: {storage}, 包含 {len(data.get('snapshot', {}))} 个文件")
                return data
            logger.debug(f"快照文件不存在: {storage}")
            return None
        except Exception as e:
            logger.error(f"加载快照失败: {e}")
            return None

    @staticmethod
    def adjust_monitor_interval(file_count: int) -> int:
        """
        根据文件数量动态调整监控间隔
        :param file_count: 文件数量
        :return: 监控间隔（分钟）
        """
        if file_count < 100:
            return 5  # 5分钟
        elif file_count < 500:
            return 10  # 10分钟
        elif file_count < 1000:
            return 15  # 15分钟
        else:
            return 30  # 30分钟

    @staticmethod
    def compare_snapshots(old_snapshot: Dict, new_snapshot: Dict) -> Dict[str, List]:
        """
        比对快照，找出变化的文件（只处理新增和修改，不处理删除）
        :param old_snapshot: 旧快照
        :param new_snapshot: 新快照
        :return: 变化信息
        """
        changes = {
            'added': [],
            'modified': []
        }

        old_files = set(old_snapshot.keys())
        new_files = set(new_snapshot.keys())

        # 新增文件
        changes['added'] = list(new_files - old_files)

        # 修改文件（大小或时间变化）
        for file_path in old_files & new_files:
            old_info = old_snapshot[file_path]
            new_info = new_snapshot[file_path]

            # 检查文件大小变化
            old_size = old_info.get('size', 0) if isinstance(old_info, dict) else old_info
            new_size = new_info.get('size', 0) if isinstance(new_info, dict) else new_info

            # 检查修改时间变化（如果有的话）
            old_time = old_info.get('modify_time', 0) if isinstance(old_info, dict) else 0
            new_time = new_info.get('modify_time', 0) if isinstance(new_info, dict) else 0

            if old_size != new_size or (old_time and new_time and old_time != new_time):
                changes['modified'].append(file_path)

        return changes

    @staticmethod
    def __is_bluray_sub(_path: Path) -> bool:
        """
        判断是否蓝光原盘目录内的媒体流文件。
        """
        return True if re.search(r"BDMV[/\\]STREAM", _path.as_posix(), re.IGNORECASE) else False

    @staticmethod
    def __get_bluray_dir(_path: Path) -> Optional[Path]:
        """
        获取蓝光原盘BDMV目录的上级目录。
        """
        for p in _path.parents:
            if p.name == "BDMV":
                return p.parent
        return None

    @staticmethod
    def __has_suffix_in(file_path: Path, extensions: List[str]) -> bool:
        """
        判断路径后缀是否命中给定扩展名列表。
        """
        if not file_path.suffix:
            return False
        return file_path.suffix.casefold() in {ext.casefold() for ext in extensions}

    def __is_transfer_candidate_path(self, file_path: Path) -> bool:
        """
        判断监控事件路径是否需要进入整理链。
        """
        if self.__has_suffix_in(file_path, settings.DOWNLOAD_TMPEXT):
            return False
        return self.__has_suffix_in(file_path, self.all_exts)

    @staticmethod
    def __build_transfer_src_path(event_path: Path, is_bluray_folder: bool) -> str:
        """
        生成整理记录使用的源路径。
        """
        if is_bluray_folder:
            return f"{event_path.as_posix()}/"
        return event_path.as_posix()

    @staticmethod
    def __has_transfer_history(storage: str, src_path: str) -> Optional[bool]:
        """
        判断源文件是否已经存在整理记录。
        """
        try:
            return bool(TransferHistoryOper().get_by_src(src_path, storage=storage))
        except Exception as err:
            logger.error(f"查询整理历史失败: {src_path} - {err}")
            return None

    @staticmethod
    def count_directory_files(directory: Path, max_check: int = 10000) -> int:
        """
        统计目录下的文件数量（用于检测是否超过系统限制）
        :param directory: 目录路径
        :param max_check: 最大检查数量，避免长时间阻塞
        :return: 文件数量
        """
        try:
            count = 0
            import os
            for root, dirs, files in os.walk(str(directory)):
                count += len(files)
                if count > max_check:
                    return count
            return count
        except Exception as err:
            logger.debug(f"统计目录文件数量失败: {err}")
            return 0

    @staticmethod
    def check_system_limits() -> Dict[str, Any]:
        """
        检查系统限制
        :return: 系统限制信息
        """
        limits = {
            'max_user_watches': 0,
            'max_user_instances': 0,
            'current_watches': 0,
            'warnings': []
        }

        try:
            system = platform.system()
            if system == 'Linux':
                # 检查 inotify 限制
                try:
                    with open('/proc/sys/fs/inotify/max_user_watches', 'r', encoding='utf-8', errors='replace') as f:
                        limits['max_user_watches'] = int(f.read().strip())
                except Exception as e:
                    logger.debug(f"读取 inotify 限制失败: {e}")
                    limits['max_user_watches'] = 8192  # 默认值

                try:
                    with open('/proc/sys/fs/inotify/max_user_instances', 'r', encoding='utf-8', errors='replace') as f:
                        limits['max_user_instances'] = int(f.read().strip())
                except Exception as e:
                    logger.debug(f"读取 inotify 实例限制失败: {e}")

                # 检查当前使用的watches
                try:
                    import subprocess
                    result = subprocess.run(['find', '/proc/*/fd', '-lname', 'anon_inode:inotify', '-printf', '%h\n'],
                                            capture_output=True, text=True, timeout=5)
                    if result.returncode == 0:
                        limits['current_watches'] = len(result.stdout.strip().split('\n'))
                except Exception as e:
                    logger.debug(f"检查当前 inotify 使用失败: {e}")

        except Exception as e:
            limits['warnings'].append(f"检查系统限制时出错: {e}")

        return limits

    @staticmethod
    def get_system_optimization_tips() -> List[str]:
        """
        获取系统优化建议
        :return: 优化建议列表
        """
        tips = []
        system = platform.system()

        if system == 'Linux':
            tips.extend([
                "增加 inotify 监控数量限制:",
                "echo fs.inotify.max_user_watches=524288 | sudo tee -a /etc/sysctl.conf",
                "echo fs.inotify.max_user_instances=524288 | sudo tee -a /etc/sysctl.conf",
                "sudo sysctl -p",
                "",
                "如果在Docker中运行，请在宿主机上执行以上命令"
            ])
        elif system == 'Darwin':
            tips.extend([
                "macOS 系统优化建议:",
                "sudo sysctl kern.maxfiles=65536",
                "sudo sysctl kern.maxfilesperproc=32768",
                "ulimit -n 32768"
            ])
        elif system == 'Windows':
            tips.extend([
                "Windows 系统优化建议:",
                "1. 关闭不必要的实时保护软件对监控目录的扫描",
                "2. 将监控目录添加到Windows Defender排除列表",
                "3. 确保有足够的可用内存"
            ])

        return tips

    @staticmethod
    def should_use_polling(directory: Path, monitor_mode: str,
                           file_count: int, limits: dict) -> tuple[bool, str]:
        """
        判断是否应该使用轮询模式
        :param directory: 监控目录
        :param monitor_mode: 配置的监控模式
        :param file_count: 目录文件数量
        :param limits: 系统限制信息
        :return: (是否使用轮询, 原因)
        """
        if monitor_mode == "compatibility":
            return True, "用户配置为兼容模式"

        # 检查网络文件系统
        if SystemUtils.is_network_filesystem(directory):
            return True, "检测到网络文件系统，建议使用兼容模式"

        max_watches = limits.get('max_user_watches')
        if max_watches and file_count > max_watches * 0.8:
            return True, f"目录文件数量({file_count})接近系统限制({max_watches})"
        return False, "使用快速模式"

    def init(self):
        """
        启动监控
        """
        # 停止现有任务
        self.stop()

        # 读取目录配置
        monitor_dirs = DirectoryHelper().get_download_dirs()
        if not monitor_dirs:
            logger.info("未找到任何目录监控配置")
            return

        # 按下载目录去重
        monitor_dirs = list({f"{d.storage}_{d.download_path}": d for d in monitor_dirs}.values())
        logger.info(f"找到 {len(monitor_dirs)} 个目录监控配置")

        # 启动定时服务进程
        self._scheduler = BackgroundScheduler(timezone=settings.TZ)

        messagehelper = MessageHelper()
        mon_storages = {}
        for mon_dir in monitor_dirs:
            if not mon_dir.library_path:
                logger.warn(f"跳过监控配置 {mon_dir.download_path}：未设置媒体库目录")
                continue
            if mon_dir.monitor_type != "monitor":
                logger.debug(f"跳过监控配置 {mon_dir.download_path}：监控类型为 {mon_dir.monitor_type}")
                continue

            # 检查媒体库目录是不是下载目录的子目录
            mon_path = Path(mon_dir.download_path)
            target_path = Path(mon_dir.library_path)
            if target_path.is_relative_to(mon_path):
                logger.warn(f"{target_path} 是监控目录 {mon_path} 的子目录，无法监控！")
                messagehelper.put(f"{target_path} 是监控目录 {mon_path} 的子目录，无法监控", title="目录监控")
                continue

            # 启动监控
            if mon_dir.storage == "local":
                # 本地目录监控
                logger.info(f"正在启动本地目录监控: {mon_path}")
                logger.info("*** 重要提示：目录监控只处理新增和修改的文件，不会处理监控启动前已存在的文件 ***")

                try:
                    # 统计文件数量并给出提示
                    file_count = self.count_directory_files(mon_path)
                    logger.info(f"监控目录 {mon_path} 包含约 {file_count} 个文件")

                    # 检查系统限制
                    limits = self.check_system_limits()

                    # 检查是否需要使用轮询模式
                    use_polling, reason = self.should_use_polling(mon_path,
                                                                  monitor_mode=mon_dir.monitor_mode,
                                                                  file_count=file_count,
                                                                  limits=limits)
                    logger.info(f"监控模式决策: {reason}")

                    mode_name = "兼容模式(轮询)" if use_polling else "快速模式"
                    logger.info(f"使用{mode_name}监控 {mon_path}")
                    if not use_polling:
                        if limits['warnings']:
                            for warning in limits['warnings']:
                                logger.warn(f"系统限制警告: {warning}")
                        if limits['max_user_watches'] > 0:
                            usage_percent = (file_count / limits['max_user_watches']) * 100
                            logger.info(
                                f"系统监控资源使用率: {usage_percent:.1f}% ({file_count}/{limits['max_user_watches']})")

                    watcher = LocalDirectoryWatcher(
                        mon_path=mon_path,
                        callback=self,
                        force_polling=True if use_polling else None
                    )
                    self._watchers.append(watcher)
                    watcher.start()

                    logger.info(f"✓ 本地目录监控已启动: {mon_path} [{mode_name}]")

                except Exception as e:
                    err_msg = str(e)
                    logger.error(f"启动本地目录监控失败: {mon_path}")
                    logger.error(f"错误详情: {err_msg}")

                    if "inotify" in err_msg.lower():
                        logger.error("inotify 相关错误，这通常是由于系统监控数量限制导致的")
                        logger.error("解决方案:")
                        tips = self.get_system_optimization_tips()
                        for tip in tips:
                            logger.error(f"  {tip}")
                        logger.error("执行上述命令后重启 MoviePilot")
                    elif "permission" in err_msg.lower():
                        logger.error("权限错误，请检查 MoviePilot 是否有足够的权限访问监控目录")
                    else:
                        logger.error("建议尝试使用兼容模式进行监控")

                    messagehelper.put(f"启动本地目录监控失败: {mon_path}\n错误: {err_msg}", title="目录监控")
            else:
                if not mon_storages.get(mon_dir.storage):
                    mon_storages[mon_dir.storage] = []
                mon_storages[mon_dir.storage].append(mon_path)

        for storage, paths in mon_storages.items():
            # 远程目录监控 - 使用智能间隔
            # 先尝试加载已有快照获取文件数量
            snapshot_data = self.load_snapshot(storage)
            file_count = snapshot_data.get('file_count', 0) if snapshot_data else 0
            interval = self.adjust_monitor_interval(file_count)
            for path in paths:
                logger.info(f"正在启动远程目录监控: {path} [{storage}]")
            logger.info("*** 重要提示：远程目录监控只处理新增和修改的文件，不会处理监控启动前已存在的文件 ***")
            logger.info(f"预估文件数量: {file_count}, 监控间隔: {interval}分钟")

            self._scheduler.add_job(
                self.polling_observer,
                'interval',
                minutes=interval,
                kwargs={
                    'storage': storage,
                    'mon_paths': paths
                },
                id=f"monitor_{storage}",
                replace_existing=True
            )
            logger.info(f"✓ 远程目录监控已启动: [间隔: {interval}分钟]")

        # 启动定时服务
        if self._scheduler.get_jobs():
            self._scheduler.print_jobs()
            self._scheduler.start()
            logger.info("定时监控服务已启动")

        # 输出监控总结
        local_count = len([d for d in monitor_dirs if d.storage == "local" and d.monitor_type == "monitor"])
        remote_count = len([d for d in monitor_dirs if d.storage != "local" and d.monitor_type == "monitor"])
        logger.info(f"目录监控启动完成: 本地监控 {local_count} 个，远程监控 {remote_count} 个")

    def polling_observer(self, storage: str, mon_paths: List[Path]):
        """
        轮询监控（改进版）
        """
        monitor_scope = ",".join(str(mon_path) for mon_path in mon_paths) or "未配置路径"
        with snapshot_lock:
            try:
                # 加载上次快照数据
                old_snapshot_data = self.load_snapshot(storage)
                old_snapshot = old_snapshot_data.get('snapshot', {}) if old_snapshot_data else {}
                last_snapshot_time = old_snapshot_data.get('timestamp', 0) if old_snapshot_data else 0

                # 判断是否为首次快照：检查快照文件是否存在且有效
                is_first_snapshot = old_snapshot_data is None
                new_snapshot = {}
                for mon_path in mon_paths:
                    logger.debug(f"开始对 {storage}:{mon_path} 进行快照...")

                    # 生成新快照（增量模式）
                    snapshot = StorageChain().snapshot_storage(
                        storage=storage,
                        path=mon_path,
                        last_snapshot_time=last_snapshot_time
                    )

                    if snapshot is None:
                        logger.warn(f"获取 {storage}:{mon_path} 快照失败")
                        continue
                    new_snapshot.update(snapshot)
                    file_count = len(snapshot)
                    logger.info(f"{storage}:{mon_path} 快照完成，发现 {file_count} 个文件")
                file_count = len(new_snapshot)
                if not is_first_snapshot:
                    # 比较快照找出变化
                    changes = self.compare_snapshots(old_snapshot, new_snapshot)
                    added_files = [
                        file_path
                        for file_path in changes['added']
                        if self.__is_transfer_candidate_path(Path(file_path))
                    ]
                    modified_files = [
                        file_path
                        for file_path in changes['modified']
                        if self.__is_transfer_candidate_path(Path(file_path))
                    ]

                    # 处理新增文件
                    handled_added_count = 0
                    for new_file in added_files:
                        file_info = new_snapshot.get(new_file, {})
                        file_size = file_info.get('size', 0) if isinstance(file_info, dict) else file_info
                        if self.__handle_file(storage=storage, event_path=Path(new_file), file_size=file_size):
                            handled_added_count += 1

                    # 处理修改文件
                    handled_modified_count = 0
                    for modified_file in modified_files:
                        file_info = new_snapshot.get(modified_file, {})
                        file_size = file_info.get('size', 0) if isinstance(file_info, dict) else file_info
                        if self.__handle_file(storage=storage, event_path=Path(modified_file), file_size=file_size):
                            handled_modified_count += 1

                    if handled_added_count or handled_modified_count:
                        logger.info(
                            f"{storage} 发现 {handled_added_count} 个新增文件，{handled_modified_count} 个修改文件")
                    else:
                        logger.debug(f"{storage} 无文件变化")
                else:
                    logger.info(f"{storage} 首次快照完成，共 {file_count} 个文件")
                    logger.info("*** 首次快照仅建立基准，不会处理现有文件。后续监控将处理新增和修改的文件 ***")

                # 保存新快照
                self.save_snapshot(storage, new_snapshot, file_count, last_snapshot_time)

                # 动态调整监控间隔
                new_interval = self.adjust_monitor_interval(file_count)
                current_job = self._scheduler.get_job(f"monitor_{storage}")
                if current_job and current_job.trigger.interval.total_seconds() / 60 != new_interval:
                    # 重新安排任务
                    self._scheduler.modify_job(
                        f"monitor_{storage}",
                        trigger='interval',
                        minutes=new_interval
                    )
                    logger.info(f"{storage}:{monitor_scope} 监控间隔已调整为 {new_interval} 分钟")

            except Exception as e:
                logger.error(f"轮询监控 {storage}:{monitor_scope} 出现错误：{e}")
                logger.debug(traceback.format_exc())

    def event_handler(self, event, text: str, event_path: str, file_size: float = None):
        """
        处理文件变化
        :param event: 事件
        :param text: 事件描述
        :param event_path: 事件文件路径
        :param file_size: 文件大小
        """
        if not event.is_directory:
            if not self.__is_transfer_candidate_path(Path(event_path)):
                return
            # 整理文件
            self.__handle_file(storage="local", event_path=Path(event_path), file_size=file_size)

    def __handle_file(self, storage: str, event_path: Path, file_size: float = None) -> bool:
        """
        整理一个文件
        :param storage: 存储
        :param event_path: 事件文件路径
        :param file_size: 文件大小
        :return: 是否进入整理链
        """
        # 全程加锁
        with lock:
            is_bluray_folder = False
            # 蓝光原盘文件处理
            if self.__is_bluray_sub(event_path):
                event_path = self.__get_bluray_dir(event_path)
                if not event_path:
                    return False
                is_bluray_folder = True
            elif not self.__is_transfer_candidate_path(event_path):
                return False

            # TTL缓存控重
            if self._cache.get(str(event_path)):
                return False
            self._cache[str(event_path)] = True

            src_path = self.__build_transfer_src_path(
                event_path=event_path,
                is_bluray_folder=is_bluray_folder,
            )
            has_transfer_history = self.__has_transfer_history(
                storage=storage,
                src_path=src_path,
            )
            if has_transfer_history is not False:
                return False

            try:
                if is_bluray_folder:
                    logger.info(f"开始整理蓝光原盘: {event_path}")
                else:
                    logger.info(f"开始整理文件: {event_path}")
                # 开始整理
                TransferChain().do_transfer(
                    fileitem=FileItem(
                        storage=storage,
                        path=src_path,
                        type="file" if not is_bluray_folder else "dir",
                        name=event_path.name,
                        basename=event_path.stem,
                        extension=event_path.suffix[1:],
                        size=file_size
                    )
                )
                return True
            except Exception as e:
                logger.error("目录监控整理文件发生错误：%s - %s" % (str(e), traceback.format_exc()))
                return False

    def stop(self):
        """
        退出监控
        """
        if self._watchers:
            logger.info("正在停止本地目录监控服务...")
            for watcher in self._watchers:
                try:
                    watcher.stop()
                    watcher.join(timeout=5)
                    if watcher.is_alive():
                        logger.warning(f"本地目录监控线程在5秒内未能停止: {watcher.watch_path}")
                    else:
                        logger.debug(f"已停止本地目录监控服务: {watcher.watch_path}")
                except Exception as e:
                    logger.error(f"停止目录监控服务出现了错误：{e}")
            self._watchers = []
            logger.info("本地目录监控服务已停止")
        if self._scheduler:
            self._scheduler.remove_all_jobs()
            if self._scheduler.running:
                try:
                    self._scheduler.shutdown()
                    logger.info("定时监控服务已停止")
                except Exception as e:
                    logger.error(f"停止定时服务出现了错误：{e}")
            self._scheduler = None
        if self._cache:
            self._cache.close()
        if self._snapshot_cache:
            self._snapshot_cache.close()
