"""Application 重复逻辑收敛后的共享纯函数回归测试。"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.application.download.validation import validate_torrent_hash
from app.application.server.payload import build_subscribe_payload
from app.domain.classification.conditions import condition_field_ids
from app.schemas.category import ClassificationCondition, ClassificationConditionGroup

PROJECT_ROOT = Path(__file__).parents[1]


def test_application_refactor_modules_follow_single_word_leaf_names() -> None:
    """本轮新增应用模块使用单词文件名，并按同一模块进入子目录。"""
    application_root = PROJECT_ROOT / "app" / "application"
    refactored_files = [
        application_root / "download" / "validation.py",
        application_root / "history" / "__init__.py",
        application_root / "history" / "contracts.py",
        application_root / "history" / "mutation.py",
        application_root / "history" / "retry.py",
        application_root / "messaging" / "channel" / "__init__.py",
        application_root / "messaging" / "channel" / "admin.py",
        application_root / "messaging" / "interaction" / "__init__.py",
        application_root / "messaging" / "interaction" / "agent.py",
        application_root / "messaging" / "webagent" / "__init__.py",
        application_root / "messaging" / "webagent" / "events.py",
        application_root / "messaging" / "webagent" / "stream.py",
        application_root / "server" / "payload.py",
        application_root / "transfer" / "jobs.py",
        application_root / "transfer" / "models.py",
        application_root / "transfer" / "notifications.py",
        application_root / "transfer" / "projection.py",
    ]
    assert all(path.is_file() for path in refactored_files)
    assert all(path.stem == "__init__" or "_" not in path.stem for path in refactored_files)

    obsolete_files = [
        application_root / "history.py",
        application_root / "history_contracts.py",
        application_root / "history_retry.py",
        application_root / "historymutation.py",
        application_root / "messaging" / "agent_interaction.py",
        application_root / "messaging" / "channel_admin.py",
        application_root / "messaging" / "web_agent_events.py",
        application_root / "messaging" / "webagentstream.py",
    ]
    assert all(not path.exists() for path in obsolete_files)


def test_condition_field_ids_preserves_nested_declaration_order() -> None:
    """条件树公共遍历必须保留 all/any/not 的声明顺序。"""
    node = ClassificationConditionGroup(
        all=[
            ClassificationCondition(field="media.title", operator="exists"),
            ClassificationConditionGroup(
                any=[
                    ClassificationCondition(field="media.year", operator="exists"),
                    ClassificationConditionGroup(
                        not_=ClassificationCondition(
                            field="media.adult",
                            operator="exists",
                        )
                    ),
                ]
            ),
        ]
    )

    assert condition_field_ids(node) == (
        "media.title",
        "media.year",
        "media.adult",
    )


@pytest.mark.parametrize("value", ["", "a" * 39, "g" * 40, "a" * 41])
def test_validate_torrent_hash_rejects_non_v1_hash(value: str) -> None:
    """下载应用服务必须拒绝长度或字符集不符合 v1 Hash 的输入。"""
    with pytest.raises(ValueError, match="hash 格式无效"):
        validate_torrent_hash(value)


def test_build_subscribe_payload_filters_fields_and_requires_identity() -> None:
    """中心服务公共载荷投影只保留白名单字段并要求媒体身份完整。"""
    payload = build_subscribe_payload(
        {
            "name": "Demo",
            "media_source": "tmdb",
            "media_id": "123",
            "private": "hidden",
        },
        {"name", "media_source", "media_id"},
    )

    assert payload == {
        "name": "Demo",
        "media_source": "themoviedb",
        "media_id": "123",
    }
    assert build_subscribe_payload(SimpleNamespace(), {"name"}) is None
