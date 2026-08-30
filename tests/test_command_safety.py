"""Agent 命令安全校验测试。"""

from app.agent.tools.impl._command_safety import detect_dangerous_command


def test_compound_command_does_not_associate_jq_recursive_flag_with_rm():
    """复合命令中其他子命令的 -r 不应触发 rm 递归删除误报。"""
    command = "cd /tmp && rm -f qbcookiejar; printf '{}' | jq -r ."

    assert detect_dangerous_command(command) == ""


def test_recursive_rm_of_root_level_directory_remains_blocked():
    """递归删除根目录下一级目录仍需显式确认。"""
    assert detect_dangerous_command("cd /tmp && rm -rf /tmp")
