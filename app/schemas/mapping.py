"""
非 TMDB 来源到 TMDB 的共享关系协议，不包含完整媒体详情。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.media import normalize_media_source
from app.schemas.types import MediaSource


class MediaMappingQuery(BaseModel):
    """
    用原来源身份、可选类型与季号定位已有关系。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    media_source: MediaSource = Field(description="非 TMDB 的影视来源")
    media_id: str = Field(min_length=1, max_length=256, description="来源原生媒体 ID")
    type: Literal["movie", "tv"] | None = Field(default=None, description="原来源影视类型")
    season: int | None = Field(default=None, ge=0, le=10000, description="原来源季号；未指定为 null")

    @field_validator("media_source", mode="before")
    @classmethod
    def normalize_source(cls, value: object) -> MediaSource:
        """
        规范来源别名，并拒绝反向 TMDB 转换。

        :param value: 内置来源或插件来源标识
        :return: 非 TMDB 的有效来源
        """
        source = normalize_media_source(value) if isinstance(value, (str, MediaSource)) else None
        if source is None or source == MediaSource.TMDB:
            raise ValueError("仅支持非 TMDB 来源到 TMDB 的关系")
        return source

    @field_validator("media_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        """
        拒绝零哨兵并保留原生 ID 大小写。

        :param value: 去除首尾空白后的媒体标识
        :return: 有效的原生标识
        """
        if value == "0":
            raise ValueError("媒体 ID 无效")
        return value


class MediaMapping(MediaMappingQuery):
    """
    记录单向转换的原身份及对应的 TMDB 类型、ID、季号。
    """

    type: Literal["movie", "tv"] = Field(description="原来源影视类型")
    tmdb_id: int = Field(gt=0, le=2147483647, description="TMDB 原生 ID")
    tmdb_type: Literal["movie", "tv"] = Field(description="TMDB 电影或剧集类型")
    tmdb_season: int | None = Field(default=None, ge=0, le=10000, description="TMDB 季号，电影或未知为 null")

    @model_validator(mode="after")
    def validate_seasons(self) -> "MediaMapping":
        """
        拒绝电影携带季号的无效共享结果。

        :return: 类型与季号相容的关系
        """
        if self.type == "movie" and self.season is not None:
            raise ValueError("电影来源不能指定季号")
        if self.tmdb_type == "movie" and self.tmdb_season is not None:
            raise ValueError("TMDB 电影不能指定季号")
        return self
