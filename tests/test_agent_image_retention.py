"""工具截图仅供当前运行观察，持久化与日志不得复制像素或伪造用户消息。"""

import base64
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, messages_from_dict, messages_to_dict

from app.agent.memory import MemoryManager
from app.agent.orchestrator import MoviePilotAgent
from app.agent.policy.sanitizer import sanitize_for_host, summarize_error, summarize_result
from app.agent.tools.result import (
    EXECUTION_OUTCOME_KEY,
    TOOL_OBSERVATION_MARKER,
    inspect_tool_result,
    is_image_content_block,
    messages_for_persistence,
    sanitize_tool_image_message,
)

_TOOL_PIXELS = base64.b64encode(b"private-browser-pixels" * 2000).decode("ascii")
_USER_PIXELS = base64.b64encode(b"original-user-attachment").decode("ascii")


def _image(kind="image_url", pixels=_TOOL_PIXELS):
    """构造三种标准类别，只测试传递和去留，不把任意字节伪称有效 JPEG。"""
    url = f"data:image/jpeg;base64,{pixels}"
    if kind == "image":
        return {"type": "image", "base64": pixels, "mime_type": "image/jpeg", "width": 1280, "height": 720}
    if kind == "input_image":
        return {"type": "input_image", "image_url": url, "detail": "low"}
    return {"type": "image_url", "image_url": {"url": url, "detail": "low"}}


def _tool_message(kind="image_url", outcome="succeeded"):
    """保留截图来源、文本、执行状态和可能重复带图的 artifact。"""
    return ToolMessage(
        id="source-message", name="browse_webpage", tool_call_id="screenshot-call",
        content=[{"type": "text", "text": "页面来源 https://page.invalid；标题：确认表单"}, _image(kind)],
        status="error" if outcome in {"failed", "unknown"} else "success",
        artifact={"screenshot": _image(kind)},
        additional_kwargs={EXECUTION_OUTCOME_KEY: outcome, "source": {"tab": "main", "title": "确认表单"}},
    )


@pytest.mark.parametrize("kind", ["image", "image_url", "input_image"])
def test_tool_image_history_is_a_deep_copy_with_explicit_expiry(kind):
    """图像去留不改变原图、文本、来源、调用归属和结果状态。"""
    original = _tool_message(kind)
    before = deepcopy(original.model_dump())
    sanitized = sanitize_tool_image_message(original)
    assert sanitized is not original
    assert original.model_dump() == before
    assert not any(is_image_content_block(block) for block in sanitized.content)
    assert "历史图像未保留" in str(sanitized.content)
    assert sanitized.content[0] == original.content[0]
    assert sanitized.additional_kwargs == original.additional_kwargs
    assert sanitized.id == original.id and sanitized.name == original.name
    assert sanitized.tool_call_id == original.tool_call_id and sanitized.status == original.status
    assert sanitized.artifact is None
    assert _TOOL_PIXELS[:80] not in json.dumps(sanitized.model_dump())
    sanitized.content[0]["text"] = "仅修改持久化副本"
    sanitized.additional_kwargs["source"]["title"] = "副本来源"
    assert original.model_dump() == before


@pytest.mark.parametrize("outcome", ["succeeded", "failed", "unknown", "pending"])
def test_image_removal_does_not_change_execution_outcome(outcome):
    """图像清理只改变像素保留，不把未知、失败或尚未完成的工具写成成功。"""
    original = _tool_message(outcome=outcome)
    assert inspect_tool_result(sanitize_tool_image_message(original)) == inspect_tool_result(original)


def test_image_artifact_is_discarded_before_attempting_deep_copy():
    """不持久化的运行资源不应阻断安全历史副本的生成。"""
    class RuntimeArtifact:
        """模拟不能被复制的浏览器运行资源。"""

        def __deepcopy__(self, _memo):
            """持久化应先去掉该资源，不能为了丢弃而先触发其复制。"""
            raise AssertionError("运行资源不可复制")

    original = _tool_message()
    original.artifact = RuntimeArtifact()
    sanitized = sanitize_tool_image_message(original)
    assert sanitized.artifact is None
    assert original.artifact is not None
    assert "历史图像未保留" in str(sanitized.content)


@pytest.mark.parametrize("message", [
    HumanMessage(content=[_image(pixels=_USER_PIXELS)]),
    AIMessage(content=[_image(pixels=_USER_PIXELS)]),
    ToolMessage(content="普通文本结果", name="ordinary", tool_call_id="plain", artifact={"rows": [1, 2]}),
])
def test_real_user_attachments_and_non_image_tool_data_are_unchanged(message):
    """用户附件、模型原有输出和普通工具 artifact 不属于截图清理范围。"""
    original = deepcopy(message.model_dump())
    assert sanitize_tool_image_message(message) is message
    assert message.model_dump() == original


@pytest.mark.parametrize("marker", [None, False, 1, "true", "True"])
def test_only_literal_host_true_marker_removes_temporary_observation(marker):
    """值相似不代表宿主标记，普通用户内容及未知 marker 必须保留。"""
    user = HumanMessage(content=[_image(pixels=_USER_PIXELS)], additional_kwargs={TOOL_OBSERVATION_MARKER: marker})
    assert messages_for_persistence([user]) == [user]


def test_temporary_human_observation_is_filtered_without_losing_tool_or_user():
    """请求投影的临时 Human 图片不持久化，工具回执和真实用户输入仍保持顺序。"""
    user = HumanMessage(content=json.dumps({TOOL_OBSERVATION_MARKER: True}))
    tool = _tool_message()
    temporary = HumanMessage(content=[_image()], additional_kwargs={TOOL_OBSERVATION_MARKER: True})
    assistant = AIMessage(content="已根据截图检查", additional_kwargs={TOOL_OBSERVATION_MARKER: True})
    persisted = messages_for_persistence([user, tool, temporary, assistant])
    assert len(persisted) == 3
    assert persisted[0] is user and persisted[-1] is assistant
    assert isinstance(persisted[1], ToolMessage)
    assert persisted[1].tool_call_id == tool.tool_call_id
    assert "历史图像未保留" in str(persisted[1].content)
    assert is_image_content_block(tool.content[1])


@pytest.mark.parametrize("value", [
    "image", ["image"], {"type": "video", "base64": "ordinary"},
    {"type": "custom", "image_url": "unsupported"}, {"image": "untyped"},
    {"type": ["image"]}, {"type": "IMAGE"},
])
def test_unknown_representations_do_not_become_image_content(value):
    """只识别精确标准类别，普通字段名称不能自行启用模型视觉。"""
    assert is_image_content_block(value) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_memory_keeps_current_images_but_persists_text_only_tool_history(asynchronous):
    """真实 MemoryManager 的同步和异步写入都只改 DB 副本，运行中缓存可继续观察截图。"""
    chat = SimpleNamespace(save_agent_messages=Mock())
    persistence = SimpleNamespace(async_save_agent_messages=AsyncMock())
    memory = MemoryManager(chat=chat, persistence=persistence)
    user = HumanMessage(content=[{"type": "text", "text": "用户附件"}, _image(pixels=_USER_PIXELS)])
    tool = _tool_message()
    temporary = HumanMessage(content=[_image()], additional_kwargs={TOOL_OBSERVATION_MARKER: True})
    messages = [user, tool, temporary]
    original = messages_to_dict(messages)
    if asynchronous:
        await memory.async_save_agent_messages(session_id="image-session", user_id="owner", messages=messages)
        serialized = persistence.async_save_agent_messages.call_args.kwargs["messages"]
    else:
        memory.save_agent_messages(session_id="image-session", user_id="owner", messages=messages)
        serialized = chat.save_agent_messages.call_args.kwargs["messages"]
    assert messages_to_dict(messages) == original
    cached = memory.get_memory("image-session", "owner")
    assert len(cached.messages) == 3 and is_image_content_block(cached.messages[1].content[1])
    restored = messages_from_dict(serialized)
    assert len(restored) == 2
    assert restored[0].content == user.content
    assert _USER_PIXELS in json.dumps(serialized)
    assert _TOOL_PIXELS[:80] not in json.dumps(serialized)
    assert "历史图像未保留" in str(restored[1].content)
    assert restored[1].artifact is None


def test_reloading_persisted_images_applies_same_retention_contract():
    """历史记录即使曾保存截图或临时投影，恢复时也不能重新成为视觉输入。"""
    user = HumanMessage(content=[_image(pixels=_USER_PIXELS)])
    tool = _tool_message()
    temporary = HumanMessage(content=[_image()], additional_kwargs={TOOL_OBSERVATION_MARKER: True})
    serialized = messages_to_dict([user, tool, temporary])
    original = deepcopy(serialized)
    chat = SimpleNamespace(get_sync=Mock(return_value=SimpleNamespace(agent_messages=serialized)))
    memory = MemoryManager(chat=chat)
    restored = memory.get_agent_messages(session_id="history", user_id="owner")
    assert len(restored) == 2
    assert restored[0].content == user.content
    assert "历史图像未保留" in str(restored[1].content)
    assert serialized == original
    assert memory.get_memory("history", "owner").messages == restored


def test_interruption_recovery_uses_placeholder_instead_of_broken_data_url():
    """原有通用脱敏长度上限不能把截图截成仍声明为图像的坏 data URL。"""
    original = _tool_message(outcome="unknown")
    sanitized = MoviePilotAgent._sanitize_recovery_message(original)
    assert not any(is_image_content_block(block) for block in sanitized.content)
    assert "历史图像未保留" in str(sanitized.content)
    assert _TOOL_PIXELS[:80] not in str(sanitized.model_dump())
    assert is_image_content_block(original.content[1])
    assert sanitized.tool_call_id == original.tool_call_id


@pytest.mark.parametrize("kind", ["image", "image_url", "input_image"])
def test_interruption_recovery_preserves_user_images_and_sanitizes_only_text(kind):
    """同一恢复消息中的用户附件保留完整字节，文字凭据仍清理，不能生成坏图块。"""
    original = HumanMessage(content=[{"type": "text", "text": "password=private-credential"},
                                     _image(kind, pixels=_USER_PIXELS)])
    before = messages_to_dict([original])
    recovered = MoviePilotAgent._sanitize_recovery_message(original)
    portable = MoviePilotAgent._portable_recovery_messages([recovered])
    assert portable[0].content[1] == original.content[1]
    assert _USER_PIXELS in json.dumps(messages_to_dict(portable))
    assert "private-credential" not in json.dumps(messages_to_dict(portable))
    recovered.content[1]["test_copy"] = True
    assert messages_to_dict([original]) == before


@pytest.mark.parametrize("kind", ["image", "image_url", "input_image"])
def test_log_sanitizer_removes_structured_image_pixels_but_keeps_metadata(kind):
    """真实图像块的像素必须完全移除，URL/标题文本和尺寸等元信息仍可诊断。"""
    payload = {"url": "https://page.invalid", "title": "确认表单", "result": [_image(kind)]}
    original = deepcopy(payload)
    sanitized = sanitize_for_host(payload)
    assert payload == original
    assert sanitized["url"] == payload["url"] and sanitized["title"] == payload["title"]
    assert sanitized["result"][0]["type"] == kind
    assert _TOOL_PIXELS[:80] not in json.dumps(sanitized)
    if kind == "image_url":
        assert sanitized["result"][0]["image_url"]["detail"] == "low"
    if kind == "image":
        assert sanitized["result"][0]["mime_type"] == "image/jpeg"
        assert sanitized["result"][0]["width"] == 1280


def test_log_sanitizer_redacts_nested_image_source_and_artifact():
    """嵌套 source、ToolMessage 和 artifact 的重复截图也不能绕过日志边界。"""
    message = _tool_message("image")
    message.content[1] = {"type": "image", "source": {
        "type": "base64", "media_type": "image/jpeg", "data": _TOOL_PIXELS,
    }}
    sanitized = sanitize_for_host(message)
    assert _TOOL_PIXELS[:80] not in json.dumps(sanitized)
    assert sanitized["content"][1]["source"]["media_type"] == "image/jpeg"
    assert "确认表单" in str(sanitized)


@pytest.mark.parametrize("large", [False, True])
def test_browser_json_logs_never_keep_base64_prefix_after_text_truncation(large):
    """超过 16KiB 的浏览器 JSON 走文本截断路径时仍应完整遮蔽像素前缀。"""
    pixels = _TOOL_PIXELS if large else _TOOL_PIXELS[:120]
    payload = json.dumps({"url": "https://page.invalid", "title": "截图标题", "screenshot_base64": pixels,
                          "format": "jpeg", "api_key": "private-credential"}, ensure_ascii=False)
    assert (len(payload) > 16 * 1024) is large
    for output in (sanitize_for_host(payload), summarize_result(payload), summarize_error(ValueError(payload))):
        assert _TOOL_PIXELS[:80] not in str(output)
        assert "private-credential" not in str(output)
        assert "截图标题" in str(output)


@pytest.mark.parametrize("large", [False, True])
def test_plain_text_data_url_is_removed_before_log_preview(large):
    """数据 URL 无需 JSON 包装也必须去掉像素，不能只输出一段截断的 base64。"""
    pixels = _TOOL_PIXELS if large else _TOOL_PIXELS[:120]
    output = sanitize_for_host(f"截图地址 data:image/jpeg;base64,{pixels} 后续说明")
    assert _TOOL_PIXELS[:80] not in output
    assert "截图地址" in output


def test_non_image_data_and_existing_credential_redaction_remain_distinct():
    """普通 base64 数据不自动当作图片，现有凭据字段仍按原规则清理。"""
    payload = {"type": "ordinary", "base64": "non-image-encoded-data", "width": 12, "api_key": "secret"}
    assert sanitize_for_host(payload) == {**payload, "api_key": "***"}
