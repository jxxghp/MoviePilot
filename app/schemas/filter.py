"""候选种子过滤分析的判定契约。"""

from typing import Optional

from pydantic import BaseModel, Field


class TorrentVerdict(BaseModel):
    """
    一个分析器对一个候选种子的判定。

    判定按下标与候选列表一一对应，多个分析器的判定按合取组合：
    任一分析器判定不通过，候选即被过滤。
    """

    analyzer: str = Field(description="给出本条判定的分析器标识")
    passed: bool = Field(description="候选是否通过本分析器")
    reason: str = Field(description="判定依据，未通过时说明被否决的原因")
    order: Optional[int] = Field(
        default=None,
        description="排序权重，数值越大越优先；为空表示本分析器不参与排序",
    )
