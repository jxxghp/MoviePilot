"""名称解析管道的数据契约。

解析器交回的是纯字段数据而不是领域富对象：宿主据此构建 `MetaBase`，扩展只需
表达「我认为这些字段是什么」。字段集与 `app.domain.meta.metabase.MetaBase` 的
槽位一一对应，枚举槽位在此退化为其取值字符串，因此整套契约可序列化，扩展改由
独立进程承载时协议原样成立。

管道的控制权在宿主：每一环拿到当前已知结果，返回更完整的结果，宿主负责串接。
下游环可以覆盖上游已填的字段，代价是宿主为每个字段记录来源与被覆盖前的取值，
即 `MetaParseTrace`。
"""

from enum import StrEnum
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from app.schemas.common import JsonData


# 内建解析环在溯源中的解析器标识
BUILTIN_META_PARSER = "builtin"


class ParsedMeta(BaseModel):
    """一次名称解析交出的字段。

    每个字段为 None 表示本环对该字段无话可说，宿主保留上游取值；填了值即表示
    本环认为该字段就是这个取值，与上游不同即构成覆盖。空字符串、0、False 与空
    列表都是取值而不是弃权，与宿主的让出协议一致。

    要把上游填错的字段清空，把字段名列进 `clears`——单靠 None 表达不了「这里
    本来就不该有值」。`clears` 只在解析器交出的贡献上有意义，宿主累积出的结果
    中该字段恒为空。

    名称按 `cn_name`/`en_name` 两个槽位表达，`MetaBase.name` 是二者的派生属性，
    不是槽位，因此不在本契约内。
    """

    # 是否处理的文件
    isfile: Optional[bool] = None
    # 原标题字符串（未经过识别词处理）
    title: Optional[str] = None
    # 识别用字符串（经过识别词处理后）
    org_string: Optional[str] = None
    # 副标题
    subtitle: Optional[str] = None
    # 类型，取值为 MediaType 成员的值，如「电影」「电视剧」
    type: Optional[str] = None
    # 识别的中文名
    cn_name: Optional[str] = None
    # 识别的英文名
    en_name: Optional[str] = None
    # 未应用识别词时识别出的名称
    original_name: Optional[str] = None
    # 年份
    year: Optional[str] = None
    # 总季数
    total_season: Optional[int] = None
    # 识别的开始季
    begin_season: Optional[int] = None
    # 识别的结束季
    end_season: Optional[int] = None
    # 总集数
    total_episode: Optional[int] = None
    # 识别的开始集
    begin_episode: Optional[int] = None
    # 识别的结束集
    end_episode: Optional[int] = None
    # Partx Cd Dvd Disk Disc
    part: Optional[str] = None
    # 识别的资源类型
    resource_type: Optional[str] = None
    # 识别的效果
    resource_effect: Optional[str] = None
    # 识别的分辨率
    resource_pix: Optional[str] = None
    # 识别的制作组/字幕组
    resource_team: Optional[str] = None
    # 识别的自定义占位符
    customization: Optional[str] = None
    # 识别的流媒体平台
    web_source: Optional[str] = None
    # 视频编码
    video_encode: Optional[str] = None
    # 视频位深
    video_bit: Optional[str] = None
    # 音频编码
    audio_encode: Optional[str] = None
    # 应用的识别词信息
    apply_words: Optional[List[str]] = None
    # 媒体主身份来源，取值为 MediaSource 成员的值；与 media_id 必须成对给出
    media_source: Optional[str] = None
    # 媒体主身份在该来源下的原生 ID
    media_id: Optional[str] = None
    # 剧集组
    episode_group: Optional[str] = None
    # 帧率信息（纯数值）
    fps: Optional[int] = None
    # 本环断言应清空的字段名，宿主按槽位默认值抹掉上游取值
    clears: Tuple[str, ...] = ()


# 解析结果承载的字段名，即与 MetaBase 槽位对应的那一部分
PARSED_META_FIELDS: Tuple[str, ...] = tuple(
    name for name in ParsedMeta.model_fields if name != "clears"
)


class MetaParseStatus(StrEnum):
    """单个解析环一次执行的结局。"""

    # 交回了结果，无论是否真的改动了字段
    CLAIMED = "claimed"
    # 返回 None 表示不认领本次解析
    ABSTAINED = "abstained"
    # 抛异常或交回不合契约的结果，本环被跳过
    FAILED = "failed"


class MetaParseRequest(BaseModel):
    """交给单个解析环的输入。

    `parsed` 是截至本环为止的已知结果，因此解析器既能在空位上补值，也能判断上游
    已经解完、自己无事可做而立即弃权。
    """

    # 标题、种子名或文件名
    title: str = ""
    # 副标题、描述
    subtitle: Optional[str] = None
    # 按路径识别时的完整路径，按标题识别时为 None
    path: Optional[str] = None
    # 本次识别使用的自定义识别词
    custom_words: Tuple[str, ...] = ()
    # 上游各环累积出的解析结果
    parsed: ParsedMeta = Field(default_factory=ParsedMeta)


class MetaFieldRevision(BaseModel):
    """某个字段的一次写入记录。"""

    # 写入该取值的解析器标识
    parser: str
    # 该次写入的取值
    value: JsonData = None


class MetaParserRun(BaseModel):
    """某个解析环的一次执行记录。"""

    # 解析器标识
    parser: str
    # 本次执行的结局
    status: MetaParseStatus
    # 本环实际写入的字段名
    fields: Tuple[str, ...] = ()
    # 执行失败的原因
    error: Optional[str] = None


class MetaParseTrace(BaseModel):
    """一次名称解析的字段级溯源。

    `revisions` 按写入顺序记录每个字段的全部取值，最后一条即当前取值的来源，
    其余是被覆盖的原值；`runs` 记录每一环是认领、弃权还是出错，据此可以回答
    「为什么识别成这个」，也能定位到该关掉哪个解析器。
    """

    # 字段名到该字段全部写入记录的映射
    revisions: Dict[str, List[MetaFieldRevision]] = Field(default_factory=dict)
    # 各解析环的执行记录，顺序即执行顺序
    runs: List[MetaParserRun] = Field(default_factory=list)

    def origin(self, field: str) -> Optional[str]:
        """
        返回字段当前取值的来源

        :param field: 字段名
        :return: 写入当前取值的解析器标识；该字段没有写入记录时为 None
        """
        revisions = self.revisions.get(field)
        return revisions[-1].parser if revisions else None

    def overridden(self, field: str) -> Tuple[MetaFieldRevision, ...]:
        """
        返回字段被覆盖前的历次取值

        :param field: 字段名
        :return: 按写入顺序排列的历史写入记录，不含当前取值
        """
        revisions = self.revisions.get(field) or []
        return tuple(revisions[:-1])


class MetaParseOutcome(BaseModel):
    """整条管道跑完后的结果与溯源。"""

    # 各环累积出的最终解析结果
    parsed: ParsedMeta = Field(default_factory=ParsedMeta)
    # 字段级溯源
    trace: MetaParseTrace = Field(default_factory=MetaParseTrace)


class MetaParserOrderEntry(BaseModel):
    """名称解析管道顺序配置中的一条。

    顺序即语义——谁先跑决定谁的结果会被覆盖，因此它必须是用户看得见也改得动的
    持久数据，而不是宿主的登记顺序。未出现在配置里的解析器按其声明的 priority
    追加在末尾，声明的 priority 只作默认初始顺序。
    """

    # 解析器标识
    parser: str
    # 是否参与解析，为 False 时该环整体不执行
    enabled: bool = True


class MetaParserRing(BaseModel):
    """名称解析管道中一环的呈现。

    解析环标识是 `实例键#声明标识` 的合成串，单看它认不出是哪个插件的哪个分身
    贡献的哪一环，因此三段各自拆成独立字段；标识本身保留，供顺序与启停接口按原样
    回传。
    """

    # 解析环标识，形如 AIMetaPlugin@alt#llm；内建环为 builtin
    parser: str
    # 声明标识，即扩展在自己命名空间内给这一环起的名字
    parser_id: str
    # 展示名称
    name: str
    # 登记方实例键，形如 AIMetaPlugin@alt；内建环为 None
    owner: Optional[str] = None
    # 登记方的扩展标识，即哪个插件；内建环为 None
    extension_id: Optional[str] = None
    # 登记方的实例标识，即哪个分身；内建环为 None
    instance_id: Optional[str] = None
    # 声明的默认顺序，只在用户未排到该环时决定它排在哪
    priority: int = 0
    # 该环是否参与解析
    enabled: bool = True
    # 该环在最终生效顺序中的位次，停用的环仍占住位次但不执行
    order: int = 0
    # 用户是否显式排过该环，为 False 表示按声明 priority 追加在末尾
    configured: bool = False
    # 登记方的发行方式
    distribution: str = "builtin"
    # 位次与启停是否由宿主固定，内建环恒为 True
    pinned: bool = False


class MetaParserPipeline(BaseModel):
    """名称解析管道当前的最终生效顺序。"""

    # 按最终生效顺序排列的解析环
    rings: List[MetaParserRing] = Field(default_factory=list)


class MetaParserToggle(BaseModel):
    """单独启停一个解析环的请求。"""

    # 解析环标识
    parser: str
    # 目标启停状态
    enabled: bool
