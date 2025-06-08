import gc
import threading
import time
from datetime import datetime
from typing import Optional

import psutil
from pympler import muppy, summary, asizeof

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.log import logger
from app.schemas import ConfigChangeEventData
from app.schemas.types import EventType
from app.utils.singleton import Singleton


class MemoryHelper(metaclass=Singleton):
    """
    内存管理工具类，用于监控和优化内存使用
    """

    def __init__(self):
        # 检查间隔(秒) - 从配置获取，默认5分钟
        self._check_interval = settings.MEMORY_SNAPSHOT_INTERVAL * 60
        self._monitoring = False
        self._monitor_thread: Optional[threading.Thread] = None
        # 内存快照保存目录
        self._memory_snapshot_dir = settings.LOG_PATH / "memory_snapshots"
        # 保留的快照文件数量
        self._keep_count = settings.MEMORY_SNAPSHOT_KEEP_COUNT

    @eventmanager.register(EventType.ConfigChanged)
    def handle_config_changed(self, event: Event):
        """
        处理配置变更事件，更新内存监控设置
        :param event: 事件对象
        """
        if not event:
            return
        event_data: ConfigChangeEventData = event.event_data
        if event_data.key not in ['MEMORY_ANALYSIS', 'MEMORY_SNAPSHOT_INTERVAL', 'MEMORY_SNAPSHOT_KEEP_COUNT']:
            return

        # 更新配置
        if event_data.key == 'MEMORY_SNAPSHOT_INTERVAL':
            self._check_interval = settings.MEMORY_SNAPSHOT_INTERVAL * 60
        elif event_data.key == 'MEMORY_SNAPSHOT_KEEP_COUNT':
            self._keep_count = settings.MEMORY_SNAPSHOT_KEEP_COUNT
        self.stop_monitoring()
        self.start_monitoring()

    def start_monitoring(self):
        """
        开始内存监控
        """
        if not settings.MEMORY_ANALYSIS:
            return
        if self._monitoring:
            return

        # 创建内存快照目录
        self._memory_snapshot_dir.mkdir(parents=True, exist_ok=True)

        # 初始化内存分析器
        self._monitoring = True
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()
        logger.info("内存监控已启动")

    def stop_monitoring(self):
        """
        停止内存监控
        """
        self._monitoring = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=5)

        logger.info("内存监控已停止")

    def _monitor_loop(self):
        """
        内存监控循环
        """
        logger.info("内存监控循环开始")
        while self._monitoring:
            try:
                # 生成内存快照
                self._create_memory_snapshot()
                time.sleep(self._check_interval)
            except Exception as e:
                logger.error(f"内存监控出错: {e}")
                # 出错后等待1分钟再继续
                time.sleep(60)

        logger.info("内存监控循环结束")

    def _create_memory_snapshot(self):
        """
        创建内存快照并保存到文件
        """
        try:
            # 获取当前时间戳
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            snapshot_file = self._memory_snapshot_dir / f"memory_snapshot_{timestamp}.txt"

            # 获取系统内存使用情况
            memory_usage = psutil.Process().memory_info().rss

            logger.info(f"开始创建内存快照: {snapshot_file}")

            # 第一步：写入基本信息和对象类型统计
            self._write_basic_info(snapshot_file, memory_usage)

            # 第二步：分析并写入类实例内存使用情况
            self._append_class_analysis(snapshot_file)

            # 第三步：分析并写入大内存变量详情
            self._append_variable_analysis(snapshot_file)

            logger.info(f"内存快照已保存: {snapshot_file}, 当前内存使用: {memory_usage / 1024 / 1024:.2f} MB")

            # 清理过期的快照文件（保留最近30个）
            self._cleanup_old_snapshots()

        except Exception as e:
            logger.error(f"创建内存快照失败: {e}")

    @staticmethod
    def _write_basic_info(snapshot_file, memory_usage):
        """
        写入基本信息和对象类型统计
        """
        # 获取当前进程的内存使用情况
        all_objects = muppy.get_objects()
        sum1 = summary.summarize(all_objects)

        with open(snapshot_file, 'w', encoding='utf-8') as f:
            f.write(f"内存快照时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"当前进程内存使用: {memory_usage / 1024 / 1024:.2f} MB\n")
            f.write("=" * 80 + "\n")
            f.write("对象类型统计:\n")
            f.write("-" * 80 + "\n")

            # 写入对象统计信息
            for line in summary.format_(sum1):
                f.write(line + "\n")

            # 立即刷新到磁盘
            f.flush()

        logger.debug("基本信息已写入快照文件")

    def _append_class_analysis(self, snapshot_file):
        """
        分析并追加类实例内存使用情况
        """
        with open(snapshot_file, 'a', encoding='utf-8') as f:
            f.write("\n" + "=" * 80 + "\n")
            f.write("类实例内存使用情况 (按内存大小排序):\n")
            f.write("-" * 80 + "\n")
            f.write("正在分析中...\n")
            # 立即刷新，让用户知道这部分开始了
            f.flush()

        try:
            logger.debug("开始分析类实例内存使用情况")
            class_objects = self._get_class_memory_usage()

            # 重新打开文件，移除"正在分析中..."并写入实际结果
            with open(snapshot_file, 'r', encoding='utf-8') as f:
                content = f.read()

            # 替换"正在分析中..."
            content = content.replace("正在分析中...\n", "")

            with open(snapshot_file, 'w', encoding='utf-8') as f:
                f.write(content)

                if class_objects:
                    # 只显示前100个类
                    for i, class_info in enumerate(class_objects[:100], 1):
                        f.write(f"{i:3d}. {class_info['name']:<50} "
                                f"{class_info['size_mb']:>8.2f} MB ({class_info['count']} 个实例)\n")
                else:
                    f.write("未找到有效的类实例信息\n")

                f.flush()

        except Exception as e:
            logger.error(f"获取类实例信息失败: {e}")

            # 即使出错也要更新文件
            with open(snapshot_file, 'r', encoding='utf-8') as f:
                content = f.read()

            content = content.replace("正在分析中...\n", f"获取类实例信息失败: {e}\n")

            with open(snapshot_file, 'w', encoding='utf-8') as f:
                f.write(content)
                f.flush()

        logger.debug("类实例分析已完成并写入")

    def _append_variable_analysis(self, snapshot_file):
        """
        分析并追加大内存变量详情
        """
        with open(snapshot_file, 'a', encoding='utf-8') as f:
            f.write("\n" + "=" * 80 + "\n")
            f.write("大内存变量详情 (前100个):\n")
            f.write("-" * 80 + "\n")
            f.write("正在分析中...\n")
            # 立即刷新，让用户知道这部分开始了
            f.flush()

        try:
            logger.debug("开始分析大内存变量")
            large_variables = self._get_large_variables(100)

            # 重新打开文件，移除"正在分析中..."并写入实际结果
            with open(snapshot_file, 'r', encoding='utf-8') as f:
                content = f.read()

            # 替换最后的"正在分析中..."
            content = content.replace("正在分析中...\n", "")

            with open(snapshot_file, 'w', encoding='utf-8') as f:
                f.write(content)

                if large_variables:
                    for i, var_info in enumerate(large_variables, 1):
                        f.write(
                            f"{i:3d}. {var_info['name']:<30} {var_info['type']:<15} {var_info['size_mb']:>8.2f} MB\n")
                else:
                    f.write("未找到大内存变量\n")

                f.flush()

        except Exception as e:
            logger.error(f"获取大内存变量信息失败: {e}")

            # 即使出错也要更新文件
            with open(snapshot_file, 'r', encoding='utf-8') as f:
                content = f.read()

            content = content.replace("正在分析中...\n", f"获取变量信息失败: {e}\n")

            with open(snapshot_file, 'w', encoding='utf-8') as f:
                f.write(content)
                f.flush()

        logger.debug("大内存变量分析已完成并写入")

    def _cleanup_old_snapshots(self):
        """
        清理过期的内存快照文件，只保留最近的指定数量文件
        """
        try:
            snapshot_files = list(self._memory_snapshot_dir.glob("memory_snapshot_*.txt"))
            if len(snapshot_files) > self._keep_count:
                # 按修改时间排序，删除最旧的文件
                snapshot_files.sort(key=lambda x: x.stat().st_mtime)
                for old_file in snapshot_files[:-self._keep_count]:
                    old_file.unlink()
                    logger.debug(f"已删除过期内存快照: {old_file}")
        except Exception as e:
            logger.error(f"清理过期快照失败: {e}")

    @staticmethod
    def _get_class_memory_usage():
        """
        获取所有类实例的内存使用情况，按内存大小排序
        """
        class_info = {}
        processed_count = 0
        error_count = 0

        # 获取所有对象
        all_objects = muppy.get_objects()
        logger.debug(f"开始分析 {len(all_objects)} 个对象的类实例内存使用情况")

        for obj in all_objects:
            try:
                # 跳过类对象本身，统计类的实例
                if isinstance(obj, type):
                    continue

                # 获取对象的类名 - 这里可能会出错
                obj_class = type(obj)

                # 安全地获取类名
                try:
                    if hasattr(obj_class, '__module__') and hasattr(obj_class, '__name__'):
                        class_name = f"{obj_class.__module__}.{obj_class.__name__}"
                    else:
                        class_name = str(obj_class)
                except Exception as e:
                    # 如果获取类名失败，使用简单的类型描述
                    class_name = f"<unknown_class_{id(obj_class)}>"
                    logger.debug(f"获取类名失败: {e}")

                # 计算对象的内存使用
                size_bytes = asizeof.asizeof(obj)
                if size_bytes < 100:  # 跳过太小的对象
                    continue

                size_mb = size_bytes / 1024 / 1024
                processed_count += 1

                if class_name in class_info:
                    class_info[class_name]['size_mb'] += size_mb
                    class_info[class_name]['count'] += 1
                else:
                    class_info[class_name] = {
                        'name': class_name,
                        'size_mb': size_mb,
                        'count': 1
                    }

            except Exception as e:
                # 捕获所有可能的异常，包括SQLAlchemy、ORM等框架的异常
                error_count += 1
                if error_count <= 5:  # 只记录前5个错误，避免日志过多
                    logger.debug(f"分析对象时出错: {e}")
                continue

        logger.debug(f"类实例分析完成: 处理了 {processed_count} 个对象, 遇到 {error_count} 个错误")

        # 按内存大小排序
        sorted_classes = sorted(class_info.values(), key=lambda x: x['size_mb'], reverse=True)
        return sorted_classes

    def _get_large_variables(self, limit=100):
        """
        获取大内存变量信息，按内存大小排序
        """
        large_vars = []
        processed_count = 0

        # 获取所有对象
        all_objects = muppy.get_objects()
        logger.debug(f"开始分析 {len(all_objects)} 个对象的内存使用情况")

        for obj in all_objects:
            # 跳过类对象
            if isinstance(obj, type):
                continue

            try:
                # 计算对象大小
                size_bytes = asizeof.asizeof(obj)

                # 只处理大于10KB的对象，提高分析效率
                if size_bytes < 10240:
                    continue

                size_mb = size_bytes / 1024 / 1024
                processed_count += 1

                # 获取对象信息
                var_info = self._get_variable_info(obj, size_mb)
                if var_info:
                    large_vars.append(var_info)

                # 如果已经找到足够多的大对象，可以提前结束
                if len(large_vars) >= limit * 2:  # 多收集一些，后面排序筛选
                    break

            except Exception as e:
                # 更广泛的异常捕获
                logger.debug(f"分析对象失败: {e}")
                continue

        logger.debug(f"处理了 {processed_count} 个大对象，找到 {len(large_vars)} 个有效变量")

        # 按内存大小排序并返回前N个
        large_vars.sort(key=lambda x: x['size_mb'], reverse=True)
        return large_vars[:limit]

    def _get_variable_info(self, obj, size_mb):
        """
        获取变量的描述信息
        """
        try:
            obj_type = type(obj).__name__

            # 尝试获取变量名
            var_name = self._get_variable_name(obj)

            # 生成描述性信息
            if isinstance(obj, dict):
                key_count = len(obj)
                if key_count > 0:
                    sample_keys = list(obj.keys())[:3]
                    var_name += f" ({key_count}项, 键: {sample_keys})"
            elif isinstance(obj, (list, tuple, set)):
                var_name += f" ({len(obj)}个元素)"
            elif isinstance(obj, str):
                if len(obj) > 50:
                    var_name += f" (长度: {len(obj)}, 内容: '{obj[:50]}...')"
                else:
                    var_name += f" ('{obj}')"
            elif hasattr(obj, '__class__') and hasattr(obj.__class__, '__name__'):
                if hasattr(obj, '__dict__'):
                    attr_count = len(obj.__dict__)
                    var_name += f" ({attr_count}个属性)"

            return {
                'name': var_name,
                'type': obj_type,
                'size_mb': size_mb
            }

        except Exception as e:
            logger.debug(f"获取变量信息失败: {e}")
            return None

    @staticmethod
    def _get_variable_name(obj):
        """
        尝试获取变量名
        """
        try:
            # 尝试通过gc获取引用该对象的变量名
            referrers = gc.get_referrers(obj)

            for referrer in referrers:
                if isinstance(referrer, dict):
                    # 检查是否在某个模块的全局变量中
                    for name, value in referrer.items():
                        if value is obj and isinstance(name, str):
                            return name
                elif hasattr(referrer, '__dict__'):
                    # 检查是否在某个实例的属性中
                    for name, value in referrer.__dict__.items():
                        if value is obj and isinstance(name, str):
                            return f"{type(referrer).__name__}.{name}"

            # 如果找不到变量名，返回对象类型和id
            return f"{type(obj).__name__}_{id(obj)}"

        except Exception as e:
            logger.debug(f"获取变量名失败: {e}")
            return f"{type(obj).__name__}_{id(obj)}"
