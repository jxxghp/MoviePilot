from pydantic import model_validator

from app.schemas.types import MediaSource


class OptionalMediaIdentityMixin:
    """为可选媒体身份模型统一校验内置或插件来源与原生 ID 的成对约束。"""

    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy_media_source(cls, value: object) -> object:
        """兼容媒体身份重构前的旧缓存/插件数据：将旧 ``source`` 键迁移为 ``media_source``。

        旧版本推荐等缓存以 ``source`` 键序列化媒体来源，与 ``media_id`` 成对出现；
        仅当缺失新键、旧值可解析为规范枚举时才迁移，避免误判其他模型同名字段。
        """
        if (
            isinstance(value, dict)
            and "source" in value
            and "media_source" not in value
            and "media_id" in value
            and value.get("source")
        ):
            try:
                MediaSource(value["source"])
            except ValueError:
                return value
            normalized = dict(value)
            normalized["media_source"] = value["source"]
            return normalized
        return value

    @model_validator(mode="after")
    def _validate_optional_media_identity(self):
        """规范化 ID，并拒绝显式半对、空白或零值身份。"""
        source_provided = "media_source" in self.model_fields_set
        id_provided = "media_id" in self.model_fields_set
        if source_provided != id_provided:
            raise ValueError("media_source 和 media_id 必须同时提供")
        normalized_id = (
            str(self.media_id).strip()
            if self.media_id is not None
            else None
        )
        if bool(self.media_source) != bool(normalized_id):
            raise ValueError("media_source 和 media_id 必须同时提供")
        if normalized_id == "0":
            raise ValueError("media_id 不能为 0")
        # 校验器内部的规范化不能伪装成请求显式提交字段，否则 PATCH 会误清空存量身份。
        object.__setattr__(self, "media_id", normalized_id)
        return self


class RequiredMediaIdentityMixin:
    """为必填媒体身份模型统一校验内置或插件来源与原生 ID。"""

    @model_validator(mode="after")
    def _validate_required_media_identity(self):
        """去除 ID 两端空白，并拒绝空白或零值身份。"""
        normalized_id = str(self.media_id).strip()
        if not normalized_id or normalized_id == "0":
            raise ValueError("media_id 必须是非零的来源原生 ID")
        self.media_id = normalized_id
        return self
