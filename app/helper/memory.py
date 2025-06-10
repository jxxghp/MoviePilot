import gc
import json
import shutil
import sys
import threading
import time
import tracemalloc
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List, Any, Tuple

import psutil
from pympler import muppy, asizeof

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
        self._keep_count = settings.MEMORY_SNAPSHOT_AND_LOG_KEEP_COUNT
        self._check_interval = settings.MEMORY_SNAPSHOT_INTERVAL * 60
        self._snapshot_lock = threading.Lock()
        self._monitoring = False
        self._monitor_thread: Optional[threading.Thread] = None
        self._tracemalloc_monitoring = False
        self._tracemalloc_depth = 15

        # 日志和配置目录
        log_base_dir = settings.LOG_PATH / "memory"
        config_base_dir = settings.CONFIG_PATH / "memory"
        # 所有快照数据存放目录
        self._snapshot_data_dir = config_base_dir / "snapshots"
        # 报告按类型分目录
        self._anatomy_reports_dir = log_base_dir / "anatomy_reports"
        self._diff_reports_dir = log_base_dir / "diff_reports"

    @eventmanager.register(EventType.ConfigChanged)
    def handle_config_changed(self, event: Event):
        """
        处理配置变更事件，更新内存监控设置
        :param event: 事件对象
        """
        if not event:
            return
        event_data: ConfigChangeEventData = event.event_data
        config_key = event_data.key
        if config_key == 'MEMORY_ANALYSIS':
            if settings.MEMORY_ANALYSIS:
                self.start_monitoring()
            else:
                self.stop_monitoring()
        elif config_key == 'MEMORY_SNAPSHOT_INTERVAL':
            self._check_interval = settings.MEMORY_SNAPSHOT_INTERVAL * 60
            logger.info(f"内存快照间隔已更新为: {self._check_interval} 秒")
        elif config_key == 'MEMORY_SNAPSHOT_AND_LOG_KEEP_COUNT':
            self._keep_count = settings.MEMORY_SNAPSHOT_AND_LOG_KEEP_COUNT
            logger.info(f"快照目录保留数量已更新为: {self._keep_count}")
        elif config_key == 'MEMORY_TRACEMALLOC':
            if settings.MEMORY_TRACEMALLOC:
                self.start_tracemalloc_monitoring()
            else:
                self.stop_tracemalloc_monitoring()

    def generate_diff_report(self, ts1: str, ts2: str, report_name: str = None) -> Optional[Path]:
        """
        接收两个时间戳字符串进行对比
        """
        try:
            logger.info(f"开始生成增量归因报告: {ts1} vs {ts2}")
            self._diff_reports_dir.mkdir(parents=True, exist_ok=True)

            if not report_name:
                report_name = f"diff_{ts1}_vs_{ts2}.txt"
            report_path = self._diff_reports_dir / report_name

            pympler_diff = self.compare_snapshots(ts1, ts2)
            if "error" in pympler_diff:
                raise ValueError(f"Pympler 快照对比失败: {pympler_diff['error']}")

            app_filter = [
                # 从中排除 memory_helper 自身
                tracemalloc.Filter(False, "*/helper/memory.py")
            ]

            # 当关闭全局 Tracemalloc 时，仍然允许 app 目录下的代码进行内存跟踪
            if not settings.MEMORY_TRACEMALLOC_GLOBAL:
                app_filter.append(tracemalloc.Filter(True, "*/app/*"))

            tracemalloc_diff = self.compare_tracemalloc_snapshots(ts1, ts2, filters=app_filter)

            with open(report_path, 'w', encoding='utf-8') as f:
                f.write("=" * 80 + "\n")
                f.write(" 内存增量归因报告 (Attribution Report)\n")
                f.write("=" * 80 + "\n\n")
                meta = pympler_diff.get("metadata", {})
                f.write(f"对比周期: {meta.get('before_time')} -> {meta.get('after_time')}\n")
                f.write(f"对比文件: 快照 {ts1} vs {ts2}\n")
                f.write(f"总内存变化 (RSS): {meta.get('memory_diff_mb', 0):+.2f} MB\n\n")
                f.write("-" * 30 + " 现象归因 (Pympler) " + "-" * 29 + "\n\n")
                self._write_diff_section(f, "增长最快的类", pympler_diff.get("class_diff", {}).get("increased", []))
                self._write_diff_section(f, "内存增加的变量",
                                         pympler_diff.get("variable_diff", {}).get("increased", []))
                self._write_diff_section(f, "新增的大变量", pympler_diff.get("variable_diff", {}).get("new", []))
                f.write("\n" + "-" * 30 + " 源头归因 (Tracemalloc) " + "-" * 26 + "\n\n")
                if tracemalloc_diff:
                    f.write("内存增长最快的代码位置 (Top 20, 仅限 app 目录):\n")
                    for i, stat in enumerate(tracemalloc_diff, 1):
                        f.write(f"{i:3d}. {stat['filename']}:{stat['lineno']}\n")
                        f.write(f"     - 内存增长: {stat['size_diff_kb']:+.2f} KB\n")
                        f.write(f"     - 新增对象: {stat['count_diff']:+}\n")
                else:
                    f.write("无 Tracemalloc 增量数据或对比失败。\n")
            logger.info(f"增量归因报告已生成: {report_path}")
            return report_path
        except Exception as e:
            logger.error(f"生成增量归因报告失败: {e}", exc_info=True)
            return None

    def generate_anatomy_report(self, timestamp: str) -> Optional[Path]:
        """
        接收时间戳字符串生成 Pympler 剖析报告
        :param timestamp: 快照时间戳字符串
        """
        try:
            logger.info(f"开始生成 Pympler 深度剖析报告: 快照 {timestamp}")
            report_dir = self._anatomy_reports_dir / timestamp
            report_dir.mkdir(parents=True, exist_ok=True)

            json_path = self._snapshot_data_dir / timestamp / "pympler.json"
            if not json_path.exists():
                raise FileNotFoundError(f"Pympler JSON 快照文件不存在: {json_path}")
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            report_path = report_dir / "pympler_report.txt"
            self._write_report_from_data(report_path, data)
            logger.info(f"Pympler 深度剖析报告已生成: {report_path}")
            return report_path
        except Exception as e:
            logger.error(f"生成 Pympler 深度剖析报告失败: {e}", exc_info=True)
            return None

    def generate_tracemalloc_anatomy_report(self, timestamp: str) -> Optional[Path]:
        """
        接收时间戳字符串生成 Tracemalloc 剖析报告
        :param timestamp: 快照时间戳字符串
        """
        try:
            logger.info(f"开始生成 Tracemalloc 剖析报告: 快照 {timestamp}")
            report_dir = self._anatomy_reports_dir / timestamp
            report_dir.mkdir(parents=True, exist_ok=True)

            snap_path = self._snapshot_data_dir / timestamp / "tracemalloc.snap"
            if not snap_path.exists():
                raise FileNotFoundError(f"Tracemalloc 快照文件不存在: {snap_path}")
            snapshot = tracemalloc.Snapshot.load(str(snap_path))

            report_path = report_dir / "tracemalloc_report.txt"
            with open(report_path, 'w', encoding='utf-8') as f:
                f.write("=" * 80 + "\n")
                f.write(f" Tracemalloc 快照剖析报告: {timestamp}\n")
                f.write("=" * 80 + "\n\n")
                f.write("内存占用最高的代码位置 (Top 50):\n")
                f.write("-" * 80 + "\n")
                stats = snapshot.statistics('lineno')
                for i, stat in enumerate(stats[:50], 1):
                    frame = stat.traceback[0]
                    f.write(f"{i:3d}. {frame.filename}:{frame.lineno}\n")
                    f.write(f"     - 内存占用: {stat.size / 1024:.2f} KB\n")
                    f.write(f"     - 对象数量: {stat.count}\n")
            logger.info(f"Tracemalloc 剖析报告已生成: {report_path}")
            return report_path
        except Exception as e:
            logger.error(f"生成 Tracemalloc 剖析报告失败: {e}", exc_info=True)
            return None

    # --- 监控控制与核心逻辑 ---

    def start_monitoring(self):
        """
        初始化内存监控目录并启动监控线程
        """
        if not settings.MEMORY_ANALYSIS or self._monitoring:
            return
        logger.info("正在初始化内存监控目录...")
        try:
            self._snapshot_data_dir.mkdir(parents=True, exist_ok=True)
            self._anatomy_reports_dir.mkdir(parents=True, exist_ok=True)
            self._diff_reports_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.error(f"创建内存监控目录失败: {e}", exc_info=True)
            return
        self._monitoring = True
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()
        logger.info("内存监控已启动")
        if settings.MEMORY_TRACEMALLOC:
            self.start_tracemalloc_monitoring()

    def _monitor_loop(self):
        """
        每次循环创建一个时间戳目录
        """
        logger.info("内存监控循环开始")
        while self._monitoring:
            timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            snapshot_dir = self._snapshot_data_dir / timestamp_str
            try:
                snapshot_dir.mkdir(parents=True, exist_ok=True)
                self.create_memory_snapshot(snapshot_dir)
                if self._tracemalloc_monitoring:
                    self.take_tracemalloc_snapshot(snapshot_dir)
                # 自动生成单次 Pympler 报告
                self.generate_anatomy_report(timestamp_str)
                # 如果启用了 Tracemalloc 监控，自动生成 Tracemalloc 报告
                if self._tracemalloc_monitoring:
                    self.generate_tracemalloc_anatomy_report(timestamp_str)
                # 如果时间戳目录大于2，则自动生成增量归因报告
                existing_dirs = sorted([d for d in self._snapshot_data_dir.iterdir() if d.is_dir()])
                if len(existing_dirs) > 1:
                    ts1 = existing_dirs[-2].name
                    ts2 = existing_dirs[-1].name
                    self.generate_diff_report(ts1, ts2)
            except Exception as e:
                logger.error(f"内存监控循环出错: {e}", exc_info=True)
            finally:
                # 在循环末尾统一执行所有清理任务
                self._cleanup_snapshot_dirs()
                self._cleanup_diff_reports()
                time.sleep(self._check_interval)
        logger.info("内存监控循环结束")

    def create_memory_snapshot(self, snapshot_dir: Path):
        """
        接收目标目录路径
        """
        with self._snapshot_lock:
            try:
                snapshot_json_file = snapshot_dir / "pympler.json"
                logger.info(f"开始创建 Pympler 内存快照于: {snapshot_dir.name}")
                memory_usage = psutil.Process().memory_info().rss
                class_objects, large_variables = self._analyze_all_objects()
                snapshot_data = {
                    "metadata": {"time": datetime.now().isoformat(), "rss_mb": memory_usage / 1024 / 1024},
                    "classes": {item['name']: {"size_mb": item['size_mb'], "count": item['count']} for item in
                                class_objects},
                    "variables": {item['name']: {"type": item['type'], "size_mb": item['size_mb']} for item in
                                  large_variables}
                }
                with open(snapshot_json_file, 'w', encoding='utf-8') as f:
                    json.dump(snapshot_data, f, indent=2, ensure_ascii=False)
                logger.info(f"Pympler 快照已保存: {snapshot_json_file}")
            except Exception as e:
                logger.error(f"创建 Pympler 内存快照失败: {e}", exc_info=True)

    def take_tracemalloc_snapshot(self, snapshot_dir: Path):
        """
        接收目标目录路径
        """
        if not self._tracemalloc_monitoring:
            return
        try:
            snapshot = tracemalloc.take_snapshot()
            filename = snapshot_dir / "tracemalloc.snap"
            snapshot.dump(str(filename))
            logger.info(f"Tracemalloc 快照已持久化到: {filename}")
        except Exception as e:
            logger.error(f"持久化 Tracemalloc 快照失败: {e}", exc_info=True)

    # --- 内部对比与清理逻辑 ---

    def compare_snapshots(self, ts1: str, ts2: str) -> Dict[str, Any]:
        """
        通过时间戳构造路径
        """
        try:
            path1 = self._snapshot_data_dir / ts1 / "pympler.json"
            path2 = self._snapshot_data_dir / ts2 / "pympler.json"
            if not path1.exists() or not path2.exists():
                raise FileNotFoundError(f"Pympler 快照数据文件不存在于目录 {ts1} 或 {ts2}")
            with open(path1, 'r', encoding='utf-8') as f:
                data1 = json.load(f)
            with open(path2, 'r', encoding='utf-8') as f:
                data2 = json.load(f)
            return {
                "metadata": {
                    "before_time": data1.get("metadata", {}).get("time"),
                    "after_time": data2.get("metadata", {}).get("time"),
                    "memory_diff_mb": data2.get("metadata", {}).get("rss_mb", 0) - data1.get("metadata",
                                                                                             {}).get("rss_mb",
                                                                                                     0)},
                "class_diff": self._diff_data(data1.get("classes", {}), data2.get("classes", {})),
                "variable_diff": self._diff_data(data1.get("variables", {}), data2.get("variables", {}))
            }
        except Exception as e:
            logger.error(f"对比 Pympler 快照失败: {e}", exc_info=True)
            return {"error": str(e)}

    def compare_tracemalloc_snapshots(self, ts1: str, ts2: str, filters: Optional[List[tracemalloc.Filter]] = None) -> \
            List[Dict[str, Any]]:
        """
        通过时间戳构造路径
        """
        try:
            path1 = self._snapshot_data_dir / ts1 / "tracemalloc.snap"
            path2 = self._snapshot_data_dir / ts2 / "tracemalloc.snap"
            # 因为tracemalloc是可选启动的功能，所以没有则不直接raise异常，
            if not path1.exists() or not path2.exists():
                logger.error(f"Tracemalloc 快照文件不存在于目录 {ts1} 或 {ts2}")
                return []
            snapshot_before = tracemalloc.Snapshot.load(str(path1))
            snapshot_after = tracemalloc.Snapshot.load(str(path2))
            if filters:
                snapshot_before = snapshot_before.filter_traces(filters)
                snapshot_after = snapshot_after.filter_traces(filters)
            top_stats = snapshot_after.compare_to(snapshot_before, 'lineno')
            result = []
            for stat in top_stats[:20]:
                frame = stat.traceback[0]
                result.append({
                    "filename": frame.filename,
                    "lineno": frame.lineno,
                    "size_diff_kb": stat.size_diff / 1024,
                    "count_diff": stat.count_diff,
                    "traceback": str(stat.traceback)
                })
            return result
        except Exception as e:
            logger.error(f"加载或对比 Tracemalloc 快照失败: {e}", exc_info=True)
            return []

    def _cleanup_snapshot_dirs(self):
        """
        统一清理数据和对应的单快照报告
        """
        try:
            # 1. 清理核心数据目录
            snapshot_dirs = sorted([d for d in self._snapshot_data_dir.iterdir() if d.is_dir()])
            if len(snapshot_dirs) > self._keep_count:
                dirs_to_delete = snapshot_dirs[:-self._keep_count]
                for old_dir in dirs_to_delete:
                    # 获取要删除的时间戳
                    timestamp_to_delete = old_dir.name

                    # 删除数据目录
                    shutil.rmtree(old_dir)
                    logger.debug(f"已删除过期快照数据目录: {old_dir.name}")

                    # 同步删除对应的单快照报告目录
                    anatomy_report_dir = self._anatomy_reports_dir / timestamp_to_delete
                    if anatomy_report_dir.exists():
                        shutil.rmtree(anatomy_report_dir)
                        logger.debug(f"已同步删除过期单快照报告目录: {anatomy_report_dir.name}")

        except Exception as e:
            logger.error(f"清理过期快照目录失败: {e}", exc_info=True)

    def _cleanup_diff_reports(self):
        """
        清理过期的对比报告
        """
        try:
            # 按修改时间排序，保留最新的 N 份报告
            diff_files = sorted(self._diff_reports_dir.glob("diff_*.txt"), key=lambda p: p.stat().st_mtime)

            if len(diff_files) > self._keep_count:
                files_to_delete = diff_files[:-self._keep_count]
                for old_file in files_to_delete:
                    old_file.unlink()
                    logger.debug(f"已删除过期对比报告: {old_file.name}")
        except Exception as e:
            logger.error(f"清理过期对比报告失败: {e}", exc_info=True)

    def stop_monitoring(self):
        """
        停止内存监控并清理线程
        """
        self._monitoring = False
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=5)
        logger.info("内存监控已停止")
        self.stop_tracemalloc_monitoring()

    def start_tracemalloc_monitoring(self):
        """
        启动 Tracemalloc 监控
        :return:
        """
        if not settings.MEMORY_TRACEMALLOC or self._tracemalloc_monitoring:
            return 
        tracemalloc.start(self._tracemalloc_depth)
        self._tracemalloc_monitoring = True
        logger.info(f"Tracemalloc 监控已启动 (深度: {self._tracemalloc_depth})")

    def stop_tracemalloc_monitoring(self):
        """
        停止 Tracemalloc 监控
        :return:
        """
        if not self._tracemalloc_monitoring:
            return
        tracemalloc.stop()
        self._tracemalloc_monitoring = False
        logger.info("Tracemalloc 监控已停止")

    def _analyze_all_objects(self, large_var_limit=100) -> Tuple[List[Dict], List[Dict]]:
        """
        分析所有对象并返回类信息和大变量候选列表
        :param large_var_limit: 限制返回的大变量数量
        :return:
        """
        all_objects = muppy.get_objects()
        logger.debug(f"开始单次遍历分析 {len(all_objects)} 个对象")
        class_info = {}
        large_vars_candidates = []
        calculated_objects = set()
        error_count = 0
        for obj in all_objects:
            try:
                obj_id = id(obj)
                if obj_id in calculated_objects or isinstance(obj, type):
                    continue
                obj_class = type(obj)
                try:
                    if hasattr(obj_class, '__module__') and hasattr(obj_class, '__name__'):
                        class_name = f"{obj_class.__module__}.{obj_class.__name__}"
                    else:
                        class_name = str(obj_class)
                except Exception as e:
                    # 如果获取类名失败，使用简单的类型描述
                    class_name = f"<unknown_class_{id(obj_class)}>"
                    logger.debug(f"获取类名失败: {e}")

                # 计算对象本身的内存使用（不包括引用对象，避免重复计算）
                size_bytes = sys.getsizeof(obj)
                if size_bytes > 100:
                    size_mb = size_bytes / 1024 / 1024
                    if class_name in class_info:
                        class_info[class_name]['size_mb'] += size_mb
                        class_info[class_name]['count'] += 1
                    else:
                        class_info[class_name] = {'name': class_name, 'size_mb': size_mb, 'count': 1}
                if size_bytes > 10240:
                    deep_size_bytes = asizeof.asizeof(obj)
                    if deep_size_bytes > 10240:
                        size_mb = deep_size_bytes / 1024 / 1024
                        var_info = self._get_variable_info(obj, size_mb)
                        if var_info:
                            large_vars_candidates.append(var_info)
                        calculated_objects.add(obj_id)
            except Exception as e:
                error_count += 1
                if error_count <= 10:
                    logger.debug(f"分析对象时出错: {e}")
                continue
        logger.debug(f"对象分析完成, 遇到 {error_count} 个错误")
        sorted_classes = sorted(class_info.values(), key=lambda x: x['size_mb'], reverse=True)
        sorted_large_vars = sorted(large_vars_candidates, key=lambda x: x['size_mb'], reverse=True)[:large_var_limit]
        return sorted_classes, sorted_large_vars

    @staticmethod
    def _write_report_from_data(snapshot_file: Path, data: Dict[str, Any]) -> None:
        """
        从数据字典写入报告文件
        :param snapshot_file: Path, 快照文件路径
        :param data: Dict, 包含内存快照数据
        :return:
        """
        with open(snapshot_file, 'w', encoding='utf-8') as f:
            f.write(f"内存快照时间: {data['metadata']['time']}\n")
            f.write(f"当前进程内存使用: {data['metadata']['rss_mb']:.2f} MB\n")
            f.write("\n" + "=" * 80 + "\n")
            f.write("类实例内存使用情况 (按内存大小排序):\n")
            f.write("-" * 80 + "\n")
            sorted_classes = sorted(data['classes'].items(), key=lambda item: item[1]['size_mb'], reverse=True)
            for i, (name, info) in enumerate(sorted_classes[:100], 1):
                f.write(f"{i:3d}. {name:<50} {info['size_mb']:>8.2f} MB ({info['count']} 个实例)\n")
            f.write("\n" + "=" * 80 + "\n")
            f.write("大内存变量详情 (前100个):\n")
            f.write("-" * 80 + "\n")
            sorted_vars = sorted(data['variables'].items(), key=lambda item: item[1]['size_mb'], reverse=True)
            for i, (name, info) in enumerate(sorted_vars[:100], 1):
                f.write(f"{i:3d}. {name:<30} {info['type']:<15} {info['size_mb']:>8.2f} MB\n")

    @staticmethod
    def _write_diff_section(f, title: str, items: List[Dict]) -> None:
        """
        写入内存差异报告部分
        :param f: TextIOWrapper, 报告文件句柄
        :param title: str, 部分标题
        :param items: List[Dict], 差异项列表
        :return:
        """
        f.write(f"{title}:\n")
        if not items:
            f.write("  (无)\n\n")
            return
        for item in items[:20]:
            name = item.get('name', 'N/A')
            size_diff = item.get('size_diff')
            count_diff = item.get('count_diff')
            line = f"  - {name:<40}"
            if size_diff is not None:
                line += f" | Size: {size_diff:+.2f} MB"
            if count_diff is not None and count_diff != 0:
                line += f" | Count: {count_diff:+}"
            f.write(line + "\n")
        f.write("\n")

    @staticmethod
    def _diff_data(before: Dict, after: Dict) -> Dict[str, List]:
        """
        对比两个字典数据，返回内存变化的详细信息
        :param before: Dict, 对比前的字典数据
        :param after: Dict, 对比后的字典数据
        """
        diff = {
            "increased": [],
            "decreased": [],
            "new": [],
            "removed": []
        }
        all_keys = set(before.keys()) | set(after.keys())
        for key in all_keys:
            item_before = before.get(key)
            item_after = after.get(key)
            if item_before and item_after:
                size_diff = item_after['size_mb'] - item_before['size_mb']
                count_diff = item_after.get('count', 0) - item_before.get('count', 0)
                if size_diff > 0.01 or count_diff > 0:
                    diff["increased"].append({"name": key, "size_diff": size_diff, "count_diff": count_diff})
                elif size_diff < -0.01 or count_diff < 0:
                    diff["decreased"].append({"name": key, "size_diff": size_diff, "count_diff": count_diff})
            elif item_after:
                diff["new"].append({"name": key, **item_after})
            elif item_before:
                diff["removed"].append({"name": key, **item_before})
        diff["increased"].sort(key=lambda x: x['size_diff'], reverse=True)
        diff["new"].sort(key=lambda x: x['size_mb'], reverse=True)
        return diff

    def _get_variable_info(self, obj, size_mb) -> Optional[Dict[str, Any]]:
        """
        获取变量信息，包含名称、类型和大小
        :param obj: 对象实例
        :param size_mb: 对象的大小（MB）
        """
        try:
            obj_type = type(obj).__name__
            var_name = self._get_variable_name(obj)
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
    def _get_variable_name(obj) -> str:
        """
        获取对象的变量名称
        :param obj: 对象实例
        """
        try:
            referrers = gc.get_referrers(obj)
            for referrer in referrers:
                if isinstance(referrer, dict):
                    for name, value in referrer.items():
                        if value is obj and isinstance(name, str):
                            return name
                elif hasattr(referrer, '__dict__'):
                    for name, value in referrer.__dict__.items():
                        if value is obj and isinstance(name, str):
                            return f"{type(referrer).__name__}.{name}"
            return f"{type(obj).__name__}_{id(obj)}"
        except Exception as e:
            logger.debug(f"获取变量名失败: {e}")
            return f"{type(obj).__name__}_{id(obj)}"
