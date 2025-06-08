import gc
import linecache
import os
import sys
import threading
import time
import tracemalloc
from collections import defaultdict, deque
from datetime import datetime, timedelta
from functools import wraps
from typing import Optional, Callable, Any, Dict, List

import psutil

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.log import logger
from app.schemas import ConfigChangeEventData
from app.schemas.types import EventType
from app.utils.singleton import Singleton


class MemoryAnalyzer:
    """
    内存分析器，用于分析内存使用详情
    """

    _analyzing_depth = 15  # 默认分析深度

    def __init__(self):
        self.memory_history = deque(maxlen=100)  # 保留最近100次内存记录
        self.module_memory = defaultdict(list)  # 模块内存使用记录
        self._analyzing = False
        # 创建专门的内存日志记录器
        self._memory_logger = logger.get_logger("memory_analysis")

    def start_analyzing(self):
        """
        开始内存分析
        """
        if not self._analyzing:
            tracemalloc.start(self._analyzing_depth)
            self._analyzing = True
            self._memory_logger.info("内存分析器已启动")
            logger.info("内存分析器已启动")

    def stop_analyzing(self):
        """
        停止内存分析
        """
        if self._analyzing:
            tracemalloc.stop()
            self._analyzing = False
            self._memory_logger.info("内存分析器已停止")
            logger.info("内存分析器已停止")

    def record_memory_snapshot(self, tag: str = ""):
        """
        记录内存快照
        :param tag: 快照标签
        """
        if not self._analyzing:
            return None

        try:
            snapshot = tracemalloc.take_snapshot()
            top_stats = snapshot.statistics('lineno')

            # 记录当前时间和内存使用
            current_time = datetime.now()
            memory_info = MemoryHelper.get_memory_usage()

            # 记录基本信息到内存日志
            self._memory_logger.info(f"[{tag}] 内存快照 - RSS: {memory_info['rss']:.1f}MB, "
                                     f"系统使用率: {memory_info['system_percent']:.1f}%")

            # 分析最大内存使用的代码行
            top_memory_lines = []
            for index, stat in enumerate(top_stats[:10]):
                try:
                    # 安全地访问traceback属性
                    if hasattr(stat, 'traceback') and stat.traceback:
                        filename = getattr(stat.traceback, 'filename', 'unknown')
                        lineno = getattr(stat.traceback, 'lineno', 0)
                        size_mb = stat.size / 1024 / 1024

                        # 获取代码行内容
                        try:
                            line_content = linecache.getline(filename, lineno).strip()
                        except Exception as e:
                            line_content = f"无法读取代码行：{e}"

                        top_memory_lines.append({
                            'filename': os.path.basename(filename) if filename != 'unknown' else 'unknown',
                            'lineno': lineno,
                            'size_mb': size_mb,
                            'line_content': line_content
                        })

                        # 记录详细的内存使用信息到内存日志
                        if size_mb > 1.0:  # 只记录大于1MB的内存使用
                            self._memory_logger.info(f"[{tag}] 内存使用: {os.path.basename(filename)}:{lineno} "
                                                     f"使用 {size_mb:.2f}MB - {line_content[:50]}")

                except Exception as e:
                    self._memory_logger.error(f"处理内存统计项时出错: {e}")
                    continue

            # 记录到历史
            snapshot_record = {
                'timestamp': current_time,
                'tag': tag,
                'memory_info': memory_info,
                'top_memory_lines': top_memory_lines
            }

            self.memory_history.append(snapshot_record)
            return snapshot_record

        except Exception as e:
            self._memory_logger.error(f"记录内存快照失败: {e}")
            logger.error(f"记录内存快照失败: {e}")
            return None

    def get_memory_trend(self, minutes: int = 30) -> List[Dict]:
        """
        获取内存使用趋势
        :param minutes: 获取最近多少分钟的数据
        :return: 内存趋势数据
        """
        cutoff_time = datetime.now() - timedelta(minutes=minutes)
        trend_data = [
            record for record in self.memory_history
            if record['timestamp'] >= cutoff_time
        ]

        if trend_data:
            self._memory_logger.info(f"获取内存趋势数据: 最近{minutes}分钟内有{len(trend_data)}条记录")

        return trend_data

    def get_top_memory_files(self, limit: int = 10) -> List[Dict]:
        """
        获取内存使用最多的文件
        :param limit: 返回数量限制
        :return: 文件内存使用统计
        """
        if not self._analyzing:
            return []

        try:
            snapshot = tracemalloc.take_snapshot()
            top_stats = snapshot.statistics('filename')

            result = []
            for stat in top_stats[:limit]:
                try:
                    if hasattr(stat, 'traceback') and stat.traceback:
                        filename = getattr(stat.traceback, 'filename', 'unknown')
                        size_mb = stat.size / 1024 / 1024

                        file_info = {
                            'filename': os.path.basename(filename) if filename != 'unknown' else 'unknown',
                            'full_path': filename,
                            'size_mb': size_mb,
                            'count': stat.count
                        }
                        result.append(file_info)

                        # 记录到内存日志
                        if size_mb > 0.5:  # 只记录大于0.5MB的文件
                            self._memory_logger.info(f"文件内存使用: {file_info['filename']} "
                                                     f"使用 {size_mb:.2f}MB ({stat.count} 次分配)")

                except Exception as e:
                    self._memory_logger.error(f"处理文件统计项时出错: {e}")
                    continue

            if result:
                self._memory_logger.info(f"获取内存使用最多的{len(result)}个文件")

            return result

        except Exception as e:
            self._memory_logger.error(f"获取文件内存统计失败: {e}")
            logger.error(f"获取文件内存统计失败: {e}")
            return []

    def analyze_memory_leaks(self) -> Dict:
        """
        分析可能的内存泄漏
        :return: 内存泄漏分析结果
        """
        if len(self.memory_history) < 5:
            return {'status': 'insufficient_data', 'message': '数据不足，无法分析'}

        try:
            # 分析内存增长趋势
            recent_records = list(self.memory_history)[-10:]
            memory_values = [record['memory_info']['rss'] for record in recent_records]

            # 计算内存增长率
            if len(memory_values) > 1:
                growth_rate = (memory_values[-1] - memory_values[0]) / len(memory_values)

                # 记录分析结果到内存日志
                self._memory_logger.info(f"内存泄漏分析: 平均增长率 {growth_rate:.2f}MB/次")

                # 每次检查增长超过10MB
                if growth_rate > 10:
                    result = {
                        'status': 'potential_leak',
                        'growth_rate_mb': growth_rate,
                        'message': f'检测到潜在内存泄漏，平均每次检查增长 {growth_rate:.2f}MB'
                    }
                    self._memory_logger.warning(f"⚠️ 潜在内存泄漏: {result['message']}")
                    return result
                elif growth_rate > 5:
                    result = {
                        'status': 'high_growth',
                        'growth_rate_mb': growth_rate,
                        'message': f'内存增长较快，平均每次检查增长 {growth_rate:.2f}MB'
                    }
                    self._memory_logger.warning(f"⚠️ 内存增长较快: {result['message']}")
                    return result

            self._memory_logger.info("内存使用正常，未检测到异常增长")
            return {'status': 'normal', 'message': '内存使用正常'}

        except Exception as e:
            self._memory_logger.error(f"分析内存泄漏失败: {e}")
            logger.error(f"分析内存泄漏失败: {e}")
            return {'status': 'error', 'message': f'分析失败: {str(e)}'}


class MemoryHelper(metaclass=Singleton):
    """
    内存管理工具类，用于监控和优化内存使用
    """

    def __init__(self):
        # 内存使用阈值(MB)
        self._memory_threshold = 512
        # 检查间隔(秒)
        self._check_interval = 300
        self._monitoring = False
        self._monitor_thread: Optional[threading.Thread] = None
        self._analyzer = MemoryAnalyzer()
        # 是否启用详细日志
        self._detailed_logging = False
        # 创建专门的内存日志记录器
        self._memory_logger = logger.get_logger("memory_monitor")

    @property
    def analyzer(self):
        return self._analyzer

    @staticmethod
    def get_memory_usage() -> dict:
        """
        获取当前内存使用情况
        """
        try:
            process = psutil.Process()
            memory_info = process.memory_info()
            system_memory = psutil.virtual_memory()

            return {
                'rss': memory_info.rss / 1024 / 1024,  # MB
                'vms': memory_info.vms / 1024 / 1024,  # MB
                'percent': process.memory_percent(),
                'system_percent': system_memory.percent,
                'system_available': system_memory.available / 1024 / 1024 / 1024,  # GB
                'system_total': system_memory.total / 1024 / 1024 / 1024,  # GB
                'system_used': system_memory.used / 1024 / 1024 / 1024  # GB
            }
        except Exception as e:
            logger.error(f"获取内存使用情况失败: {e}")
            return {
                'rss': 0, 'vms': 0, 'percent': 0,
                'system_percent': 0, 'system_available': 0,
                'system_total': 0, 'system_used': 0
            }

    def get_detailed_memory_info(self) -> Dict:
        """
        获取详细的内存信息
        """
        try:
            process = psutil.Process()

            # 获取更详细的进程内存信息
            try:
                memory_full_info = process.memory_full_info()
                detailed_info = {
                    'uss': memory_full_info.uss / 1024 / 1024,  # 进程独占内存 MB
                    'pss': memory_full_info.pss / 1024 / 1024,  # 进程按比例共享内存 MB  
                    'swap': memory_full_info.swap / 1024 / 1024,  # 交换内存 MB
                }
            except (psutil.AccessDenied, AttributeError) as e:
                self._memory_logger.error(f"获取详细内存信息失败: {e}")
                detailed_info = {}

            # 获取垃圾回收信息
            gc_info = {}
            try:
                for generation in range(3):
                    gc_info[f'gen_{generation}'] = gc.get_count()[generation]
            except Exception as e:
                self._memory_logger.error(f"获取垃圾回收信息失败: {e}")

            # 获取对象统计
            object_counts = {}
            try:
                # 统计主要对象类型的数量
                for obj_type in [list, dict, tuple, set, str, int, float]:
                    try:
                        object_counts[obj_type.__name__] = len([obj for obj in gc.get_objects()
                                                                if type(obj) is obj_type])
                    except Exception as e:
                        self._memory_logger.error(f"统计对象类型 {obj_type.__name__} 失败: {e}")
                        continue
            except Exception as e:
                self._memory_logger.error(f"获取对象统计失败: {e}")

            detailed_result = {
                'basic': self.get_memory_usage(),
                'detailed': detailed_info,
                'gc_info': gc_info,
                'object_counts': object_counts,
                'thread_count': threading.active_count(),
                'fd_count': len(process.open_files()) if hasattr(process, 'open_files') else 0
            }

            # 记录详细信息到内存日志
            basic = detailed_result['basic']
            self._memory_logger.info(f"详细内存信息获取 - RSS: {basic['rss']:.1f}MB, "
                                     f"线程数: {detailed_result['thread_count']}, "
                                     f"文件描述符: {detailed_result['fd_count']}")

            return detailed_result

        except Exception as e:
            self._memory_logger.error(f"获取详细内存信息失败: {e}")
            logger.error(f"获取详细内存信息失败: {e}")
            return {
                'basic': self.get_memory_usage(),
                'detailed': {},
                'gc_info': {},
                'object_counts': {},
                'thread_count': 0,
                'fd_count': 0
            }

    def get_module_memory_usage(self) -> Dict[str, float]:
        """
        获取各模块的内存使用情况（估算）
        """
        module_memory = {}

        try:
            # 统计已导入模块的大小
            for module_name, module in sys.modules.items():
                if module and hasattr(module, '__file__') and module.__file__:
                    try:
                        # 估算模块内存使用（通过模块中的对象数量）
                        objects = []
                        if hasattr(module, '__dict__'):
                            objects = list(module.__dict__.values())

                        # 粗略估算：每个对象平均占用内存
                        estimated_size = len(objects) * 0.001  # MB
                        module_memory[module_name] = estimated_size
                    except Exception as e:
                        self._memory_logger.error(f"获取模块 {module_name} 内存使用失败: {e}")
                        continue

            # 按内存使用量排序，返回前20个
            sorted_modules = sorted(module_memory.items(), key=lambda x: x[1], reverse=True)
            top_modules = dict(sorted_modules[:20])

            # 记录到内存日志
            self._memory_logger.info(f"模块内存统计完成，共分析 {len(module_memory)} 个模块，"
                                     f"前5个模块: {list(top_modules.keys())[:5]}")

            return top_modules

        except Exception as e:
            self._memory_logger.error(f"获取模块内存使用失败: {e}")
            logger.error(f"获取模块内存使用失败: {e}")
            return {}

    def force_gc(self, generation: Optional[int] = None) -> int:
        """
        强制执行垃圾回收
        :param generation: 垃圾回收代数，None表示所有代数
        :return: 回收的对象数量
        """
        try:
            before_memory = self.get_memory_usage()
            self._memory_logger.info(f"开始强制垃圾回收，当前内存使用: {before_memory['rss']:.2f}MB")

            if generation is not None:
                collected = gc.collect(generation)
                self._memory_logger.info(f"执行第{generation}代垃圾回收")
            else:
                collected = gc.collect()
                self._memory_logger.info("执行全代垃圾回收")

            after_memory = self.get_memory_usage()
            memory_freed = before_memory['rss'] - after_memory['rss']

            if memory_freed > 0:
                self._memory_logger.info(f"垃圾回收完成: 回收对象 {collected} 个, 释放内存 {memory_freed:.2f}MB")
            else:
                self._memory_logger.info(f"垃圾回收完成: 回收对象 {collected} 个, 内存无明显释放")

            # 记录内存快照
            if self._detailed_logging:
                self._analyzer.record_memory_snapshot("after_gc")

            return collected

        except Exception as e:
            self._memory_logger.error(f"执行垃圾回收失败: {e}")
            logger.error(f"执行垃圾回收失败: {e}")
            return 0

    def check_memory_and_cleanup(self) -> bool:
        """
        检查内存使用量，如果超过阈值则执行清理
        :return: 是否执行了清理
        """
        try:
            memory_info = self.get_memory_usage()
            current_memory_mb = memory_info['rss']

            # 记录常规检查到内存日志
            self._memory_logger.info(f"常规内存检查 - RSS: {current_memory_mb:.1f}MB, "
                                     f"阈值: {self._memory_threshold}MB, "
                                     f"系统使用率: {memory_info['system_percent']:.1f}%")

            # 记录内存快照
            if self._detailed_logging:
                self._analyzer.record_memory_snapshot("routine_check")

            if current_memory_mb > self._memory_threshold:
                self._memory_logger.warning(
                    f"内存使用超过阈值: {current_memory_mb:.1f}MB > {self._memory_threshold:.1f}MB, 开始清理...")

                # 详细记录高内存使用情况
                if self._detailed_logging:
                    self.get_detailed_memory_info()
                    self._memory_logger.info(f"高内存使用详细信息记录完成")

                    # 记录内存使用最多的文件
                    top_files = self._analyzer.get_top_memory_files(10)
                    if top_files:
                        self._memory_logger.info("内存使用最多的文件:")
                        for file_info in top_files:
                            self._memory_logger.info(f"  {file_info['filename']}: {file_info['size_mb']:.2f}MB")

                self.force_gc()

                # 再次检查清理效果
                after_memory = self.get_memory_usage()
                self._memory_logger.info(f"清理后内存: {after_memory['rss']:.1f}MB")

                # 检查是否可能存在内存泄漏
                leak_analysis = self._analyzer.analyze_memory_leaks()
                if leak_analysis['status'] != 'normal':
                    self._memory_logger.warning(f"内存泄漏分析: {leak_analysis['message']}")

                return True
            return False

        except Exception as e:
            self._memory_logger.error(f"内存检查和清理失败: {e}")
            logger.error(f"内存检查和清理失败: {e}")
            return False

    def generate_memory_report(self) -> Dict:
        """
        生成详细的内存使用报告
        """
        try:
            self._memory_logger.info("开始生成内存使用报告")

            report = {
                'timestamp': datetime.now().isoformat(),
                'basic_info': self.get_memory_usage(),
                'detailed_info': self.get_detailed_memory_info(),
                'module_memory': self.get_module_memory_usage(),
                'memory_trend': self._analyzer.get_memory_trend(30),
                'top_memory_files': self._analyzer.get_top_memory_files(10),
                'leak_analysis': self._analyzer.analyze_memory_leaks(),
                'gc_stats': {
                    'thresholds': gc.get_threshold(),
                    'counts': gc.get_count(),
                    'stats': gc.get_stats() if hasattr(gc, 'get_stats') else None
                }
            }

            # 记录报告摘要到内存日志
            basic = report['basic_info']
            trend_count = len(report['memory_trend'])
            files_count = len(report['top_memory_files'])

            self._memory_logger.info(f"内存报告生成完成 - RSS: {basic['rss']:.1f}MB, "
                                     f"趋势记录: {trend_count}条, 文件统计: {files_count}个, "
                                     f"泄漏状态: {report['leak_analysis']['status']}")

            return report

        except Exception as e:
            self._memory_logger.error(f"生成内存报告失败: {e}")
            logger.error(f"生成内存报告失败: {e}")
            return {
                'timestamp': datetime.now().isoformat(),
                'error': str(e),
                'basic_info': self.get_memory_usage()
            }

    def enable_detailed_logging(self, enable: bool = True):
        """
        启用/禁用详细日志记录
        :param enable: 是否启用
        """
        try:
            self._detailed_logging = enable
            if enable:
                self._analyzer.start_analyzing()
                self._memory_logger.info("已启用详细内存日志记录和分析")
                logger.info("已启用详细内存日志记录")
            else:
                self._analyzer.stop_analyzing()
                self._memory_logger.info("已禁用详细内存日志记录和分析")
                logger.info("已禁用详细内存日志记录")
        except Exception as e:
            self._memory_logger.error(f"切换详细日志记录状态失败: {e}")
            logger.error(f"切换详细日志记录状态失败: {e}")

    @eventmanager.register(EventType.ConfigChanged)
    def handle_config_changed(self, event: Event):
        """
        处理配置变更事件，更新内存监控设置
        :param event: 事件对象
        """
        if not event:
            return
        event_data: ConfigChangeEventData = event.event_data
        if event_data.key not in ['MEMORY_MONITOR_ENABLE']:
            return
        self.stop_monitoring()
        self.start_monitoring()

    def start_monitoring(self):
        """
        开始内存监控
        """
        if not settings.MEMORY_MONITOR_ENABLE:
            return
        if self._monitoring:
            return

        # 初始化内存分析器
        self._monitoring = True
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()

        # 启用详细分析（如果配置允许）
        if settings.MEMORY_DETAILED_ANALYSIS:
            self.enable_detailed_logging(True)

        self._memory_logger.info(
            f"内存监控已启动 - 阈值: {self._memory_threshold}MB, 检查间隔: {self._check_interval}秒")

    def stop_monitoring(self):
        """
        停止内存监控
        """
        self._monitoring = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=5)

        # 停止详细分析
        self.enable_detailed_logging(False)

        self._memory_logger.info("内存监控已停止")

    def _monitor_loop(self):
        """
        内存监控循环
        """
        self._memory_logger.info("内存监控循环开始")
        while self._monitoring:
            try:
                # 执行常规检查
                self.check_memory_and_cleanup()

                # 每10次检查生成一次详细报告
                if self._detailed_logging and hasattr(self, '_check_count'):
                    self._check_count = getattr(self, '_check_count', 0) + 1
                    if self._check_count % 10 == 0:
                        report = self.generate_memory_report()
                        self._memory_logger.info(f"第{self._check_count}次检查 - 内存使用报告: "
                                                 f"RSS={report['basic_info']['rss']:.1f}MB, "
                                                 f"系统使用率={report['basic_info']['system_percent']:.1f}%")

                time.sleep(self._check_interval)
            except Exception as e:
                self._memory_logger.error(f"内存监控出错: {e}")
                logger.error(f"内存监控出错: {e}")
                # 出错后等待1分钟再继续
                time.sleep(60)

        self._memory_logger.info("内存监控循环结束")

    def set_threshold(self, threshold_mb: int):
        """
        设置内存使用阈值
        :param threshold_mb: 内存阈值，单位MB（500-4096之间）
        """
        old_threshold = self._memory_threshold
        self._memory_threshold = max(512, min(4096, threshold_mb))
        self._memory_logger.info(f"内存阈值已从 {old_threshold}MB 更新为: {self._memory_threshold}MB")

    def set_check_interval(self, interval: int):
        """
        设置检查间隔
        :param interval: 检查间隔，单位秒（最少60秒）
        """
        old_interval = self._check_interval
        self._check_interval = max(60, interval)
        self._memory_logger.info(f"内存检查间隔已从 {old_interval}秒 更新为: {self._check_interval}秒")

    def get_threshold(self) -> int:
        """
        获取当前内存阈值
        :return: 当前阈值(MB)
        """
        return self._memory_threshold


def memory_optimized(force_gc_after: bool = False, log_memory: bool = False):
    """
    内存优化装饰器
    :param force_gc_after: 函数执行后是否强制垃圾回收
    :param log_memory: 是否记录内存使用情况
    """

    memory_logger = logger.get_logger("memory_monitor")

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            memory_helper = MemoryHelper()

            if settings.MEMORY_MONITOR_ENABLE:
                if log_memory:
                    before_memory = memory_helper.get_memory_usage()
                    memory_logger.info(f"{func.__name__} 执行前内存: {before_memory['rss']:.1f}MB")
                memory_helper.analyzer.record_memory_snapshot(f"before_{func.__name__}")

            try:
                result = func(*args, **kwargs)
                return result
            finally:
                if settings.MEMORY_MONITOR_ENABLE:
                    if force_gc_after:
                        memory_helper.force_gc()
                    if log_memory:
                        after_memory = memory_helper.get_memory_usage()
                        memory_logger.info(f"{func.__name__} 执行后内存: {after_memory['rss']:.1f}MB")
                    memory_helper.analyzer.record_memory_snapshot(f"after_{func.__name__}")

        return wrapper

    return decorator
