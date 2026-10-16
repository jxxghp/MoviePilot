from collections.abc import Sized
from typing import Optional, TypeVar, Union, cast, overload
from xml.dom import minidom

_Default = TypeVar("_Default")


class DomUtils:
    """提供不含业务状态的 XML/HTML DOM 基础能力。"""

    @staticmethod
    def has_child_elements(element: Optional[Sized]) -> bool:
        """判断 DOM 元素是否存在且至少包含一个子元素。"""
        return element is not None and len(element) > 0

    @staticmethod
    @overload
    def tag_value(tag_item: Union[minidom.Document, minidom.Element], tag_name: str,
                  attname: str = "", default: None = None) -> Optional[str]:
        """未指定默认值时，缺失的文本或属性仍返回 None。"""
        ...

    @staticmethod
    @overload
    def tag_value(tag_item: Union[minidom.Document, minidom.Element], tag_name: str,
                  attname: str = "", default: _Default = ...) -> Union[str, _Default]:
        """保留调用方显式默认值的类型。"""
        ...

    @staticmethod
    def tag_value(tag_item: Union[minidom.Document, minidom.Element], tag_name: str,
                  attname: str = "", default: object = None) -> object:
        """解析 XML 标签文本或属性，缺失时原样返回默认值。"""
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
        """添加 DOM 节点，沿用非空值统一转换为文本的规则。"""
        node = doc.createElement(name)
        parent.appendChild(node)
        if value is not None:
            text = doc.createTextNode(str(value))
            node.appendChild(text)
        return node
