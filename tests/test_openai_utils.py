"""
验证 OpenAI 与 Anthropic 兼容接口的多模态消息转换。
"""

from app.api.protocol import (
    build_anthropic_messages,
    build_completion_payload,
    build_prompt,
    build_responses_input,
    build_session_id,
    extract_text_and_images,
)


def test_extract_text_and_images() -> None:
    """
    多模态消息按顺序提取文本并收集图片 URL。
    """
    text, images = extract_text_and_images(
        [
            {"type": "text", "text": "你好"},
            {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
            {"type": "text", "text": "世界"},
        ]
    )
    assert text == "你好\n世界"
    assert images == ["https://example.com/a.png"]


def test_extract_text_and_images_with_input_image_and_base64_image() -> None:
    """
    兼容输入图片与 Base64 图片两种载荷形式。
    """
    text, images = extract_text_and_images(
        [
            {"type": "input_text", "text": "看图"},
            {"type": "input_image", "image_url": "https://example.com/b.png"},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": "YWJj",
                },
            },
        ]
    )
    assert text == "看图"
    assert images == ["https://example.com/b.png", "data:image/png;base64,YWJj"]


def test_build_prompt_without_server_session_keeps_recent_history() -> None:
    """
    没有服务端会话时在提示词中保留近期历史。
    """
    prompt, images = build_prompt(
        [
            {"role": "system", "content": "回答简短"},
            {"role": "user", "content": "第一句"},
            {"role": "assistant", "content": "第一答"},
            {"role": "user", "content": "第二句"},
        ],
        use_server_session=False,
    )
    assert "系统要求：\n回答简短" in prompt
    assert "对话上下文：\nuser: 第一句\nassistant: 第一答" in prompt
    assert "当前用户消息：\n第二句" in prompt
    assert images == []


def test_build_prompt_with_server_session_ignores_history_block() -> None:
    """
    已有服务端会话时不重复附加历史消息块。
    """
    prompt, _ = build_prompt(
        [
            {"role": "user", "content": "历史问题"},
            {"role": "assistant", "content": "历史回答"},
            {"role": "user", "content": "当前问题"},
        ],
        use_server_session=True,
    )
    assert "对话上下文：" not in prompt
    assert "当前用户消息：\n当前问题" in prompt


def test_build_prompt_accepts_image_only_user_message() -> None:
    """
    只有图片的用户消息仍能构造有效提示词。
    """
    prompt, images = build_prompt(
        [
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}],
            }
        ],
        use_server_session=True,
    )
    assert "请结合图片内容回复" in prompt
    assert images == ["https://example.com/a.png"]


def test_build_session_id_is_stable() -> None:
    """
    相同用户和会话信息生成稳定会话标识。
    """
    session_id = build_session_id("user-1", "openai:")
    assert session_id.startswith("openai:")
    assert session_id == build_session_id("user-1", "openai:")
    assert session_id != build_session_id("user-2", "openai:")


def test_build_completion_payload() -> None:
    """
    补全接口输出包含预期模型、消息和用量字段。
    """
    payload = build_completion_payload("你好", "moviepilot-agent")
    assert payload["model"] == "moviepilot-agent"
    assert payload["choices"][0]["message"]["content"] == "你好"
    assert payload["choices"][0]["finish_reason"] == "stop"


def test_build_responses_input() -> None:
    """
    Responses 请求正确转换文本和图片输入。
    """
    messages = build_responses_input(
        [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "你好"}],
            }
        ],
        instructions="你要简短回答",
    )
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"


def test_build_anthropic_messages() -> None:
    """
    Anthropic 请求正确转换系统指令及用户多模态消息。
    """
    messages = build_anthropic_messages(
        system=[{"type": "text", "text": "你是助手"}],
        messages=[{"role": "user", "content": "你好"}],
    )
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
