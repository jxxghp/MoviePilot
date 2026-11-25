"""
验证批量历史重做的人工智能提示内容和参数边界。
"""

from types import SimpleNamespace

from app.agent.prompt import prompt_manager
from app.agent.prompt.transfer import build_batch_manual_redo_prompt


def test_batch_manual_redo_prompt_requires_plain_text_result():
    """
    批量 AI 重新整理提示词应要求最终回复只输出纯文本描述。
    """
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
        bangumiid=None,
        anilistid=None,
        media_source="themoviedb",
        media_id="123",
        errmsg="识别失败",
    )

    prompt = build_batch_manual_redo_prompt([history])

    assert "Final response must be plain text only" in prompt
    assert "Do NOT include any title/header, bullet list" in prompt
    assert "Markdown formatting" in prompt
    assert "configured output language" in prompt


def test_batch_manual_redo_job_definition_contains_plain_text_rules():
    """
    批量 AI 重新整理任务定义应直接声明纯文本最终回复规则。
    """
    definition = prompt_manager.load_system_tasks_definition()
    task_rules = definition.task_types["batch_manual_transfer_redo"].task_rules

    assert any("plain text only" in rule for rule in task_rules)
    assert any("Markdown formatting" in rule for rule in task_rules)


def test_redo_tasks_preserve_durable_evidence_and_use_safe_replan():
    """
    系统任务必须通过受保护的识别修正入口恢复持久化失败，不诱导删除或普通重试。
    """
    definition = prompt_manager.load_system_tasks_definition()

    for task_name in (
        "transfer_failed_retry",
        "batch_transfer_failed_retry",
        "manual_transfer_redo",
        "batch_manual_transfer_redo",
    ):
        task = definition.task_types[task_name]
        instructions = [*task.steps, *task.task_rules]
        assert any("transfer.history.replan" in item for item in instructions)
        assert any("evidence" in item and ("Never" in item or "never" in item) for item in instructions)
        assert not any("Durable records are not deleted: the API submits" in item for item in instructions)
