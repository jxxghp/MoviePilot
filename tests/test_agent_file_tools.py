"""Agent 本地文件搜索与安全编辑工具测试。"""

import asyncio
import hashlib
import json

from app.agent.tools.impl.edit_file import EditFileTool
from app.agent.tools.impl.read_file import ReadFileTool
from app.agent.tools.impl.write_file import WriteFileTool


def _make_admin_tool(tool_class):
    """创建带管理员上下文的文件工具实例。"""
    tool = tool_class(session_id="session-1", user_id="admin")
    tool.set_agent_context({"is_admin": True})
    return tool


def test_edit_file_rejects_ambiguous_match_by_default(tmp_path):
    """精确编辑默认应拒绝多处匹配，避免静默批量修改代码。"""
    file_path = tmp_path / "plugin.py"
    file_path.write_text("enabled = False\nenabled = False\n", encoding="utf-8")
    tool = _make_admin_tool(EditFileTool)

    result = asyncio.run(
        tool.run(str(file_path), "enabled = False", "enabled = True")
    )

    assert "匹配到 2 处" in result
    assert "replace_all=true" in result
    assert file_path.read_text(encoding="utf-8") == (
        "enabled = False\nenabled = False\n"
    )


def test_edit_file_replace_all_requires_explicit_flag(tmp_path):
    """显式开启 replace_all 后才应替换全部精确匹配。"""
    file_path = tmp_path / "plugin.py"
    file_path.write_text("old\nold\n", encoding="utf-8")
    tool = _make_admin_tool(EditFileTool)

    result = asyncio.run(
        tool.run(str(file_path), "old", "new", replace_all=True)
    )

    assert "替换了 2 处" in result
    assert file_path.read_text(encoding="utf-8") == "new\nnew\n"


def test_edit_file_rejects_empty_match_and_missing_file(tmp_path):
    """编辑工具不应再通过空匹配隐式创建文件。"""
    file_path = tmp_path / "missing.py"
    tool = _make_admin_tool(EditFileTool)

    empty_result = asyncio.run(tool.run(str(file_path), "", "content"))
    missing_result = asyncio.run(tool.run(str(file_path), "old", "new"))

    assert "old_text 不能为空" in empty_result
    assert "不存在" in missing_result
    assert "write_file" in missing_result
    assert not file_path.exists()


def test_edit_file_rejects_stale_sha256(tmp_path):
    """文件在读取后变化时，哈希保护应拒绝基于旧版本编辑。"""
    file_path = tmp_path / "plugin.py"
    file_path.write_text("before", encoding="utf-8")
    old_sha256 = hashlib.sha256(b"before").hexdigest()
    file_path.write_text("changed elsewhere", encoding="utf-8")
    tool = _make_admin_tool(EditFileTool)

    result = asyncio.run(
        tool.run(
            str(file_path),
            "changed elsewhere",
            "agent change",
            expected_sha256=old_sha256,
        )
    )

    assert "已在读取后发生变化" in result
    assert file_path.read_text(encoding="utf-8") == "changed elsewhere"


def test_write_file_protects_existing_file_and_supports_guarded_overwrite(tmp_path):
    """完整写入应默认保护已有文件，并允许带版本校验的显式覆盖。"""
    file_path = tmp_path / "plugin.py"
    file_path.write_text("before", encoding="utf-8")
    expected_sha256 = hashlib.sha256(b"before").hexdigest()
    tool = _make_admin_tool(WriteFileTool)

    refused_result = asyncio.run(tool.run(str(file_path), "unexpected"))
    written_result = asyncio.run(
        tool.run(
            str(file_path),
            "after",
            overwrite=True,
            expected_sha256=expected_sha256,
        )
    )
    stale_result = asyncio.run(
        tool.run(
            str(file_path),
            "stale write",
            overwrite=True,
            expected_sha256=expected_sha256,
        )
    )

    assert "拒绝完整覆盖" in refused_result
    assert "成功写入文件" in written_result
    assert "sha256=" in written_result
    assert "已在读取后发生变化" in stale_result
    assert file_path.read_text(encoding="utf-8") == "after"


def test_read_file_can_return_sha256_metadata(tmp_path):
    """读取工具应能返回供后续冲突检查使用的文件哈希。"""
    file_path = tmp_path / "plugin.py"
    file_path.write_text("插件内容", encoding="utf-8")
    tool = _make_admin_tool(ReadFileTool)

    result = asyncio.run(tool.run(str(file_path), include_metadata=True))
    payload = json.loads(result)

    assert payload["content"] == "插件内容"
    assert payload["size_bytes"] == len("插件内容".encode("utf-8"))
    assert payload["sha256"] == hashlib.sha256(
        "插件内容".encode("utf-8")
    ).hexdigest()
    assert payload["truncated"] is False
