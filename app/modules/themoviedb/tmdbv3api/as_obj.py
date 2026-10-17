"""
历史 TMDB JSON 属性对象与集合访问的兼容包装。
"""

# encoding: utf-8
import sys
from collections.abc import ItemsView, Iterable, Iterator, KeysView, Mapping, ValuesView
from typing import Optional, TypeAlias, Union

JsonContainer: TypeAlias = Union[dict[str, object], list[object]]


class AsObj:
    """
    兼容历史 TMDB JSON 属性访问和部分字典、列表操作。

    嵌套对象递归包装为 AsObj；公开字典视图是浅层视图，不递归还原 JSON。
    属性更新与原始 _json 数据并非同一份状态，copy 会从原始数据重新构造。
    """
    def __init__(
        self,
        json: Optional[JsonContainer] = None,
        key: Optional[str] = None,
        dict_key: bool = False,
        dict_key_name: Optional[str] = None,
    ) -> None:
        """
        把 JSON 对象或数组包装为属性及集合访问对象。

        :param json: 待包装的对象或数组；空值按空对象处理
        :param key: 指定作为集合迭代来源的字段
        :param dict_key: 是否将字典各项包装成可迭代元素
        :param dict_key_name: 可选属性名，用于记录被包装字典项的原始键
        """
        self._json = json if json else {}
        self._key = key
        self._dict_key = dict_key
        self._dict_key_name = dict_key_name
        self._obj_list: Union[list[object], AsObj] = []
        self._list_only = False
        if isinstance(self._json, list):
            self._obj_list = [AsObj(o) if isinstance(o, (dict, list)) else o for o in self._json]
            self._list_only = True
        elif dict_key:
            self._obj_list = [
                AsObj({k: v}, key=k, dict_key_name=dict_key_name) if isinstance(v, (dict, list)) else v
                for k, v in self._json.items()
            ]
            self._list_only = True
        else:
            for key, value in self._json.items():
                if isinstance(value, (dict, list)):
                    if self._key and key == self._key:
                        nested = AsObj(
                            value, dict_key=isinstance(value, dict), dict_key_name=key
                        )
                        self._obj_list = nested
                        final: object = nested
                    else:
                        final = AsObj(value)
                else:
                    final = value
                if dict_key_name:
                    setattr(self, dict_key_name, key)
                setattr(self, key, final)

    def _dict(self) -> dict[str, object]:
        """
        返回当前公开属性的浅层视图，不展开嵌套包装对象。

        :return: 以属性名为键的临时字典
        """
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}

    def to_dict(self) -> dict[str, object]:
        """
        生成仅包含公开属性的浅层字典。

        :return: 新字典；嵌套 AsObj 保持包装对象，不递归展开
        """
        return self._dict()

    def __delitem__(self, key: str) -> None:
        """
        删除指定实例属性。

        :param key: 待删除的属性名称
        :return: 无返回值
        """
        return delattr(self, key)

    def __getitem__(self, key: Union[int, str]) -> object:
        """
        有集合内容时按整数下标取值，其他情况按属性名称读取。

        :param key: 集合下标或属性名称
        :return: 下标对应元素或属性值
        """
        if isinstance(key, int) and self._obj_list:
            return self._obj_list[key]
        if isinstance(key, str):
            return getattr(self, key)
        raise TypeError(f"attribute name must be string, not '{type(key).__name__}'")

    def __iter__(self) -> Iterator[object]:
        """
        有集合内容时迭代元素，否则迭代公开属性名称。

        :return: 集合元素或公开属性名的迭代器
        """
        if self._obj_list:
            return (o for o in self._obj_list)
        return iter(self._dict())

    def __len__(self) -> int:
        """
        返回集合元素数或公开属性数。

        :return: 当前视图中的项目数量
        """
        return len(self._obj_list) if self._obj_list else len(self._dict())

    def __repr__(self) -> str:
        """
        返回列表或公开属性字典的调试文本。

        :return: 当前视图的文本表示
        """
        return str(self._obj_list) if self._list_only else str(self._dict())

    def __setitem__(self, key: str, value: object) -> None:
        """
        把指定值写入同名实例属性。

        :param key: 属性名称
        :param value: 待写入的值
        :return: 无返回值
        """
        return setattr(self, key, value)

    def __str__(self) -> str:
        """
        返回列表或公开属性字典的展示文本。

        :return: 当前视图的文本表示
        """
        return str(self._obj_list) if self._list_only else str(self._dict())

    if sys.version_info >= (3, 8):
        def __reversed__(self) -> Iterator[str]:
            """
            逆序遍历公开属性名。

            :return: 属性名称的逆序迭代器
            """
            return reversed(self._dict())

    if sys.version_info >= (3, 9):
        def __class_getitem__(cls, key: object) -> object:
            """
            按历史实现转发类下标访问。

            :param key: 类下标参数
            :return: 底层映射代理给出的结果
            """
            return cls.__dict__.__class_getitem__(key)

        def __ior__(self, value: dict[str, object]) -> dict[str, object]:
            """
            返回公开属性字典与输入合并后的普通字典，不原地更新包装对象。

            :param value: 参与合并的字典
            :return: 合并后的普通字典
            """
            return self._dict().__ior__(value)

        def __or__(self, value: dict[str, object]) -> dict[str, object]:
            """
            返回公开属性字典与输入合并后的普通字典。

            :param value: 参与合并的字典
            :return: 合并后的普通字典
            """
            return self._dict().__or__(value)

    def copy(self) -> AsObj:
        """
        基于原始 JSON 的浅拷贝重新构造包装对象。

        :return: 新的 AsObj；后续直接修改的属性不会自动回写到原始 JSON
        """
        return AsObj(self._json.copy(), key=self._key, dict_key=self._dict_key, dict_key_name=self._dict_key_name)

    def get(self, key: str, value: object = None) -> object:
        """
        从公开属性字典读取键值。

        :param key: 待查询公开属性名称
        :param value: 键不存在时返回的默认值
        :return: 属性值或调用方默认值
        """
        return self._dict().get(key, value)

    def items(self) -> ItemsView[str, object]:
        """
        读取公开属性的键值对视图。

        :return: 临时浅层字典的键值对视图
        """
        return self._dict().items()

    def keys(self) -> KeysView[str]:
        """
        读取公开属性名称视图。

        :return: 临时浅层字典的键视图
        """
        return self._dict().keys()

    def pop(self, key: str, value: object = None) -> object:
        """
        从实例属性中移除指定键，允许操作内部属性。

        :param key: 待移除的实例属性名
        :param value: 不存在时使用的默认值
        :return: 移除值或默认值
        """
        return self.__dict__.pop(key, value)

    def popitem(self) -> tuple[str, object]:
        """
        从实例属性中移除并返回末尾一项，可能包含内部属性。

        :return: 被移除的键值对
        """
        return self.__dict__.popitem()

    def setdefault(self, key: str, value: object = None) -> object:
        """
        在实例属性不存在指定键时设置默认值。

        :param key: 实例属性名称，可包含内部属性
        :param value: 不存在该属性时保存的默认值
        :return: 原属性值或新默认值
        """
        return self.__dict__.setdefault(key, value)

    def update(self, entries: Union[Mapping[str, object], Iterable[tuple[str, object]]]) -> None:
        """
        直接合并实例属性，允许覆盖内部属性。

        :param entries: 按字典 update 规则提供的更新键值对
        """
        return self.__dict__.update(entries)

    def values(self) -> ValuesView[object]:
        """
        读取公开属性值视图。

        :return: 临时浅层字典的值视图
        """
        return self._dict().values()
