from types import SimpleNamespace

from app.agent.prompt import prompt_manager
from app.agent.prompt.transfer_redo import build_batch_manual_redo_prompt


def test_batch_manual_redo_prompt_requires_plain_text_result():
    """批量 AI 重新整理提示词应要求最终回复只输出纯文本描述。"""
    history = SimpleNamespace(
        id=7,
        src_fileitem={"path": "/downloads/a.mkv"},
        src="",
        seasons="",
        episodes="",
        status=False,
        title="示例",
        type="电影",
        category="电影",
        year="2024",
        src_storage="local",
        dest="/media/a.mkv",
        dest_storage="local",
        dest_fileitem=None,
        mode="copy",
        tmdbid=123,
        doubanid=None,
        errmsg="识别失败",
    )

    prompt = build_batch_manual_redo_prompt([history])

    assert "Final response must be plain text only" in prompt
    assert "Do NOT include any title/header, bullet list" in prompt
    assert "Markdown formatting" in prompt


def test_batch_manual_redo_job_definition_contains_plain_text_rules():
    """批量 AI 重新整理任务定义应直接声明纯文本最终回复规则。"""
    definition = prompt_manager.load_system_tasks_definition()
    task_rules = definition.task_types["batch_manual_transfer_redo"].task_rules

    assert any("plain text only" in rule for rule in task_rules)
    assert any("Markdown formatting" in rule for rule in task_rules)
