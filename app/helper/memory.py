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

    _analyzing_depth = 25  # 默认分析深度，增加深度以获取更准确的信息

    def __init__(self):
        self.memory_history = deque(maxlen=100)  # 保留最近100次内存记录
        self.module_memory = defaultdict(list)  # 模块内存使用记录
        self._analyzing = False
        # 创建专门的内存日志记录器
        self._memory_logger = logger.get_logger("memory_analysis")

    @property
    def is_analyzing(self):
        """
        是否正在进行内存分析
        """
        return self._analyzing

    def _debug_traceback_structure(self, stat, index: int):
        """
        调试traceback结构的辅助函数
        """
        try:
            self._memory_logger.debug(f"统计项 {index}: size={stat.size}, count={stat.count}")
            if hasattr(stat, 'traceback') and stat.traceback:
                self._memory_logger.debug(f"traceback类型: {type(stat.traceback)}, 长度: {len(stat.traceback)}")
                for i, frame in enumerate(stat.traceback):
                    self._memory_logger.debug(f"Frame {i}: {frame.filename}:{frame.lineno}")
                    if i >= 2:  # 只显示前3个frame
                        break
            else:
                self._memory_logger.debug("没有traceback信息")
        except Exception as e:
            self._memory_logger.error(f"调试traceback结构失败: {e}")

    def start_analyzing(self):
        """
        开始内存分析
        """
        if not self._analyzing:
            tracemalloc.start(self._analyzing_depth)
            self._analyzing = True
            self._memory_logger.info(f"内存分析器已启动，分析深度: {self._analyzing_depth}")
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
                    # 在调试模式下输出traceback结构信息
                    if settings.DEBUG and index == 0:
                        self._debug_traceback_structure(stat, index)
                    
                    # 正确访问traceback属性
                    filename = 'unknown'
                    lineno = 0
                    
                    if hasattr(stat, 'traceback') and stat.traceback:
                        try:
                            # 获取traceback的第一个frame
                            if len(stat.traceback) > 0:
                                frame = stat.traceback[0]
                                filename = frame.filename
                                lineno = frame.lineno
                        except (IndexError, AttributeError) as e:
                            self._memory_logger.debug(f"访问traceback frame失败: {e}")
                    
                    size_mb = stat.size / 1024 / 1024

                    # 获取代码行内容
                    if filename != 'unknown' and lineno > 0:
                        try:
                            line_content = linecache.getline(filename, lineno).strip()
                            if not line_content:
                                line_content = "无法读取代码行内容" # noqa
                        except Exception as e:
                            line_content = f"读取代码行失败：{str(e)}"
                    else:
                        line_content = "文件名或行号无效"

                    top_memory_lines.append({
                        'filename': os.path.basename(filename) if filename != 'unknown' else 'unknown',
                        'lineno': lineno,
                        'size_mb': size_mb,
                        'line_content': line_content
                    })

                    # 记录详细的内存使用信息到内存日志
                    if size_mb > 1.0:  # 只记录大于1MB的内存使用
                        base_filename = os.path.basename(filename) if filename != 'unknown' else 'unknown'
                        # 确保日志内容完整显示
                        log_content = line_content[:100] if line_content else "无内容"
                        self._memory_logger.info(f"[{tag}] 内存使用: {base_filename}:{lineno} "
                                                 f"使用 {size_mb:.2f}MB - {log_content}")

                except Exception as e:
                    self._memory_logger.error(f"处理内存统计项 {index} 时出错: {e}")
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
                    # 正确访问traceback属性获取文件名
                    if hasattr(stat, 'traceback') and stat.traceback:
                        # 获取traceback的第一个frame
                        frame = stat.traceback[0] if len(stat.traceback) > 0 else None
                        if frame:
                            filename = frame.filename
                        else:
                            filename = 'unknown'
                    else:
                        filename = 'unknown'
                    
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
            gc_info: Dict[str, int] = {}
            try:
                gc_counts = gc.get_count()
                for generation in range(3):
                    gc_info[f'gen_{generation}'] = gc_counts[generation]
            except Exception as e:
                self._memory_logger.error(f"获取垃圾回收信息失败: {e}")

            # 获取对象统计
            object_counts: Dict[str, int] = {}
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
                    detailed_info = self.get_detailed_memory_info()
                    self._memory_logger.info(f"高内存使用详细信息记录完成 - 线程数: {detailed_info.get('thread_count', 0)}, "
                                           f"文件描述符: {detailed_info.get('fd_count', 0)}")

                    # 记录内存使用最多的文件
                    top_files = self._analyzer.get_top_memory_files(10)
                    if top_files:
                        self._memory_logger.info("内存使用最多的文件:")
                        for file_info in top_files:
                            self._memory_logger.info(f"  {file_info['filename']}: {file_info['size_mb']:.2f}MB")

                    # 分析未跟踪的内存
                    memory_diff = self.get_tracemalloc_vs_psutil_diff()
                    if memory_diff['untracked_percentage'] > 50:  # 如果超过50%的内存未被跟踪
                        self._memory_logger.warning(f"⚠️ 大量未跟踪内存: {memory_diff['untracked_memory_mb']:.1f}MB "
                                                    f"({memory_diff['untracked_percentage']:.1f}%)，可能是C扩展或外部库内存泄漏")
                        
                        # 分析大对象
                        large_objects = self.analyze_large_objects()
                        if large_objects:
                            self._memory_logger.info("检测到的大对象类型:")
                            for obj in large_objects[:5]:  # 只显示前5个
                                if obj['total_size_mb'] > 5:  # 只显示超过5MB的
                                    self._memory_logger.info(f"  {obj['type']}: {obj['count']}个对象, "
                                                            f"总计{obj['total_size_mb']:.1f}MB")

                self.force_gc()

                # 再次检查清理效果
                after_memory = self.get_memory_usage()
                memory_freed = current_memory_mb - after_memory['rss']
                self._memory_logger.info(f"清理后内存: {after_memory['rss']:.1f}MB，释放: {memory_freed:.1f}MB")

                # 检查是否可能存在内存泄漏
                leak_analysis = self._analyzer.analyze_memory_leaks()
                if leak_analysis['status'] != 'normal':
                    self._memory_logger.warning(f"内存泄漏分析: {leak_analysis['message']}")

                # 如果清理效果不佳且内存仍然很高，生成完整报告
                if memory_freed < 50 and after_memory['rss'] > self._memory_threshold:
                    self._memory_logger.warning(f"⚠️ 垃圾回收效果不佳，生成详细内存报告")
                    try:
                        # 生成并打印详细内存报告
                        self.print_detailed_memory_report()
                    except Exception as e:
                        self._memory_logger.error(f"生成详细内存报告失败: {e}")

                return True
            return False

        except Exception as e:
            self._memory_logger.error(f"内存检查和清理失败: {e}")
            logger.error(f"内存检查和清理失败: {e}")
            return False

    def get_tracemalloc_vs_psutil_diff(self) -> Dict:
        """
        比较 tracemalloc 和 psutil 的内存统计差异
        """
        try:
            # 获取 psutil 的内存使用
            psutil_memory = self.get_memory_usage()
            
            # 获取 tracemalloc 的总内存统计
            tracemalloc_total = 0
            if self._analyzer.is_analyzing:
                snapshot = tracemalloc.take_snapshot()
                top_stats = snapshot.statistics('lineno')
                tracemalloc_total = sum(stat.size for stat in top_stats) / 1024 / 1024  # MB
            
            diff_mb = psutil_memory['rss'] - tracemalloc_total
            diff_percent = (diff_mb / psutil_memory['rss']) * 100 if psutil_memory['rss'] > 0 else 0
            
            result = {
                'psutil_rss_mb': psutil_memory['rss'],
                'tracemalloc_total_mb': tracemalloc_total,
                'untracked_memory_mb': diff_mb,
                'untracked_percentage': diff_percent
            }
            
            self._memory_logger.info(f"内存差异分析: PSUtil={psutil_memory['rss']:.1f}MB, "
                                     f"Tracemalloc={tracemalloc_total:.1f}MB, "
                                     f"未跟踪={diff_mb:.1f}MB ({diff_percent:.1f}%)")
            
            return result
            
        except Exception as e:
            self._memory_logger.error(f"内存差异分析失败: {e}")
            return {
                'psutil_rss_mb': 0,
                'tracemalloc_total_mb': 0,
                'untracked_memory_mb': 0,
                'untracked_percentage': 0,
                'error': str(e)
            }

    def analyze_large_objects(self) -> List[Dict]:
        """
        分析大对象，查找可能的内存泄漏源
        """
        try:
            self._memory_logger.info("开始分析大对象")
            large_objects = []
            
            # 获取所有对象
            all_objects = gc.get_objects()
            
            # 按类型分组统计
            type_stats: Dict[str, Dict[str, Any]] = defaultdict(lambda: {'count': 0, 'total_size': 0, 'objects': []})
            
            for obj in all_objects:
                try:
                    obj_type = type(obj).__name__
                    obj_size = sys.getsizeof(obj)
                    
                    type_stats[obj_type]['count'] += 1
                    type_stats[obj_type]['total_size'] += obj_size
                    
                    # 记录大对象（>1MB）
                    if obj_size > 1024 * 1024:
                        type_stats[obj_type]['objects'].append({
                            'size_mb': obj_size / 1024 / 1024,
                            'id': id(obj),
                            'repr': str(obj)[:100] if hasattr(obj, '__str__') else 'N/A'
                        })
                        
                except Exception as e:
                    self._memory_logger.error(f"处理对象 {obj} 时出错: {e}")
                    continue
            
            # 按总大小排序，取前20个类型
            sorted_types = sorted(type_stats.items(), 
                                key=lambda x: x[1]['total_size'], 
                                reverse=True)[:20]
            
            for obj_type, stats in sorted_types:
                size_mb = stats['total_size'] / 1024 / 1024
                large_objects.append({
                    'type': obj_type,
                    'count': stats['count'],
                    'total_size_mb': size_mb,
                    'avg_size_kb': (stats['total_size'] / stats['count']) / 1024,
                    'large_instances': stats['objects'][:5]  # 只保留前5个大实例
                })
                
                # 记录到日志
                if size_mb > 10:  # 只记录总大小超过10MB的类型
                    self._memory_logger.info(f"大对象类型: {obj_type} - 数量: {stats['count']}, "
                                             f"总大小: {size_mb:.1f}MB, "
                                             f"平均大小: {(stats['total_size'] / stats['count']) / 1024:.1f}KB")
            
            self._memory_logger.info(f"大对象分析完成，共分析 {len(all_objects)} 个对象，"
                                     f"发现 {len(large_objects)} 种主要类型")
            
            return large_objects
            
        except Exception as e:
            self._memory_logger.error(f"分析大对象失败: {e}")
            return []

    def analyze_reference_cycles(self) -> Dict:
        """
        分析引用循环，查找可能导致内存泄漏的循环引用
        """
        try:
            self._memory_logger.info("开始分析引用循环")
            
            # 强制垃圾回收前的统计
            before_counts = gc.get_count()
            before_objects = len(gc.get_objects())
            
            # 检查引用循环
            cycles_found = gc.collect()
            
            # 强制垃圾回收后的统计
            after_counts = gc.get_count()
            after_objects = len(gc.get_objects())
            
            # 获取垃圾对象（如果有的话）
            garbage_count = len(gc.garbage)
            
            result = {
                'cycles_collected': cycles_found,
                'objects_before': before_objects,
                'objects_after': after_objects,
                'objects_freed': before_objects - after_objects,
                'garbage_objects': garbage_count,
                'gc_counts_before': before_counts,
                'gc_counts_after': after_counts
            }
            
            self._memory_logger.info(f"引用循环分析: 回收循环 {cycles_found} 个, "
                                     f"释放对象 {result['objects_freed']} 个, "
                                     f"垃圾对象 {garbage_count} 个")
            
            # 如果有垃圾对象，记录详细信息
            if garbage_count > 0:
                garbage_types: Dict[str, int] = defaultdict(int)
                for obj in gc.garbage[:10]:  # 只检查前10个
                    garbage_types[type(obj).__name__] += 1
                
                result['garbage_types'] = dict(garbage_types) # noqa
                self._memory_logger.warning(f"发现垃圾对象类型: {dict(garbage_types)}")
            
            return result
            
        except Exception as e:
            self._memory_logger.error(f"分析引用循环失败: {e}")
            return {'error': str(e)}

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
                'memory_diff': self.get_tracemalloc_vs_psutil_diff(),
                'large_objects': self.analyze_large_objects(),
                'reference_cycles': self.analyze_reference_cycles(),
                'memory_hotspots': self.analyze_memory_hotspots(),
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
            untracked_mb = report['memory_diff']['untracked_memory_mb']
            large_objects_count = len(report['large_objects'])

            self._memory_logger.info(f"内存报告生成完成 - RSS: {basic['rss']:.1f}MB, "
                                     f"未跟踪: {untracked_mb:.1f}MB, "
                                     f"趋势记录: {trend_count}条, 文件统计: {files_count}个, "
                                     f"大对象类型: {large_objects_count}个, "
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

    def analyze_memory_hotspots(self) -> Dict:
        """
        分析内存热点，识别可能的内存泄漏源
        """
        try:
            self._memory_logger.info("开始分析内存热点")
            
            hotspots = {
                'high_allocation_functions': [],
                'large_objects_by_module': {},
                'suspicious_patterns': [],
                'recommendations': []
            }
            
            # 1. 分析高分配频率的函数
            if self._analyzer.is_analyzing:
                snapshot = tracemalloc.take_snapshot()
                top_stats = snapshot.statistics('lineno')
                
                for stat in top_stats[:20]:
                    try:
                        if hasattr(stat, 'traceback') and stat.traceback and len(stat.traceback) > 0:
                            frame = stat.traceback[0]
                            if frame.filename and frame.lineno:
                                size_mb = stat.size / 1024 / 1024
                                if size_mb > 5:  # 只分析大于5MB的
                                    hotspots['high_allocation_functions'].append({
                                        'filename': os.path.basename(frame.filename),
                                        'lineno': frame.lineno,
                                        'size_mb': size_mb,
                                        'allocations': stat.count
                                    })
                    except Exception as e:
                        self._memory_logger.error(f"处理高分配函数统计项时出错: {e}")
                        continue
            
            # 2. 按模块分析大对象
            large_objects = self.analyze_large_objects()
            for obj in large_objects:
                if obj['total_size_mb'] > 10:
                    module_name = 'unknown'
                    if 'module' in obj['type'].lower() or obj['type'] in ['dict', 'list']:
                        module_name = f"{obj['type']}_objects"
                    hotspots['large_objects_by_module'][module_name] = obj
            
            # 3. 检测可疑模式
            suspicious_patterns = []
            
            # 检查JSON相关的内存使用
            for obj in large_objects:
                if 'decoder' in obj['type'].lower() or 'encoder' in obj['type'].lower():
                    suspicious_patterns.append(f"JSON处理占用大量内存: {obj['type']} ({obj['total_size_mb']:.1f}MB)")
            
            # 检查HTTP/网络相关的内存使用
            for obj in large_objects:
                if any(keyword in obj['type'].lower() for keyword in ['http', 'response', 'request', 'models']):
                    suspicious_patterns.append(f"HTTP/网络对象占用大量内存: {obj['type']} ({obj['total_size_mb']:.1f}MB)")
            
            # 检查缓存相关的内存使用
            for obj in large_objects:
                if any(keyword in obj['type'].lower() for keyword in ['cache', 'pickle', 'init']):
                    suspicious_patterns.append(f"缓存/序列化对象占用大量内存: {obj['type']} ({obj['total_size_mb']:.1f}MB)")
            
            hotspots['suspicious_patterns'] = suspicious_patterns
            
            # 4. 生成建议
            recommendations = []
            memory_diff = self.get_tracemalloc_vs_psutil_diff()
            
            if memory_diff['untracked_percentage'] > 70:
                recommendations.append("大量内存未被Python跟踪，可能是C扩展库内存泄漏，建议检查第三方库")
            
            if any('json' in pattern.lower() for pattern in suspicious_patterns):
                recommendations.append("JSON处理占用大量内存，建议使用流式解析或分批处理大JSON数据")
            
            if any('http' in pattern.lower() for pattern in suspicious_patterns):
                recommendations.append("HTTP响应对象占用大量内存，建议及时释放响应对象或使用流式下载")
            
            if any('cache' in pattern.lower() or 'pickle' in pattern.lower() for pattern in suspicious_patterns):
                recommendations.append("缓存或序列化对象占用大量内存，建议检查缓存策略和对象生命周期")
            
            hotspots['recommendations'] = recommendations
            
            # 记录分析结果
            self._memory_logger.info(f"内存热点分析完成: 高分配函数 {len(hotspots['high_allocation_functions'])} 个, "
                                     f"大对象模块 {len(hotspots['large_objects_by_module'])} 个, "
                                     f"可疑模式 {len(suspicious_patterns)} 个")
            
            if suspicious_patterns:
                self._memory_logger.warning("🔍 发现可疑内存使用模式:")
                for pattern in suspicious_patterns:
                    self._memory_logger.warning(f"  - {pattern}")
            
            if recommendations:
                self._memory_logger.info("💡 内存优化建议:")
                for rec in recommendations:
                    self._memory_logger.info(f"  - {rec}")
            
            return hotspots
            
        except Exception as e:
            self._memory_logger.error(f"分析内存热点失败: {e}")
            return {'error': str(e)}

    def print_detailed_memory_report(self) -> None:
        """
        生成并打印详细的内存使用报告到日志
        """
        try:
            self._memory_logger.info("=" * 80)
            self._memory_logger.info("📊 开始生成详细内存使用报告")
            self._memory_logger.info("=" * 80)
            
            report = self.generate_memory_report()
            
            # 1. 基本内存信息
            basic = report.get('basic_info', {})
            self._memory_logger.info(f"💾 基本内存信息:")
            self._memory_logger.info(f"  - RSS内存: {basic.get('rss', 0):.1f}MB")
            self._memory_logger.info(f"  - VMS内存: {basic.get('vms', 0):.1f}MB")
            self._memory_logger.info(f"  - 进程内存占用: {basic.get('percent', 0):.1f}%")
            self._memory_logger.info(f"  - 系统内存使用率: {basic.get('system_percent', 0):.1f}%")
            self._memory_logger.info(f"  - 系统可用内存: {basic.get('system_available', 0):.1f}GB")
            
            # 2. 内存差异分析
            memory_diff = report.get('memory_diff', {})
            self._memory_logger.info(f"\n🔍 内存跟踪差异分析:")
            self._memory_logger.info(f"  - PSUtil统计内存: {memory_diff.get('psutil_rss_mb', 0):.1f}MB")
            self._memory_logger.info(f"  - Tracemalloc统计内存: {memory_diff.get('tracemalloc_total_mb', 0):.1f}MB")
            self._memory_logger.info(f"  - 未跟踪内存: {memory_diff.get('untracked_memory_mb', 0):.1f}MB")
            self._memory_logger.info(f"  - 未跟踪比例: {memory_diff.get('untracked_percentage', 0):.1f}%")
            
            # 3. 内存使用最多的文件
            top_files = report.get('top_memory_files', [])
            if top_files:
                self._memory_logger.info(f"\n📁 内存使用最多的文件 (Top 10):")
                for i, file_info in enumerate(top_files[:10], 1):
                    self._memory_logger.info(f"  {i:2d}. {file_info.get('filename', 'unknown'):30s} "
                                           f"{file_info.get('size_mb', 0):8.2f}MB "
                                           f"({file_info.get('count', 0):,} 次分配)")
            
            # 4. 大对象分析
            large_objects = report.get('large_objects', [])
            if large_objects:
                self._memory_logger.info(f"\n🏗️ 大对象类型分析 (Top 10):")
                for i, obj in enumerate(large_objects[:10], 1):
                    self._memory_logger.info(f"  {i:2d}. {obj.get('type', 'unknown'):25s} "
                                           f"{obj.get('total_size_mb', 0):8.1f}MB "
                                           f"({obj.get('count', 0):,} 个对象, "
                                           f"平均 {obj.get('avg_size_kb', 0):.1f}KB)")
                    
                    # 显示大实例
                    large_instances = obj.get('large_instances', [])
                    if large_instances:
                        for instance in large_instances[:3]:  # 只显示前3个
                            self._memory_logger.info(f"      └─ 大实例: {instance.get('size_mb', 0):.2f}MB - "
                                                   f"{instance.get('repr', 'N/A')[:60]}...")
            
            # 5. 内存热点分析
            hotspots = report.get('memory_hotspots', {})
            high_alloc_funcs = hotspots.get('high_allocation_functions', [])
            if high_alloc_funcs:
                self._memory_logger.info(f"\n🔥 高内存分配函数:")
                for i, func in enumerate(high_alloc_funcs[:10], 1):
                    self._memory_logger.info(f"  {i:2d}. {func.get('filename', 'unknown')}:{func.get('lineno', 0)} "
                                           f"- {func.get('size_mb', 0):.2f}MB "
                                           f"({func.get('allocations', 0):,} 次分配)")
            
            suspicious_patterns = hotspots.get('suspicious_patterns', [])
            if suspicious_patterns:
                self._memory_logger.info(f"\n⚠️ 可疑内存使用模式:")
                for i, pattern in enumerate(suspicious_patterns, 1):
                    self._memory_logger.info(f"  {i}. {pattern}")
            
            recommendations = hotspots.get('recommendations', [])
            if recommendations:
                self._memory_logger.info(f"\n💡 内存优化建议:")
                for i, rec in enumerate(recommendations, 1):
                    self._memory_logger.info(f"  {i}. {rec}")
            
            # 6. 引用循环分析
            ref_cycles = report.get('reference_cycles', {})
            if ref_cycles and not ref_cycles.get('error'):
                self._memory_logger.info(f"\n🔄 引用循环分析:")
                self._memory_logger.info(f"  - 回收的循环: {ref_cycles.get('cycles_collected', 0)} 个")
                self._memory_logger.info(f"  - 释放的对象: {ref_cycles.get('objects_freed', 0)} 个")
                self._memory_logger.info(f"  - 垃圾对象: {ref_cycles.get('garbage_objects', 0)} 个")
                
                garbage_types = ref_cycles.get('garbage_types', {})
                if garbage_types:
                    self._memory_logger.info(f"  - 垃圾对象类型: {garbage_types}")
            
            # 7. 内存泄漏分析
            leak_analysis = report.get('leak_analysis', {})
            if leak_analysis:
                self._memory_logger.info(f"\n🚨 内存泄漏分析:")
                self._memory_logger.info(f"  - 状态: {leak_analysis.get('status', 'unknown')}")
                self._memory_logger.info(f"  - 详情: {leak_analysis.get('message', 'N/A')}")
                if 'growth_rate_mb' in leak_analysis:
                    self._memory_logger.info(f"  - 增长率: {leak_analysis['growth_rate_mb']:.2f}MB/次检查")
            
            # 8. 内存趋势
            memory_trend = report.get('memory_trend', [])
            if len(memory_trend) >= 2:
                first_record = memory_trend[0]
                last_record = memory_trend[-1]
                time_diff = (last_record['timestamp'] - first_record['timestamp']).total_seconds() / 60
                memory_diff_mb = last_record['memory_info']['rss'] - first_record['memory_info']['rss']
                
                self._memory_logger.info(f"\n📈 内存趋势 (最近 {len(memory_trend)} 个记录):")
                self._memory_logger.info(f"  - 时间跨度: {time_diff:.1f} 分钟")
                self._memory_logger.info(f"  - 内存变化: {memory_diff_mb:+.1f}MB")
                self._memory_logger.info(f"  - 平均变化率: {memory_diff_mb/time_diff:+.2f}MB/分钟")
            
            # 9. 系统信息
            detailed_info = report.get('detailed_info', {})
            if detailed_info:
                self._memory_logger.info(f"\n🖥️ 系统信息:")
                self._memory_logger.info(f"  - 线程数量: {detailed_info.get('thread_count', 0)}")
                self._memory_logger.info(f"  - 文件描述符: {detailed_info.get('fd_count', 0)}")
                
                gc_info = detailed_info.get('gc_info', {})
                if gc_info:
                    self._memory_logger.info(f"  - GC计数: Gen0={gc_info.get('gen_0', 0)}, "
                                           f"Gen1={gc_info.get('gen_1', 0)}, "
                                           f"Gen2={gc_info.get('gen_2', 0)}")
            
            self._memory_logger.info("=" * 80)
            self._memory_logger.info("📊 详细内存报告生成完成")
            self._memory_logger.info("=" * 80)
            
        except Exception as e:
            self._memory_logger.error(f"打印详细内存报告失败: {e}")
            import traceback
            self._memory_logger.error(f"错误详情: {traceback.format_exc()}")

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
        if event_data.key not in ['MEMORY_MONITOR_ENABLE', 'MEMORY_DETAILED_ANALYSIS', 'BIG_MEMORY_MODE']:
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

        # 设置内存阈值
        self.set_threshold(settings.CONF['memory'])

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

    def print_memory_report(self) -> None:
        """
        手动生成并打印详细内存报告
        """
        try:
            self.print_detailed_memory_report()
        except Exception as e:
            self._memory_logger.error(f"手动生成内存报告失败: {e}")
            logger.error(f"手动生成内存报告失败: {e}")


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
