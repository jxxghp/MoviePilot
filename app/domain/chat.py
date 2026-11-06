"""
会话展示快照的时间戳兼容与选择结果合并规则，不负责交互执行或持久化。
"""

from copy import deepcopy
from typing import Any


def normalize_display_timestamps(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    为历史展示消息补齐缺失时间戳，不修改原快照或已有时间。

    缺失值沿用前方最近的有效时间；开头缺失时使用后方首个有效时间。
    整个快照没有时间时使用 0 表示未知，避免每次读取都改变历史时间。

    :param messages: 可能含有缺失 createdAt 字段的历史展示快照
    :return: 时间戳字段完整的快照；已有非法值仍由响应模型报错
    """
    timestamp = next((message["createdAt"] for message in messages
                      if isinstance(message.get("createdAt"), (int, float))), 0)
    result = []
    for message in messages:
        if isinstance(message.get("createdAt"), (int, float)):
            timestamp = message["createdAt"]
        result.append({"createdAt": timestamp, **message})
    return result


def _choice_receipts(messages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """
    优先采用完整选择回执，旧记录只有已选卡片时从卡片恢复。

    :param messages: 当前会话快照，旧回执可能缺少时间戳
    :return: 以选择 ID 索引的回执，同时保留首次选择与对应时间
    """
    receipts: dict[str, dict[str, Any]] = {}
    normalized = normalize_display_timestamps(messages)
    for message in normalized:
        selected = message.get("choice_selection")
        if isinstance(selected, dict) and selected.get("choice_id"):
            receipts.setdefault(str(selected["choice_id"]), {
                "id": f"choice-selection-{selected['choice_id']}", "role": "user", "content": "",
                "createdAt": message["createdAt"], "choice_selection": selected,
            })
    for message in normalized:
        for choice in message.get("choices") or []:
            if isinstance(choice, dict) and choice.get("status") == "selected" and choice.get("id"):
                receipts.setdefault(str(choice["id"]), {
                    "id": f"choice-selection-{choice['id']}", "role": "user", "content": "",
                    "createdAt": message["createdAt"],
                    "choice_selection": {
                        "choice_id": str(choice["id"]),
                        "prompt": choice.get("prompt") or "",
                        "buttons": choice.get("buttons") or [],
                        "button_rows": choice.get("button_rows") or [],
                        **{key: choice.get(key) for key in (
                            "title", "selected_label", "selected_value", "selected_description",
                        )},
                    },
                })
    return receipts


def merge_choice_state(
    previous: list[dict[str, Any]], messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    保留首次已选结果及回执时间，旧客户端的 pending 快照不能降级已选卡片。

    :param previous: 当前持久化的权威快照
    :param messages: 本次准备保存的完整快照，不原地修改
    :return: 应用已选结果并按稳定 ID 合并选择回显后的快照
    """
    selection_receipts = _choice_receipts(previous)
    for choice_id, receipt in _choice_receipts(messages).items():
        selection_receipts.setdefault(choice_id, receipt)
    result: list[dict[str, Any]] = []
    receipts: dict[str, int] = {}
    received_choices: set[str] = set()
    for original in messages:
        message = deepcopy(original)
        selected = message.get("choice_selection")
        if isinstance(selected, dict) and selected.get("choice_id"):
            choice_id = str(selected["choice_id"])
            received_choices.add(choice_id)
            receipt = selection_receipts[choice_id]
            message["choice_selection"] = deepcopy(receipt["choice_selection"])
            message["createdAt"] = receipt["createdAt"]
        for choice in message.get("choices") or []:
            if not isinstance(choice, dict) or str(choice.get("id") or "") not in selection_receipts:
                continue
            selection = selection_receipts[str(choice["id"])]["choice_selection"]
            choice.update({"status": "selected", **{
                key: selection.get(key) for key in ("selected_label", "selected_value", "selected_description")
            }})
        key = str(message.get("id") or "") if selected else ""
        if key and key in receipts:
            result[receipts[key]] = message
        else:
            if key:
                receipts[key] = len(result)
            result.append(message)
    for choice_id, receipt in selection_receipts.items():
        if choice_id not in received_choices:
            result.append(deepcopy(receipt))
    return result
