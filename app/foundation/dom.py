"""
提供 XML DOM 标签读取与节点构造。
"""

from collections.abc import Sized
from typing import Optional, TypeVar, Union, cast, overload
from xml.dom import minidom

_Default = TypeVar("_Default")


class DomUtils:
    """
    提供不含业务状态的 XML/HTML DOM 基础能力。
    """

    @staticmethod
    def has_child_elements(element: Optional[Sized]) -> bool:
        """
        判断 DOM 元素是否存在且至少包含一个子元素。
        """
        return element is not None and len(element) > 0

    @staticmethod
    @overload
    def tag_value(tag_item: Union[minidom.Document, minidom.Element], tag_name: str,
                  attname: str = "", default: None = None) -> Optional[str]:
        """
        读取首个同名 XML 标签的文本或指定属性。

        :param tag_item: 待查询的文档或父元素
        :param tag_name: 要查找的标签名
        :param attname: 要读取的属性名；为空时读取首个子节点文本
        :param default: 标签、属性或文本不存在时原样返回的默认值
        :return: 命中的文本、属性值或调用方默认值
        :raises AttributeError: 文本分支命中的首个子节点不支持 data 属性
        """
        ...

    @staticmethod
    @overload
    def tag_value(tag_item: Union[minidom.Document, minidom.Element], tag_name: str,
                  attname: str = "", default: _Default = ...) -> Union[str, _Default]:
        """
        读取首个同名 XML 标签的文本或指定属性。

        :param tag_item: 待查询的文档或父元素
        :param tag_name: 要查找的标签名
        :param attname: 要读取的属性名；为空时读取首个子节点文本
        :param default: 标签、属性或文本不存在时原样返回的默认值
        :return: 命中的文本、属性值或调用方默认值
        :raises AttributeError: 文本分支命中的首个子节点不支持 data 属性
        """
        ...

    @staticmethod
    def tag_value(tag_item: Union[minidom.Document, minidom.Element], tag_name: str,
                  attname: str = "", default: object = None) -> object:
        """
        读取首个同名 XML 标签的文本或指定属性。

        :param tag_item: 待查询的文档或父元素
        :param tag_name: 要查找的标签名
        :param attname: 要读取的属性名；为空时读取首个子节点文本
        :param default: 标签、属性或文本不存在时原样返回的默认值
        :return: 命中的文本、属性值或调用方默认值
        :raises AttributeError: 文本分支命中的首个子节点不支持 data 属性
        """
        tagNames = tag_item.getElementsByTagName(tag_name)
        if tagNames:
            if attname:
                attvalue = tagNames[0].getAttribute(attname)
                if attvalue:
                    return attvalue
            else:
                firstChild = tagNames[0].firstChild
                if firstChild:
                    # minidom 节点的 data 是文本；不支持 data 的节点仍抛 AttributeError。
                    return cast(str, getattr(firstChild, "data"))
        return default

    @staticmethod
    def add_node(doc: minidom.Document,
                 parent: Union[minidom.Document, minidom.Element, minidom.DocumentFragment],
                 name: str, value: object = None) -> minidom.Element:
        """
        向父节点追加一个 XML 元素。

        :param doc: 用于创建节点的所属文档
        :param parent: 接收新节点的父节点，调用时会被修改
        :param name: 新元素的标签名
        :param value: 元素文本；None 表示不添加文本节点，其余值转换为字符串
        :return: 已追加到父节点的新元素
        """
        node = doc.createElement(name)
        parent.appendChild(node)
        if value is not None:
            text = doc.createTextNode(str(value))
            node.appendChild(text)
        return node
