"""
历史 TMDB JSON 属性对象与集合访问的兼容包装。
"""

# encoding: utf-8
import sys


class AsObj:
    """
    兼容历史 TMDB JSON 属性访问和部分字典、列表操作。

    嵌套对象递归包装为 AsObj；公开字典视图是浅层视图，不递归还原 JSON。
    属性更新与原始 _json 数据并非同一份状态，copy 会从原始数据重新构造。
    """
    def __init__(self, json=None, key=None, dict_key=False, dict_key_name=None):
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
        self._obj_list = []
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
                        final = AsObj(value, dict_key=isinstance(value, dict), dict_key_name=key)
                        self._obj_list = final
                    else:
                        final = AsObj(value)
                else:
                    final = value
                if dict_key_name:
                    setattr(self, dict_key_name, key)
                setattr(self, key, final)

    def _dict(self):
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}

    def to_dict(self):
        """
        生成仅包含公开属性的浅层字典。

        :return: 新字典；嵌套 AsObj 保持包装对象，不递归展开
        """
        return self._dict()

    def __delitem__(self, key):
        return delattr(self, key)

    def __getitem__(self, key):
        """
        有集合内容时按整数下标取值，其他情况按属性名称读取。
        """
        if isinstance(key, int) and self._obj_list:
            return self._obj_list[key]
        else:
            return getattr(self, key)

    def __iter__(self):
        """
        有集合内容时迭代元素，否则迭代公开属性名称。
        """
        return (o for o in self._obj_list) if self._obj_list else iter(self._dict())

    def __len__(self):
        return len(self._obj_list) if self._obj_list else len(self._dict())

    def __repr__(self):
        return str(self._obj_list) if self._list_only else str(self._dict())

    def __setitem__(self, key, value):
        return setattr(self, key, value)

    def __str__(self):
        return str(self._obj_list) if self._list_only else str(self._dict())

    if sys.version_info >= (3, 8):
        def __reversed__(self):
            return reversed(self._dict())

    if sys.version_info >= (3, 9):
        def __class_getitem__(cls, key):
            return cls.__dict__.__class_getitem__(key)

        def __ior__(self, value):
            """
            返回公开属性字典与输入合并后的普通字典，不原地更新包装对象。
            """
            return self._dict().__ior__(value)

        def __or__(self, value):
            """
            返回公开属性字典与输入合并后的普通字典。
            """
            return self._dict().__or__(value)

    def copy(self):
        """
        基于原始 JSON 的浅拷贝重新构造包装对象。

        :return: 新的 AsObj；后续直接修改的属性不会自动回写到原始 JSON
        """
        return AsObj(self._json.copy(), key=self._key, dict_key=self._dict_key, dict_key_name=self._dict_key_name)

    def get(self, key, value=None):
        """
        从公开属性字典读取键值。

        :param key: 待查询公开属性名称
        :param value: 键不存在时返回的默认值
        :return: 属性值或调用方默认值
        """
        return self._dict().get(key, value)

    def items(self):
        """
        读取公开属性的键值对视图。

        :return: 临时浅层字典的键值对视图
        """
        return self._dict().items()

    def keys(self):
        """
        读取公开属性名称视图。

        :return: 临时浅层字典的键视图
        """
        return self._dict().keys()

    def pop(self, key, value=None):
        """
        从实例属性中移除指定键，允许操作内部属性。

        :param key: 待移除的实例属性名
        :param value: 不存在时使用的默认值
        :return: 移除值或默认值
        """
        return self.__dict__.pop(key, value)

    def popitem(self):
        """
        从实例属性中移除并返回末尾一项，可能包含内部属性。

        :return: 被移除的键值对
        """
        return self.__dict__.popitem()

    def setdefault(self, key, value=None):
        """
        在实例属性不存在指定键时设置默认值。

        :param key: 实例属性名称，可包含内部属性
        :param value: 不存在该属性时保存的默认值
        :return: 原属性值或新默认值
        """
        return self.__dict__.setdefault(key, value)

    def update(self, entries):
        """
        直接合并实例属性，允许覆盖内部属性。

        :param entries: 按字典 update 规则提供的更新键值对
        """
        return self.__dict__.update(entries)

    def values(self):
        """
        读取公开属性值视图。

        :return: 临时浅层字典的值视图
        """
        return self._dict().values()
