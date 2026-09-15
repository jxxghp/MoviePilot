"""Application 重复逻辑收敛后的共享纯函数回归测试。"""

from types import SimpleNamespace

import pytest

from app.application.download.validation import validate_torrent_hash
from app.application.server.payload import build_subscribe_payload
from app.domain.classification.conditions import condition_field_ids
from app.schemas.category import ClassificationCondition, ClassificationConditionGroup


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
