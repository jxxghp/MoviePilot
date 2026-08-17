"""Agent 多文件补丁应用工具测试。"""

import asyncio
from unittest.mock import patch

from app.agent.tools.impl import apply_patch as apply_patch_module
from app.agent.tools.impl.apply_patch import ApplyPatchTool


def _make_admin_tool(tool_class=ApplyPatchTool):
    """创建带管理员上下文的补丁工具实例。"""
    tool = tool_class(session_id="session-1", user_id="admin")
    tool.set_agent_context({"is_admin": True})
    return tool


def test_apply_patch_supports_add_update_delete_in_one_call(tmp_path):
    """单个补丁应能同时新增、更新和删除多个文件。"""
    updated = tmp_path / "plugin.py"
    updated.write_text("enabled = False\nversion = 1\n", encoding="utf-8")
    deleted = tmp_path / "legacy.py"
    deleted.write_text("old code\n", encoding="utf-8")
    tool = _make_admin_tool()
    patch_text = (
        "*** Begin Patch\n"
        f"*** Add File: {tmp_path / 'new.py'}\n"
        "+print('hello')\n"
        f"*** Update File: {updated}\n"
        "@@\n"
        "-enabled = False\n"
        "+enabled = True\n"
        " version = 1\n"
        f"*** Delete File: {deleted}\n"
        "*** End Patch\n"
    )

    result = asyncio.run(tool.run(patch_text))

    assert "成功应用补丁（3 个文件）" in result
    assert (tmp_path / "new.py").read_text(encoding="utf-8") == "print('hello')\n"
    assert updated.read_text(encoding="utf-8") == "enabled = True\nversion = 1\n"
    assert not deleted.exists()


def test_apply_patch_applies_multiple_hunks_in_order(tmp_path):
    """同一文件的多个替换片段应按顺序定位并依次生效。"""
    file_path = tmp_path / "plugin.py"
    file_path.write_text("alpha\nbeta\ngamma\nbeta\n", encoding="utf-8")
    tool = _make_admin_tool()
    patch_text = (
        "*** Begin Patch\n"
        f"*** Update File: {file_path}\n"
        "@@\n"
        " alpha\n"
        "-beta\n"
        "+BETA\n"
        "@@\n"
        " gamma\n"
        "-beta\n"
        "+BETA2\n"
        "*** End Patch\n"
    )

    result = asyncio.run(tool.run(patch_text))

    assert "成功应用补丁（1 个文件）" in result
    assert file_path.read_text(encoding="utf-8") == "alpha\nBETA\ngamma\nBETA2\n"


def test_apply_patch_rejects_whole_patch_without_any_write_on_mismatch(tmp_path):
    """上下文不匹配时应整体拒绝，已校验通过的文件也不应被写入。"""
    first = tmp_path / "first.py"
    first.write_text("keep me\n", encoding="utf-8")
    second = tmp_path / "second.py"
    second.write_text("actual content\n", encoding="utf-8")
    tool = _make_admin_tool()
    patch_text = (
        "*** Begin Patch\n"
        f"*** Update File: {first}\n"
        "@@\n"
        "-keep me\n"
        "+changed\n"
        f"*** Update File: {second}\n"
        "@@\n"
        "-not present in file\n"
        "+changed\n"
        "*** End Patch\n"
    )

    result = asyncio.run(tool.run(patch_text))

    assert "不匹配" in result
    assert first.read_text(encoding="utf-8") == "keep me\n"
    assert second.read_text(encoding="utf-8") == "actual content\n"


def test_apply_patch_rejects_invalid_patch_structure(tmp_path):
    """缺失包裹标记、非法段落顺序和无锚点片段都应返回解析错误。"""
    file_path = tmp_path / "plugin.py"
    file_path.write_text("content\n", encoding="utf-8")
    tool = _make_admin_tool()

    no_begin = asyncio.run(tool.run(f"*** Update File: {file_path}\n*** End Patch\n"))
    no_end = asyncio.run(tool.run("*** Begin Patch\n*** End PatchX\n"))
    body_before_section = asyncio.run(
        tool.run("*** Begin Patch\n+stray line\n*** End Patch\n")
    )
    anchorless_hunk = asyncio.run(
        tool.run(
            "*** Begin Patch\n"
            f"*** Update File: {file_path}\n"
            "@@\n"
            "+only addition\n"
            "*** End Patch\n"
        )
    )

    assert "必须以 '*** Begin Patch' 开头" in no_begin
    assert "必须以 '*** End Patch' 结尾" in no_end
    assert "文件段落之前" in body_before_section
    assert "缺少上下文或删除行" in anchorless_hunk
    assert file_path.read_text(encoding="utf-8") == "content\n"


def test_apply_patch_rejects_add_existing_and_update_missing_file(tmp_path):
    """Add 已存在文件或 Update 不存在文件应报错并指引正确操作。"""
    existing = tmp_path / "existing.py"
    existing.write_text("here\n", encoding="utf-8")
    missing = tmp_path / "missing.py"
    tool = _make_admin_tool()

    add_result = asyncio.run(
        tool.run(
            "*** Begin Patch\n"
            f"*** Add File: {existing}\n"
            "+line\n"
            "*** End Patch\n"
        )
    )
    update_result = asyncio.run(
        tool.run(
            "*** Begin Patch\n"
            f"*** Update File: {missing}\n"
            "@@\n"
            "-old\n"
            "+new\n"
            "*** End Patch\n"
        )
    )

    assert "已存在" in add_result
    assert "Update File" in add_result
    assert "不存在" in update_result
    assert "Add File" in update_result
    assert existing.read_text(encoding="utf-8") == "here\n"
    assert not missing.exists()


def test_apply_patch_enforces_non_admin_path_boundary(tmp_path):
    """普通用户只能对 Agent 配置目录内的文件打补丁。"""
    file_path = tmp_path / "plugin.py"
    file_path.write_text("before\n", encoding="utf-8")
    tool = ApplyPatchTool(session_id="session-1", user_id="user")
    tool.set_agent_context({"is_admin": False})

    result = asyncio.run(
        tool.run(
            "*** Begin Patch\n"
            f"*** Update File: {file_path}\n"
            "@@\n"
            "-before\n"
            "+after\n"
            "*** End Patch\n"
        )
    )

    assert "Agent配置目录" in result
    assert file_path.read_text(encoding="utf-8") == "before\n"


def test_apply_patch_detects_version_conflict_during_write(tmp_path):
    """写入阶段检测到文件被并发修改时应拒绝覆盖。"""
    file_path = tmp_path / "plugin.py"
    file_path.write_text("before\n", encoding="utf-8")
    tool = _make_admin_tool()
    patch_text = (
        "*** Begin Patch\n"
        f"*** Update File: {file_path}\n"
        "@@\n"
        "-before\n"
        "+after\n"
        "*** End Patch\n"
    )
    original_write = apply_patch_module.atomic_write_text

    def _conflicting_write(path, content, expected_sha256=None):
        file_path.write_text("changed elsewhere\n", encoding="utf-8")
        original_write(path, content, expected_sha256)

    with patch.object(
        apply_patch_module, "atomic_write_text", _conflicting_write
    ):
        result = asyncio.run(tool.run(patch_text))

    assert "在应用补丁期间发生变化" in result
    assert file_path.read_text(encoding="utf-8") == "changed elsewhere\n"


def test_apply_patch_tool_message_counts_patch_files(tmp_path):
    """工具消息应汇总补丁涉及的文件数量。"""
    tool = _make_admin_tool()

    message = tool.get_tool_message(
        patch=(
            "*** Begin Patch\n"
            "*** Add File: a.py\n"
            "+x\n"
            "*** Update File: b.py\n"
            "@@\n"
            "-old\n"
            "+new\n"
            "*** Delete File: c.py\n"
            "*** End Patch\n"
        )
    )

    assert message == "应用补丁: 3 个文件"
