from typing import Dict, List, Set, TypeVar, Any, Union

K = TypeVar("K")
V = TypeVar("V")


class DictUtils:
    @staticmethod
    def filter_keys_to_subset(source: Dict[K, V], reference: Dict[K, V]) -> Dict[K, V]:
        """
        过滤 source 字典，使其键成为 reference 字典键的子集

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
        判断 source 字典的键是否为 reference 字典键的子集

        :param source: 要检查的字典
        :param reference: 参考字典
        :return: 如果 source 的键是 reference 的键子集，则返回 True，否则返回 False
        """
        if not isinstance(source, dict) or not isinstance(reference, dict):
            return False

        return all(key in reference for key in source)


class ListUtils:
    @staticmethod
    def flatten(nested_list: Union[List[List[Any]], List[Any]]) -> List[Any]:
        """
        将嵌套的列表展平成单个列表

        :param nested_list: 嵌套的列表
        :return: 展平后的列表
        """
        if not isinstance(nested_list, list):
            return []

        # 检查是否嵌套，若不嵌套直接返回
        if not any(isinstance(sublist, list) for sublist in nested_list):
            return nested_list

        return [item for sublist in nested_list if isinstance(sublist, list) for item in sublist]


class SetUtils:
    @staticmethod
    def flatten(nested_sets: Union[Set[Set[Any]], Set[Any]]) -> Set[Any]:
        """
        将嵌套的集合展开为单个集合

        :param nested_sets: 嵌套的集合
        :return: 展开的集合
        """
        if not isinstance(nested_sets, set):
            return set()

        # 检查是否嵌套，若不嵌套直接返回
        if not any(isinstance(subset, set) for subset in nested_sets):
            return nested_sets

        return {item for subset in nested_sets if isinstance(subset, set) for item in subset}
