import sys
import time
from collections import deque
from typing import Any, Dict, Set

from app.log import logger


class MemoryCalculator:
    """
    内存计算器，用于递归计算对象的内存占用
    """

    def __init__(self):
        # 缓存已计算的对象ID，避免重复计算
        self._calculated_ids: Set[int] = set()
        # 最大递归深度，防止无限递归
        self._max_depth = 10
        # 最大对象数量，防止计算过多对象
        self._max_objects = 10000

    def calculate_object_memory(self, obj: Any, max_depth: int = None, max_objects: int = None) -> Dict[str, Any]:
        """
        计算对象的内存占用
        :param obj: 要计算的对象
        :param max_depth: 最大递归深度
        :param max_objects: 最大对象数量
        :return: 内存统计信息
        """
        if max_depth is None:
            max_depth = self._max_depth
        if max_objects is None:
            max_objects = self._max_objects

        # 重置缓存
        self._calculated_ids.clear()

        start_time = time.time()
        object_details = []

        try:
            # 递归计算内存
            memory_info = self._calculate_recursive(obj, depth=0, max_depth=max_depth,
                                                    max_objects=max_objects, object_count=0)
            total_memory = memory_info['total_memory']
            object_count = memory_info['object_count']
            object_details = memory_info['object_details']

        except Exception as e:
            logger.error(f"计算对象内存时出错：{str(e)}")
            total_memory = 0
            object_count = 0

        calculation_time = time.time() - start_time

        return {
            'total_memory_bytes': total_memory,
            'total_memory_mb': round(total_memory / (1024 * 1024), 2),
            'object_count': object_count,
            'calculation_time_ms': round(calculation_time * 1000, 2),
            'object_details': object_details[:10]  # 只返回前10个最大的对象
        }

    def _calculate_recursive(self, obj: Any, depth: int, max_depth: int,
                             max_objects: int, object_count: int) -> Dict[str, Any]:
        """
        递归计算对象内存
        """
        if depth > max_depth or object_count > max_objects:
            return {
                'total_memory': 0,
                'object_count': object_count,
                'object_details': []
            }

        total_memory = 0
        object_details = []

        # 获取对象ID，避免重复计算
        obj_id = id(obj)
        if obj_id in self._calculated_ids:
            return {
                'total_memory': 0,
                'object_count': object_count,
                'object_details': []
            }

        self._calculated_ids.add(obj_id)
        object_count += 1

        try:
            # 计算对象本身的内存
            obj_memory = sys.getsizeof(obj)
            total_memory += obj_memory

            # 记录大对象
            if obj_memory > 1024:  # 大于1KB的对象
                object_details.append({
                    'type': type(obj).__name__,
                    'memory_bytes': obj_memory,
                    'memory_mb': round(obj_memory / (1024 * 1024), 2),
                    'depth': depth
                })

            # 递归计算容器对象的内容
            if depth < max_depth:
                container_memory = self._calculate_container_memory(
                    obj, depth + 1, max_depth, max_objects, object_count
                )
                total_memory += container_memory['total_memory']
                object_count = container_memory['object_count']
                object_details.extend(container_memory['object_details'])

        except Exception as e:
            logger.debug(f"计算对象 {type(obj).__name__} 内存时出错：{str(e)}")

        return {
            'total_memory': total_memory,
            'object_count': object_count,
            'object_details': object_details
        }

    def _calculate_container_memory(self, obj: Any, depth: int, max_depth: int,
                                    max_objects: int, object_count: int) -> Dict[str, Any]:
        """
        计算容器对象的内存
        """
        total_memory = 0
        object_details = []

        try:
            # 处理不同类型的容器
            if isinstance(obj, (list, tuple, deque)):
                for item in obj:
                    if object_count > max_objects:
                        break
                    item_memory = self._calculate_recursive(item, depth, max_depth, max_objects, object_count)
                    total_memory += item_memory['total_memory']
                    object_count = item_memory['object_count']
                    object_details.extend(item_memory['object_details'])

            elif isinstance(obj, dict):
                for key, value in obj.items():
                    if object_count > max_objects:
                        break
                    # 计算key的内存
                    key_memory = self._calculate_recursive(key, depth, max_depth, max_objects, object_count)
                    total_memory += key_memory['total_memory']
                    object_count = key_memory['object_count']
                    object_details.extend(key_memory['object_details'])

                    # 计算value的内存
                    value_memory = self._calculate_recursive(value, depth, max_depth, max_objects, object_count)
                    total_memory += value_memory['total_memory']
                    object_count = value_memory['object_count']
                    object_details.extend(value_memory['object_details'])

            elif hasattr(obj, '__dict__'):
                # 处理有__dict__属性的对象
                for attr_name, attr_value in obj.__dict__.items():
                    if object_count > max_objects:
                        break
                    # 跳过一些特殊属性
                    if attr_name.startswith('_') and attr_name not in ['_calculated_ids']:
                        continue
                    attr_memory = self._calculate_recursive(attr_value, depth, max_depth, max_objects, object_count)
                    total_memory += attr_memory['total_memory']
                    object_count = attr_memory['object_count']
                    object_details.extend(attr_memory['object_details'])

        except Exception as e:
            logger.debug(f"计算容器对象 {type(obj).__name__} 内存时出错：{str(e)}")

        return {
            'total_memory': total_memory,
            'object_count': object_count,
            'object_details': object_details
        }
