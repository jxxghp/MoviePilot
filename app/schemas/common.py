"""API 端点共享的小型业务数据模型。"""

from typing import Optional, Union

from pydantic import BaseModel, Field, RootModel
from typing_extensions import TypeAliasType


JsonData = TypeAliasType(
    "JsonData",
    Union[
        dict[str, "JsonData"],
        list["JsonData"],
        str,
        int,
        float,
        bool,
        None,
    ],
)
"""可递归序列化的 JSON 数据；OpenAPI 会展示每一种合法 JSON 结构。"""


class JsonObject(RootModel[dict[str, JsonData]]):
    """字段由运行时扩展点决定的 JSON 对象。"""


class JsonObjectList(RootModel[list[JsonObject]]):
    """字段由运行时扩展点决定的 JSON 对象列表。"""


class IdData(BaseModel):
    """创建资源后返回的资源 ID。"""

    id: Optional[int | str] = Field(default=None, description="资源 ID")


class ValueData(BaseModel):
    """单个动态配置值。"""

    value: JsonData = Field(default=None, description="配置值")


class FileNameData(BaseModel):
    """文件操作结果中的文件名。"""

    filename: Optional[str] = Field(default=None, description="文件名")


class NameData(BaseModel):
    """名称计算结果。"""

    name: Optional[str] = Field(default=None, description="名称")


class ServiceClientInfo(BaseModel):
    """可选择的下载器或媒体服务器摘要。"""

    name: Optional[str] = Field(default=None, description="实例名称")
    type: Optional[str] = Field(default=None, description="服务类型")


class ProgressKeyData(BaseModel):
    """异步任务进度查询标识。"""

    progress_key: str = Field(description="进度查询标识")


class BatchProgressKeyData(ProgressKeyData):
    """批量异步任务进度查询标识。"""

    history_ids: list[int] = Field(default_factory=list, description="历史记录 ID 列表")


class TimeData(BaseModel):
    """网络请求耗时。"""

    time: int | float = Field(description="耗时毫秒数")
