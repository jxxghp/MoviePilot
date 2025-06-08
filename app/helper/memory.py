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

            # 获取当前进程的内存使用情况
            all_objects = muppy.get_objects()
            sum1 = summary.summarize(all_objects)

            # 获取系统内存使用情况
            memory_usage = psutil.Process().memory_info().rss

            # 写入内存快照文件
            with open(snapshot_file, 'w', encoding='utf-8') as f:
                f.write(f"内存快照时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"当前进程内存使用: {memory_usage / 1024 / 1024:.2f} MB\n")
                f.write("=" * 80 + "\n")
                f.write("对象类型统计:\n")
                f.write("-" * 80 + "\n")
                
                # 写入对象统计信息
                for line in summary.format_(sum1):
                    f.write(line + "\n")
                
                # 添加最大对象信息
                f.write("\n" + "=" * 80 + "\n")
                f.write("最大内存占用对象详情:\n")
                f.write("-" * 80 + "\n")
                
                try:
                    largest_objects = self._get_largest_objects()
                    if largest_objects:
                        for i, obj_info in enumerate(largest_objects[:10], 1):
                            f.write(f"{i:2d}. {obj_info['type']:<30} {obj_info['size_mb']:>8.2f} MB - {obj_info['description']}\n")
                    else:
                        f.write("未找到大于1KB的对象或分析失败\n")
                except Exception as e:
                    f.write(f"大对象分析失败: {e}\n")
                    logger.warning(f"大对象分析失败，但快照生成继续: {e}")

            logger.info(f"内存快照已保存: {snapshot_file}, 当前内存使用: {memory_usage / 1024 / 1024:.2f} MB")

            # 清理过期的快照文件（保留最近30个）
            self._cleanup_old_snapshots()

        except Exception as e:
            logger.error(f"创建内存快照失败: {e}")

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

    def _get_largest_objects(self, top_n: int = 20) -> list:
        """
        获取内存占用最大的对象列表
        :param top_n: 返回前N个最大对象
        :return: 对象信息列表
        """
        try:
            # 获取所有对象
            all_objects = muppy.get_objects()
            logger.debug(f"开始分析 {len(all_objects)} 个对象")
            
            # 计算每个对象的大小并收集信息
            object_sizes = []
            failed_count = 0
            skip_modules = {'tkinter', 'matplotlib', 'PIL', 'cv2'}  # 可能导致GUI库问题的模块
            
            for i, obj in enumerate(all_objects):
                try:
                    # 检查对象类型，跳过可能有问题的模块
                    obj_module = getattr(type(obj), '__module__', '')
                    if any(skip_mod in obj_module for skip_mod in skip_modules):
                        continue
                    
                    # 使用asizeof计算对象真实大小
                    size = asizeof.asizeof(obj)
                    if size > 1024:  # 只关注大于1KB的对象
                        obj_type = type(obj).__name__
                        
                        # 生成对象描述
                        description = self._generate_object_description(obj)
                        
                        object_sizes.append({
                            'size': size,
                            'type': f"{obj_module}.{obj_type}" if obj_module and obj_module != 'builtins' else obj_type,
                            'description': description
                        })
                        
                except (TypeError, AttributeError, RuntimeError, OSError, ImportError) as e:
                    # 扩展异常处理，包括共享库错误
                    failed_count += 1
                    if failed_count <= 5:  # 只记录前几个错误，避免日志泛滥
                        logger.debug(f"跳过对象分析 (第{i+1}个): {type(e).__name__}: {str(e)[:100]}")
                    continue
                except Exception as e:
                    # 处理其他未预期的异常
                    failed_count += 1
                    if failed_count <= 5:
                        logger.debug(f"跳过对象分析 (第{i+1}个): 未知错误: {str(e)[:100]}")
                    continue
            
            if failed_count > 5:
                logger.debug(f"总共跳过了 {failed_count} 个无法分析的对象")
            
            logger.debug(f"成功分析了 {len(object_sizes)} 个大对象")
            
            # 按大小排序并取前N个
            object_sizes.sort(key=lambda x: x['size'], reverse=True)
            
            # 转换为所需格式
            return [
                {
                    'type': obj_info['type'],
                    'size_mb': obj_info['size'] / 1024 / 1024,
                    'size_bytes': obj_info['size'],
                    'description': obj_info['description']
                }
                for obj_info in object_sizes[:top_n]
            ]
            
        except Exception as e:
            logger.error(f"获取最大对象信息失败: {e}")
            return []

    @staticmethod
    def _generate_object_description(obj) -> str:
        """
        生成对象的描述信息
        :param obj: 要描述的对象
        :return: 对象描述字符串
        """
        try:
            # 获取对象类型名称，避免访问可能有问题的属性
            obj_type_name = type(obj).__name__
            
            # 根据对象类型生成不同的描述
            if isinstance(obj, (list, tuple)):
                try:
                    length = len(obj)
                    first_type = type(obj[0]).__name__ if obj else 'empty'
                    return f"长度={length}, 示例元素类型={first_type}"
                except (IndexError, TypeError):
                    return f"{obj_type_name}(长度未知)"
            
            elif isinstance(obj, dict):
                try:
                    length = len(obj)
                    if obj:
                        first_key = next(iter(obj))
                        key_type = type(first_key).__name__
                        return f"键值对数={length}, 示例键类型={key_type}"
                    return f"键值对数={length}, 示例键类型=empty"
                except (StopIteration, TypeError):
                    return f"{obj_type_name}(大小未知)"
            
            elif isinstance(obj, set):
                try:
                    length = len(obj)
                    if obj:
                        first_item = next(iter(obj))
                        item_type = type(first_item).__name__
                        return f"元素数={length}, 示例元素类型={item_type}"
                    return f"元素数={length}, 示例元素类型=empty"
                except (StopIteration, TypeError):
                    return f"{obj_type_name}(大小未知)"
            
            elif isinstance(obj, str):
                try:
                    length = len(obj)
                    # 限制预览长度，避免显示问题字符
                    safe_preview = ''.join(c for c in obj[:30] if c.isprintable())
                    preview = safe_preview + '...' if length > 30 else safe_preview
                    return f"长度={length}, 内容='{preview}'"
                except (UnicodeError, TypeError):
                    return f"{obj_type_name}(字符串，长度未知)"
            
            elif isinstance(obj, bytes):
                try:
                    length = len(obj)
                    return f"字节数据，长度={length}"
                except TypeError:
                    return f"{obj_type_name}(字节数据，长度未知)"
            
            # 谨慎处理可能有问题的属性访问
            try:
                if hasattr(obj, '__name__') and callable(getattr(obj, '__name__', None)):
                    name = str(obj.__name__)[:50]
                    return f"名称={name}"
            except (AttributeError, TypeError, OSError):
                pass
            
            try:
                if hasattr(obj, '__dict__'):
                    attrs_count = len(obj.__dict__)
                    return f"实例属性数={attrs_count}"
            except (AttributeError, TypeError, OSError):
                pass
            
            # 最后的安全转换
            try:
                obj_str = str(obj)[:50]
                # 确保字符串是可打印的
                safe_str = ''.join(c for c in obj_str if c.isprintable())
                return f"对象={safe_str}" if safe_str else f"{obj_type_name}对象"
            except (UnicodeError, TypeError, OSError):
                return f"{obj_type_name}对象"
                
        except Exception as e:
            # 最后的异常处理，确保总是返回有用信息
            try:
                obj_type_name = type(obj).__name__
                return f"{obj_type_name}(描述生成失败)：{e}"
            except Exception as e:
                return f"未知对象(描述生成失败)：{e}"
