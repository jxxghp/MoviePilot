"""
验证工具存根的文档转义不会改变参数传递或注入可执行源码。
"""

import ast
import inspect

import pytest

from app.adapters.system.code.client import build_client


@pytest.mark.parametrize("description", [
    '普通中文说明',
    '包含三引号 """ 和反斜杠 \\ 的说明',
    '首行\n    """\nraise RuntimeError("不得执行")\n# 末行',
    '包含回车\r、制表符\t和空字符\x00的说明',
])
def test_schema_documentation_cannot_change_generated_tool_behavior(description):
    """
    不可信 Schema 文本只能成为文档，必填参数与可选参数的省略语义保持不变。
    """
    schema = {
        "type": "object",
        "description": description,
        "properties": {
            "value": {"type": "string", "description": description},
            "optional": {"type": ["string", "null"]},
        },
        "required": ["value"],
    }
    source = build_client(("example",), {"example": schema})
    namespace = {}
    exec(compile(source, "<generated-client>", "exec"), namespace)
    namespace["_call"] = lambda name, arguments: (name, arguments)
    tool = namespace["example"]
    assert tool("输入") == ("example", {"value": "输入"})
    assert tool("输入", optional=None) == ("example", {"value": "输入", "optional": None})
    assert inspect.signature(tool).parameters["value"].default is inspect.Parameter.empty
    assert "输入 Schema：" in tool.__doc__
    node = next(item for item in ast.parse(source).body if isinstance(item, ast.FunctionDef) and item.name == "example")
    literal = ast.get_source_segment(source, node.body[0])
    assert literal.startswith('"""\n')
    assert literal.endswith('\n    """')


def test_untyped_tool_forwards_named_arguments_with_chinese_documentation():
    """
    没有 Schema 时仍转发全部命名参数，生成文档遵守中文多行格式。
    """
    source = build_client(("example",))
    namespace = {}
    exec(compile(source, "<generated-client>", "exec"), namespace)
    namespace["_call"] = lambda name, arguments: (name, arguments)
    assert namespace["example"](value=None) == ("example", {"value": None})
    assert ":param kwargs:" in namespace["example"].__doc__
