import json
import platform
import re
import subprocess
import threading
import time
import traceback
from pathlib import Path
from threading import Lock
from typing import Any, Optional, Dict, List

from apscheduler.schedulers.background import BackgroundScheduler
from cachetools import TTLCache
from watchdog.events import FileSystemEventHandler, FileSystemMovedEvent, FileSystemEvent
from watchdog.observers.polling import PollingObserver

from app.chain import ChainBase
from app.chain.storage import StorageChain
from app.chain.transfer import TransferChain
from app.core.config import settings
from app.core.event import Event, eventmanager
from app.helper.directory import DirectoryHelper
from app.helper.message import MessageHelper
from app.log import logger
from app.schemas import ConfigChangeEventData
from app.schemas import FileItem
from app.schemas.types import SystemConfigKey, EventType
from app.utils.singleton import Singleton

lock = Lock()
snapshot_lock = Lock()


class MonitorChain(ChainBase):
    pass


class FileMonitorHandler(FileSystemEventHandler):
    """
    目录监控响应类
    """

    def __init__(self, mon_path: Path, callback: Any, **kwargs):
        super(FileMonitorHandler, self).__init__(**kwargs)
        self._watch_path = mon_path
        self.callback = callback

    def on_created(self, event: FileSystemEvent):
        self.callback.event_handler(event=event, text="创建", event_path=event.src_path,
                                    file_size=Path(event.src_path).stat().st_size)

    def on_moved(self, event: FileSystemMovedEvent):
        self.callback.event_handler(event=event, text="移动", event_path=event.dest_path,
                                    file_size=Path(event.dest_path).stat().st_size)


class Monitor(metaclass=Singleton):
    """
    目录监控处理链，单例模式
    """



    def __init__(self):
        super().__init__()
        # 退出事件
        self._event = threading.Event()
        # 监控服务
        self._observers = []
        # 定时服务
        self._scheduler = None
        # 存储快照缓存目录
        self._snapshot_cache_dir = None
        # 存储过照间隔（分钟）
        self._snapshot_interval = 5
        # TTL缓存，10秒钟有效
        self._cache = TTLCache(maxsize=1024, ttl=10)
        # 监控的文件扩展名
        self.all_exts = settings.RMT_MEDIAEXT
        # 初始化快照缓存目录
        self._snapshot_cache_dir = settings.TEMP_PATH / "snapshots"
        self._snapshot_cache_dir.mkdir(exist_ok=True)
        # 启动目录监控和文件整理
        self.init()

    @eventmanager.register(EventType.ConfigChanged)
    def handle_config_changed(self, event: Event):
        """
        处理配置变更事件
        :param event: 事件对象
        """
        if not event:
            return
        event_data: ConfigChangeEventData = event.event_data
        if event_data.key not in [SystemConfigKey.Directories.value]:
            return
        logger.info("配置变更事件触发，重新初始化目录监控...")
        self.init()

    def save_snapshot(self, storage: str, snapshot: Dict, file_count: int = 0,
                      last_snapshot_time: Optional[float] = None):
        """
        保存快照到文件
        :param storage: 存储名称
        :param snapshot: 快照数据
        :param last_snapshot_time: 上次快照时间戳
        :param file_count: 文件数量，用于调整监控间隔
        """
        try:
            cache_file = self._snapshot_cache_dir / f"{storage}_snapshot.json"
            snapshot_time = max((item.get('modify_time', 0) for item in snapshot.values()), default=None)
            if snapshot_time is None:
                snapshot_time = last_snapshot_time or time.time()
            snapshot_data = {
                'timestamp': snapshot_time,
                'file_count': file_count,
                'snapshot': snapshot
            }
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(snapshot_data, f, ensure_ascii=False, indent=2)  # noqa
            logger.debug(f"快照已保存到 {cache_file}")
        except Exception as e:
            logger.error(f"保存快照失败: {e}")

    def reset_snapshot(self, storage: str) -> bool:
        """
        重置快照，强制下次扫描时重新建立基准
        :param storage: 存储名称
        :return: 是否成功
        """
        try:
            cache_file = self._snapshot_cache_dir / f"{storage}_snapshot.json"
            if cache_file.exists():
                cache_file.unlink()
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
                    logger.info(f"处理文件：{file_path}")
                    file_size = file_info.get('size', 0) if isinstance(file_info, dict) else file_info
                    self.__handle_file(storage=storage, event_path=Path(file_path), file_size=file_size)
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
        从文件加载快照
        :param storage: 存储名称
        :return: 快照数据或None
        """
        try:
            cache_file = self._snapshot_cache_dir / f"{storage}_snapshot.json"
            if cache_file.exists():
                with open(cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    logger.debug(f"成功加载快照: {cache_file}, 包含 {len(data.get('snapshot', {}))} 个文件")
                    return data
            logger.debug(f"快照文件不存在: {cache_file}")
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
                    with open('/proc/sys/fs/inotify/max_user_watches', 'r') as f:
                        limits['max_user_watches'] = int(f.read().strip())
                except Exception as e:
                    logger.debug(f"读取 inotify 限制失败: {e}")
                    limits['max_user_watches'] = 8192  # 默认值

                try:
                    with open('/proc/sys/fs/inotify/max_user_instances', 'r') as f:
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

    def should_use_polling(self, directory: Path, monitor_mode: str,
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
        if self.is_network_filesystem(directory):
            return True, "检测到网络文件系统，建议使用兼容模式"

        max_watches = limits.get('max_user_watches')
        if max_watches and file_count > max_watches * 0.8:
            return True, f"目录文件数量({file_count})接近系统限制({max_watches})"
        return False, "使用快速模式"

    @staticmethod
    def is_network_filesystem(directory: Path) -> bool:
        """
        检测是否为网络文件系统
        :param directory: 目录路径
        :return: 是否为网络文件系统
        """
        try:
            system = platform.system()
            if system == 'Linux':
                # 检查挂载信息
                result = subprocess.run(['df', '-T', str(directory)],
                                        capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    output = result.stdout.lower()
                    # 以下本地文件系统含有fuse关键字
                    local_fs = [
                        "fuse.shfs",  # Unraid
                        "zfuse.zfsv",  # 极空间(zfuse.zfsv2、zfuse.zfsv3、...)
                        # TBD
                    ]
                    if any(fs in output for fs in local_fs):
                        return False
                    network_fs = ['nfs', 'cifs', 'smbfs', 'fuse', 'sshfs', 'ftpfs']
                    return any(fs in output for fs in network_fs)
            elif system == 'Darwin':
                # macOS 检查
                result = subprocess.run(['df', '-T', str(directory)],
                                        capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    output = result.stdout.lower()
                    return 'nfs' in output or 'smbfs' in output
            elif system == 'Windows':
                # Windows 检查网络驱动器
                return str(directory).startswith('\\\\')
        except Exception as e:
            logger.debug(f"检测网络文件系统时出错: {e}")
        return False

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

                    if use_polling:
                        observer = PollingObserver()
                        logger.info(f"使用兼容模式(轮询)监控 {mon_path}")
                    else:
                        observer = self.__choose_observer()
                        if observer is None:
                            logger.warn(f"快速模式不可用，自动切换到兼容模式监控 {mon_path}")
                            observer = PollingObserver()
                        else:
                            logger.info(f"使用快速模式监控 {mon_path}")
                            if limits['warnings']:
                                for warning in limits['warnings']:
                                    logger.warn(f"系统限制警告: {warning}")
                            if limits['max_user_watches'] > 0:
                                usage_percent = (file_count / limits['max_user_watches']) * 100
                                logger.info(
                                    f"系统监控资源使用率: {usage_percent:.1f}% ({file_count}/{limits['max_user_watches']})")

                    self._observers.append(observer)
                    observer.schedule(FileMonitorHandler(mon_path=mon_path, callback=self),
                                      path=str(mon_path),
                                      recursive=True)
                    observer.daemon = True
                    observer.start()

                    mode_name = "兼容模式(轮询)" if use_polling else "快速模式"
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

    def __choose_observer(self) -> Optional[Any]:
        """
        选择最优的监控模式（带错误处理和自动回退）
        """
        system = platform.system()

        observers_to_try = []

        try:
            if system == 'Linux':
                observers_to_try = [
                    ('InotifyObserver',
                     lambda: self.__try_import_observer('watchdog.observers.inotify', 'InotifyObserver')),
                ]
            elif system == 'Darwin':
                observers_to_try = [
                    ('FSEventsObserver',
                     lambda: self.__try_import_observer('watchdog.observers.fsevents', 'FSEventsObserver')),
                ]
            elif system == 'Windows':
                observers_to_try = [
                    ('WindowsApiObserver',
                     lambda: self.__try_import_observer('watchdog.observers.read_directory_changes',
                                                        'WindowsApiObserver')),
                ]

            # 尝试每个观察者
            for observer_name, observer_func in observers_to_try:
                try:
                    observer_class = observer_func()
                    if observer_class:
                        # 尝试创建实例以验证是否可用
                        test_observer = observer_class()
                        test_observer.stop()  # 立即停止测试实例
                        logger.debug(f"成功初始化 {observer_name}")
                        return observer_class()
                except Exception as e:
                    logger.debug(f"初始化 {observer_name} 失败: {e}")
                    continue

        except Exception as e:
            logger.debug(f"选择观察者时出错: {e}")

        logger.debug("所有快速监控模式都不可用，将使用兼容模式")
        return None

    @staticmethod
    def __try_import_observer(module_name: str, class_name: str):
        """
        尝试导入观察者类
        """
        try:
            module = __import__(module_name, fromlist=[class_name])
            return getattr(module, class_name)
        except (ImportError, AttributeError) as e:
            logger.debug(f"导入 {module_name}.{class_name} 失败: {e}")
            return None

    def polling_observer(self, storage: str, mon_paths: List[Path]):
        """
        轮询监控（改进版）
        """
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

                    # 处理新增文件
                    for new_file in changes['added']:
                        logger.info(f"发现新增文件：{new_file}")
                        file_info = new_snapshot.get(new_file, {})
                        file_size = file_info.get('size', 0) if isinstance(file_info, dict) else file_info
                        self.__handle_file(storage=storage, event_path=Path(new_file), file_size=file_size)

                    # 处理修改文件
                    for modified_file in changes['modified']:
                        logger.info(f"发现修改文件：{modified_file}")
                        file_info = new_snapshot.get(modified_file, {})
                        file_size = file_info.get('size', 0) if isinstance(file_info, dict) else file_info
                        self.__handle_file(storage=storage, event_path=Path(modified_file), file_size=file_size)

                    if changes['added'] or changes['modified']:
                        logger.info(
                            f"{storage} 发现 {len(changes['added'])} 个新增文件，{len(changes['modified'])} 个修改文件")
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
                    logger.info(f"{storage}:{mon_path} 监控间隔已调整为 {new_interval} 分钟")

            except Exception as e:
                logger.error(f"轮询监控 {storage}:{mon_path} 出现错误：{e}")
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
            # 文件发生变化
            logger.debug(f"检测到文件变化: {event_path} [{text}]")
            # 整理文件
            self.__handle_file(storage="local", event_path=Path(event_path), file_size=file_size)

    def __handle_file(self, storage: str, event_path: Path, file_size: float = None):
        """
        整理一个文件
        :param storage: 存储
        :param event_path: 事件文件路径
        :param file_size: 文件大小
        """

        def __is_bluray_sub(_path: Path) -> bool:
            """
            判断是否蓝光原盘目录内的子目录或文件
            """
            return True if re.search(r"BDMV[/\\]STREAM", str(_path), re.IGNORECASE) else False

        def __get_bluray_dir(_path: Path) -> Optional[Path]:
            """
            获取蓝光原盘BDMV目录的上级目录
            """
            for p in _path.parents:
                if p.name == "BDMV":
                    return p.parent
            return None

        # 全程加锁
        with lock:
            # 蓝光原盘文件处理
            if __is_bluray_sub(event_path):
                event_path = __get_bluray_dir(event_path)
                if not event_path:
                    return

            # TTL缓存控重
            if self._cache.get(str(event_path)):
                logger.debug(f"文件 {event_path} 在缓存中，跳过处理")
                return
            self._cache[str(event_path)] = True

            try:
                logger.info(f"开始整理文件: {event_path}")
                # 开始整理
                TransferChain().do_transfer(
                    fileitem=FileItem(
                        storage=storage,
                        path=str(event_path).replace("\\", "/"),
                        type="file",
                        name=event_path.name,
                        basename=event_path.stem,
                        extension=event_path.suffix[1:],
                        size=file_size
                    )
                )
            except Exception as e:
                logger.error("目录监控整理文件发生错误：%s - %s" % (str(e), traceback.format_exc()))

    def stop(self):
        """
        退出插件
        """
        self._event.set()
        if self._observers:
            logger.info("正在停止本地目录监控服务...")
            for observer in self._observers:
                try:
                    observer.stop()
                    observer.join()
                    logger.debug(f"已停止监控服务: {observer}")
                except Exception as e:
                    logger.error(f"停止目录监控服务出现了错误：{e}")
            self._observers = []
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
        self._event.clear()
