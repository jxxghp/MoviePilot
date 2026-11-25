"""
DOM 类型收紧后仍保留默认值、文本转换与非法节点的既有行为。
"""

from xml.dom import minidom

import pytest

from app.foundation.dom import DomUtils


@pytest.mark.parametrize("default", [None, "", 0, False, object()])
def test_missing_value_preserves_default_identity(default):
    """
    缺失标签、空标签和空属性均返回调用方原始默认值。
    """
    document = minidom.parseString('<xml><empty attr="" /></xml>')
    assert DomUtils.tag_value(document, "missing", default=default) is default
    assert DomUtils.tag_value(document, "empty", default=default) is default
    assert DomUtils.tag_value(document, "empty", "attr", default=default) is default


@pytest.mark.parametrize("value", [None, "", 0, False, 8.5, "中文 & <文本>"])
def test_node_values_round_trip_without_losing_false_values(value):
    """
    仅 None 省略文本，数字和布尔值仍按历史 str 转换写入 XML。
    """
    document = minidom.Document()
    root = DomUtils.add_node(document, document, "xml")
    node = DomUtils.add_node(document, root, "value", value)
    assert node.parentNode is root
    assert node.ownerDocument is document
    if value is None:
        assert node.firstChild is None
    else:
        assert node.firstChild.data == str(value)
    parsed = minidom.parseString(document.toxml())
    assert DomUtils.tag_value(parsed, "value", default="") == ("" if value is None else str(value))


def test_tag_values_keep_cdata_attributes_and_unsupported_node_errors():
    """
    文本和属性保留内容，嵌套元素不被悄悄替换为默认值。
    """
    document = minidom.parseString('<xml><text attr="原值"><![CDATA[文本 & 内容]]></text><nested><child/></nested></xml>')
    assert DomUtils.tag_value(document, "text") == "文本 & 内容"
    assert DomUtils.tag_value(document, "text", "attr") == "原值"
    with pytest.raises(AttributeError):
        DomUtils.tag_value(document, "nested", default="fallback")
