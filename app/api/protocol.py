"""
把 OpenAI 与 Anthropic 消息转换为宿主 Agent 输入，并组装兼容响应。
"""

import hashlib
import time
import uuid
from typing import Any, Dict, List, Tuple


def _get_message_field(message: Any, field: str, default: Any = None) -> Any:
    if isinstance(message, dict):
        return message.get(field, default)
    return getattr(message, field, default)


def extract_text_and_images(content: Any) -> Tuple[str, List[str]]:
    """
    从兼容协议的消息内容中提取文本和图片地址。

    支持文本、分段文本、图片 URL 及 Anthropic Base64 图片；忽略不支持的内容块。

    :param content: 字符串、内容块列表或 None
    :return: 去除首尾空白并按换行拼接的文本，以及按出现顺序保留的图片地址；空内容返回空值
    """
    if content is None:
        return "", []
    if isinstance(content, str):
        return content.strip(), []

    text_parts: List[str] = []
    image_urls: List[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, str):
                normalized = item.strip()
                if normalized:
                    text_parts.append(normalized)
                continue
            if not isinstance(item, dict):
                continue
            item_type = (item.get("type") or "").lower()
            if item_type == "text":
                text = item.get("text")
                if text and str(text).strip():
                    text_parts.append(str(text).strip())
            elif item_type == "input_text":
                text = item.get("text")
                if text and str(text).strip():
                    text_parts.append(str(text).strip())
            elif item_type == "image_url":
                image_url = item.get("image_url")
                url = image_url.get("url") if isinstance(image_url, dict) else image_url
                if url and str(url).strip():
                    image_urls.append(str(url).strip())
            elif item_type == "input_image":
                url = item.get("image_url")
                if url and str(url).strip():
                    image_urls.append(str(url).strip())
            elif item_type == "image":
                source = item.get("source") or {}
                if isinstance(source, dict) and source.get("type") == "base64":
                    data = source.get("data")
                    media_type = source.get("media_type") or "image/png"
                    if data and str(data).strip():
                        image_urls.append(
                            f"data:{media_type};base64,{str(data).strip()}"
                        )
    return "\n".join(text_parts).strip(), image_urls


def build_prompt(
    messages: List[Any], use_server_session: bool
) -> Tuple[str, List[str]]:
    """
    合并系统要求、当前用户消息和必要的历史文本。

    :param messages: 按会话顺序排列的消息，支持字典和属性对象；developer 按 system 处理
    :param use_server_session: 是否由服务器保存会话；为 True 时不重复拼入历史文本
    :return: 合成提示词及最后一条有效用户消息的图片；无服务器会话时最多附加十条历史文本
    :raises ValueError: 没有包含文本或图片的用户消息
    """
    system_texts: List[str] = []
    transcript: List[str] = []
    latest_user_text = ""
    latest_user_images: List[str] = []

    for message in messages:
        role = str(_get_message_field(message, "role", "user") or "user").lower()
        if role == "developer":
            role = "system"
        text, images = extract_text_and_images(_get_message_field(message, "content"))
        if role == "system":
            if text:
                system_texts.append(text)
            continue
        if role == "user":
            if text or images:
                latest_user_text = text
                latest_user_images = images
            if text:
                transcript.append(f"user: {text}")
            continue
        if text:
            transcript.append(f"{role}: {text}")

    if not latest_user_text and not latest_user_images:
        raise ValueError("No usable user message found in messages.")

    prompt_parts: List[str] = []
    if system_texts:
        prompt_parts.append("系统要求：\n" + "\n\n".join(system_texts))

    if not use_server_session and transcript:
        history = transcript[:-1] if transcript[-1].startswith("user: ") else transcript
        if history:
            prompt_parts.append("对话上下文：\n" + "\n".join(history[-10:]))

    if latest_user_text:
        prompt_parts.append("当前用户消息：\n" + latest_user_text)
    else:
        prompt_parts.append("当前用户消息：\n请结合图片内容回复。")

    return "\n\n".join(
        part for part in prompt_parts if part
    ).strip(), latest_user_images


def build_session_id(session_key: str, prefix: str) -> str:
    """
    根据外部会话键构造固定长度的宿主会话标识。

    :param session_key: 外部客户端提供的会话键
    :param prefix: 调用协议或会话用途的命名前缀
    :return: 前缀加 SHA-256 摘要的前 32 个十六进制字符
    """
    digest = hashlib.sha256(session_key.encode("utf-8")).hexdigest()
    return f"{prefix}{digest[:32]}"


def build_completion_payload(content: str, model_id: str) -> Dict[str, Any]:
    """
    将最终文本包装为 OpenAI 非流式聊天完成响应。

    :param content: 助手生成的完整文本
    :param model_id: 对外声明的模型标识
    :return: 单个 stop 选择项；时间取当前 Unix 秒数，未统计的令牌用量均为 0
    """
    created = int(time.time())
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": created,
        "model": model_id,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


def build_responses_input(
    input_data: Any, instructions: str | None = None
) -> List[Dict[str, Any]]:
    """
    把 Responses 输入转换为统一消息列表。

    :param input_data: 字符串、消息列表或带 role/content 的单个消息映射；不支持的条目忽略
    :param instructions: 可选的系统要求；非空时插入列表首项
    :return: 规范化消息列表，文本内容会去除首尾空白，消息块内容保持原值
    """
    messages: List[Dict[str, Any]] = []
    if instructions and str(instructions).strip():
        messages.append({"role": "system", "content": str(instructions).strip()})

    if isinstance(input_data, str):
        normalized = input_data.strip()
        if normalized:
            messages.append({"role": "user", "content": normalized})
        return messages

    if isinstance(input_data, list):
        for item in input_data:
            if not isinstance(item, dict):
                continue
            item_type = (item.get("type") or "").lower()
            if item_type == "message":
                role = item.get("role") or "user"
                content = item.get("content")
                messages.append({"role": role, "content": content})
            elif item.get("role") and "content" in item:
                messages.append(
                    {"role": item.get("role"), "content": item.get("content")}
                )
        return messages

    if (
        isinstance(input_data, dict)
        and input_data.get("role")
        and "content" in input_data
    ):
        messages.append(
            {"role": input_data.get("role"), "content": input_data.get("content")}
        )

    return messages


def build_anthropic_messages(system: Any, messages: List[Any]) -> List[Dict[str, Any]]:
    """
    把 Anthropic 系统要求和消息转换为统一消息列表。

    :param system: 系统内容，仅保留可提取的文本
    :param messages: 支持字典和属性对象的有序消息列表
    :return: 可选系统消息加原顺序消息；消息内容不在本步骤重新编码
    """
    normalized: List[Dict[str, Any]] = []
    system_text, _ = extract_text_and_images(system)
    if system_text:
        normalized.append({"role": "system", "content": system_text})

    for message in messages:
        role = _get_message_field(message, "role", "user")
        content = _get_message_field(message, "content")
        normalized.append({"role": role, "content": content})
    return normalized
