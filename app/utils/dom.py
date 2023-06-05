from typing import Union


class DomUtils:

    @staticmethod
    def tag_value(tag_item, tag_name: str, attname: str = "", default: Union[str, int] = None):
        """
        解析XML标签值
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
                    return firstChild.data
        return default

    @staticmethod
    def add_node(doc, parent, name: str, value: str = None):
        """
        添加一个DOM节点
        """
        node = doc.createElement(name)
        parent.appendChild(node)
        if value is not None:
            text = doc.createTextNode(str(value))
            node.appendChild(text)
        return node
