"""
提供字典键筛选与列表、集合的单层展开。
"""

from typing import Any, Dict, List, Set, TypeVar, Union

K = TypeVar("K")
V = TypeVar("V")


class DictUtils:
    """
    提供字典键集合相关的基础操作。
    """

    @staticmethod
    def filter_keys_to_subset(source: Dict[K, V], reference: Dict[K, V]) -> Dict[K, V]:
        """
        过滤 source 字典，使其键成为 reference 字典键的子集。

        :param source: 要被过滤的字典
        :param reference: 参考字典，定义允许的键
        :return: 过滤后的字典，只包含在 reference 中存在的键
        """
        if not isinstance(source, dict) or not isinstance(reference, dict):
            return {}

        return {key: value for key, value in source.items() if key in reference}

    @staticmethod
    def is_keys_subset(source: Dict[K, V], reference: Dict[K, V]) -> bool:
        """
        判断 source 字典的键是否为 reference 字典键的子集。

        :param source: 要检查的字典
        :param reference: 参考字典
        :return: 如果 source 的键是 reference 的键子集，则返回 True，否则返回 False
        """
        if not isinstance(source, dict) or not isinstance(reference, dict):
            return False

        return all(key in reference for key in source)


class ListUtils:
    """
    提供列表结构的基础转换能力。
    """

    @staticmethod
    def flatten(nested_list: Union[List[List[Any]], List[Any]]) -> List[Any]:
        """
        将列表展开一层，保留非列表元素。

        :param nested_list: 待展开的列表；非列表输入返回空列表
        :return: 含子列表时返回新列表，否则原样返回输入列表
        """
        if not isinstance(nested_list, list):
            return []

        if not any(isinstance(sublist, list) for sublist in nested_list):
            return nested_list

        return [
            item
            for sublist in nested_list
            for item in (sublist if isinstance(sublist, list) else [sublist])
        ]


class SetUtils:
    """
    提供集合结构的基础转换能力。
    """

    @staticmethod
    def flatten(nested_sets: Union[Set[Set[Any]], Set[Any]]) -> Set[Any]:
        """
        按历史规则展开嵌套集合。

        :param nested_sets: 待展开的集合；非集合输入返回空集合
        :return: 不含子集合时原样返回；含子集合时仅合并子集合元素
        """
        if not isinstance(nested_sets, set):
            return set()

        if not any(isinstance(subset, set) for subset in nested_sets):
            return nested_sets

        return {item for subset in nested_sets if isinstance(subset, set) for item in subset}
