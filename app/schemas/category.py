from datetime import datetime
from typing import Dict, Literal, Optional, TypeAlias, Union

from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator, model_validator

from app.schemas.common import JsonData


class CategoryRule(BaseModel):
    """
    分类规则详情
    """

    # 内容类型
    genre_ids: Optional[str] = None
    # 语种
    original_language: Optional[str] = None
    # 国家或地区（电视剧）
    origin_country: Optional[str] = None
    # 国家或地区（电影）
    production_countries: Optional[str] = None
    # 发行年份
    release_year: Optional[str] = None
    # 允许接收其他动态字段
    model_config = ConfigDict(extra="allow")


class CategoryConfig(BaseModel):
    """
    分类策略配置
    """

    # 电影分类策略
    movie: Optional[Dict[str, Optional[CategoryRule]]] = Field(default_factory=dict)
    # 电视剧分类策略
    tv: Optional[Dict[str, Optional[CategoryRule]]] = Field(default_factory=dict)


class MediaCategoryMap(RootModel[Dict[str, list[str]]]):
    """媒体类型与自动分类名称列表的映射。"""


ClassificationMediaType: TypeAlias = Literal["电影", "电视剧", "音乐"]
"""分类体系支持的媒体类型。"""

ClassificationRuleKind: TypeAlias = Literal["category", "label"]
"""分类规则的输出类型。"""

ClassificationPolicyMode: TypeAlias = Literal["first_match"]
"""主分类规则的求值模式。"""

ClassificationEnrichmentMode: TypeAlias = Literal["primary_only", "enrich_missing"]
"""分类事实是否允许通过其它数据源补充缺失标准字段。"""

ClassificationSourceSupport: TypeAlias = Literal[
    "native",
    "derived",
    "partial",
    "extension",
    "unavailable",
]
"""数据源对单个分类字段的支持等级。"""

ClassificationOperator: TypeAlias = Literal[
    "equals",
    "not_equals",
    "in",
    "not_in",
    "contains",
    "starts_with",
    "ends_with",
    "gt",
    "gte",
    "lt",
    "lte",
    "between",
    "contains_any",
    "contains_all",
    "contains_none",
    "is_true",
    "is_false",
    "exists",
    "not_exists",
]
"""条件叶子可声明的操作符。"""

ClassificationFactScalar: TypeAlias = Union[str, int, float, bool, None]
"""分类事实允许的 JSON 标量。"""

ClassificationFactValue: TypeAlias = Union[ClassificationFactScalar, list[ClassificationFactScalar]]
"""分类事实和条件值允许的标量或标量列表。"""

ClassificationFieldValueType: TypeAlias = Literal[
    "string",
    "enum",
    "integer",
    "number",
    "year",
    "string_list",
    "boolean",
]
"""字段目录支持的有限输入控件和值语义类型。"""


class _ClassificationModel(BaseModel):  # type: ignore[misc]
    """分类新契约的严格字段基类，避免静默吞掉前端字段拼写错误。"""

    model_config = ConfigDict(extra="forbid")


class ClassificationCategory(_ClassificationModel):
    """稳定分类定义，ID 用于引用，路径用于生成媒体库相对目录。"""

    id: str = Field(description="稳定分类 ID")
    media_type: ClassificationMediaType = Field(description="分类适用的媒体类型")
    name: str = Field(description="分类显示名称")
    path: list[str] = Field(default_factory=list, description="相对于媒体类型根目录的路径段")
    enabled: bool = Field(default=True, description="分类是否启用")
    labels: list[str] = Field(default_factory=list, description="分类默认附加标签")


class ClassificationCondition(_ClassificationModel):
    """条件树的叶子节点，描述字段、操作符和期望值。"""

    field: str = Field(description="字段目录中的稳定字段 ID")
    operator: ClassificationOperator = Field(description="字段条件操作符")
    value: ClassificationFactValue = Field(default=None, description="条件期望值；存在性操作符可不提供")


class ClassificationConditionGroup(_ClassificationModel):
    """递归条件组；每个节点只能声明 all、any、not 中的一种。"""

    all: Optional[list[Union[ClassificationCondition, "ClassificationConditionGroup"]]] = Field(
        default=None,
        description="必须全部匹配的子节点",
    )
    any: Optional[list[Union[ClassificationCondition, "ClassificationConditionGroup"]]] = Field(
        default=None,
        description="至少一个匹配的子节点",
    )
    not_: Optional[Union[ClassificationCondition, "ClassificationConditionGroup"]] = Field(
        default=None,
        validation_alias="not",
        serialization_alias="not",
        description="需要取反的单个子节点",
    )

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        serialize_by_alias=True,
    )

    @model_validator(mode="after")  # type: ignore[misc]
    def validate_group_operator(self) -> "ClassificationConditionGroup":
        """确保条件组恰好选择一个组操作符。"""
        configured = sum(value is not None for value in (self.all, self.any, self.not_))
        if configured != 1:
            raise ValueError("条件组必须且只能声明 all、any、not 中的一种")
        return self


ClassificationConditionNode: TypeAlias = Union[ClassificationCondition, ClassificationConditionGroup]
"""条件树中允许出现的叶子或条件组节点。"""


class ClassificationTarget(_ClassificationModel):
    """规则命中后输出的主分类和附加标签。"""

    category_id: Optional[str] = Field(default=None, description="目标分类 ID")
    labels: list[str] = Field(default_factory=list, description="规则命中后附加的标签")


class ClassificationRule(_ClassificationModel):
    """全局有序的分类规则定义。"""

    id: str = Field(description="稳定规则 ID")
    name: str = Field(description="规则显示名称")
    kind: ClassificationRuleKind = Field(description="规则输出类型")
    enabled: bool = Field(default=True, description="规则是否启用")
    priority: int = Field(default=0, description="服务端保存后的规则顺序投影")
    media_types: list[ClassificationMediaType] = Field(default_factory=list, description="规则适用的媒体类型")
    sources: list[str] = Field(default_factory=list, description="规则适用的数据源；空列表表示全部来源")
    when: ClassificationConditionNode = Field(description="规则的递归条件树")
    target: ClassificationTarget = Field(default_factory=ClassificationTarget, description="规则命中目标")


class ClassificationPolicy(_ClassificationModel):
    """可版本化发布的完整媒体分类策略。"""

    schema_version: Literal[2] = Field(default=2, description="分类策略结构版本")
    revision: int = Field(default=0, ge=0, description="已发布策略修订号")
    mode: ClassificationPolicyMode = Field(default="first_match", description="主分类规则求值模式")
    enrichment_mode: ClassificationEnrichmentMode = Field(
        default="primary_only",
        description="缺失标准事实的跨来源补充模式",
    )
    categories: list[ClassificationCategory] = Field(default_factory=list, description="稳定分类定义列表")
    rules: list[ClassificationRule] = Field(default_factory=list, description="全局有序规则列表")
    fallbacks: dict[ClassificationMediaType, str] = Field(default_factory=dict, description="各媒体类型的兜底分类 ID")
    field_aliases: dict[str, dict[str, str]] = Field(default_factory=dict, description="字段值别名到规范值的映射")
    updated_at: Optional[datetime] = Field(default=None, description="策略最后发布时间")


class ClassificationPolicyState(_ClassificationModel):
    """单个系统配置键中原子保存的活动分类策略和历史快照。"""

    active: ClassificationPolicy = Field(description="当前活动分类策略")
    history: list[ClassificationPolicy] = Field(
        default_factory=list,
        max_length=10,
        description="按 revision 从新到旧排列的最近历史策略",
    )

    @model_validator(mode="after")  # type: ignore[misc]
    def validate_revisions(self) -> "ClassificationPolicyState":
        """保证活动 revision 有效，且历史 revision 唯一、降序并早于活动版本。"""
        if self.active.revision < 1:
            raise ValueError("活动分类策略 revision 必须大于等于 1")
        revisions = [policy.revision for policy in self.history]
        if any(revision < 1 for revision in revisions):
            raise ValueError("历史分类策略 revision 必须大于等于 1")
        if len(set(revisions)) != len(revisions):
            raise ValueError("历史分类策略 revision 不能重复")
        if revisions != sorted(revisions, reverse=True):
            raise ValueError("历史分类策略必须按 revision 从新到旧排列")
        if any(revision >= self.active.revision for revision in revisions):
            raise ValueError("历史分类策略 revision 必须早于活动版本")
        return self


class ClassificationIdentityFacts(_ClassificationModel):
    """分类输入中的稳定媒体身份事实。"""

    media_source: str = Field(description="当前主身份数据源")
    media_id: str = Field(description="当前数据源原生媒体 ID")


class ClassificationMediaFacts(_ClassificationModel):
    """电影、电视剧和音乐共享的标准分类事实。"""

    type: ClassificationMediaType = Field(description="标准媒体类型")
    title: Optional[str] = Field(default=None, description="标准媒体标题")
    year: Optional[int] = Field(default=None, description="标准发行或首播年份")
    language: Optional[str] = Field(default=None, description="标准语言代码")
    countries: Optional[list[str]] = Field(default=None, description="标准国家或地区代码")
    genre_keys: Optional[list[str]] = Field(default=None, description="MoviePilot 规范化类型键")
    genre_names: Optional[list[str]] = Field(default=None, description="数据源提供的类型名称")
    adult: Optional[bool] = Field(default=None, description="是否为成人内容")
    runtime: Optional[int] = Field(default=None, description="影视时长，单位为分钟")
    content_rating: Optional[str] = Field(default=None, description="内容分级")
    companies: Optional[list[str]] = Field(default=None, description="出品公司或工作室名称")
    networks: Optional[list[str]] = Field(default=None, description="电视台或播出平台名称")


class ClassificationMusicFacts(_ClassificationModel):
    """音乐实体专用的标准分类事实。"""

    entity_type: Optional[str] = Field(default=None, description="音乐实体类型")
    album_type: Optional[str] = Field(default=None, description="专辑主类型")
    secondary_types: Optional[list[str]] = Field(default=None, description="专辑副类型")
    genres: Optional[list[str]] = Field(default=None, description="标准化音乐流派")
    tags: Optional[list[str]] = Field(default=None, description="数据源音乐标签")
    artists: Optional[list[str]] = Field(default=None, description="艺术家名称")
    artist_country: Optional[str] = Field(default=None, description="艺术家国家或地区")
    release_status: Optional[str] = Field(default=None, description="发行状态")


class ClassificationFactSource(_ClassificationModel):
    """记录一个补充事实的媒体来源和实际 provider 身份。"""

    media_source: str = Field(description="提供该事实的数据源")
    provider_id: str = Field(description="宿主分配的稳定 provider ID")
    provider_name: str = Field(description="provider 显示名称")


class ClassificationFacts(_ClassificationModel):
    """规则求值器唯一接收的标准化分类事实集合。"""

    identity: ClassificationIdentityFacts = Field(description="媒体身份事实")
    media: ClassificationMediaFacts = Field(description="通用媒体事实")
    music: Optional[ClassificationMusicFacts] = Field(default=None, description="音乐专用事实")
    extensions: dict[str, dict[str, ClassificationFactValue]] = Field(
        default_factory=dict,
        description="按数据源命名空间隔离的插件扩展事实",
    )
    field_sources: dict[str, ClassificationFactSource] = Field(
        default_factory=dict,
        description="跨来源补充事实到实际 provider 的映射",
    )


class ClassificationEnrichmentMatch(_ClassificationModel):
    """provider 用于证明补充结果属于同一媒体的匹配依据。"""

    kind: Literal["external_id", "explicit_mapping"] = Field(
        description="外部 ID 命中或 provider 明确映射",
    )
    media_source: str = Field(description="用于匹配的来源")
    media_id: str = Field(description="用于匹配的来源原生 ID")


class ClassificationEnrichmentRequest(_ClassificationModel):
    """传给可选分类事实 provider 的有界只读请求。"""

    identity: ClassificationIdentityFacts = Field(description="不可变的主媒体身份")
    media_type: ClassificationMediaType = Field(description="当前媒体类型")
    missing_fields: list[str] = Field(
        min_length=1,
        description="活动策略实际引用且当前缺失的标准字段",
    )
    external_ids: dict[str, str] = Field(
        default_factory=dict,
        description="当前媒体已知的其它来源身份或 ISRC 等标准外部标识",
    )
    policy_revision: int = Field(ge=1, description="本次分类使用的策略 revision")
    timeout_seconds: float = Field(
        gt=0,
        description="provider 应遵守的单次调用剩余时间预算",
    )


class ClassificationEnrichmentResponse(_ClassificationModel):
    """可选 provider 返回的同媒体标准事实补充结果。"""

    media_source: str = Field(description="补充事实所属的数据源")
    match: ClassificationEnrichmentMatch = Field(description="同媒体匹配证明")
    facts: dict[str, ClassificationFactValue] = Field(
        default_factory=dict,
        description="仅包含请求字段的标准分类事实",
    )


class ClassificationSelection(_ClassificationModel):
    """推荐或最终生效的分类选择快照。"""

    category_id: Optional[str] = Field(default=None, description="分类 ID")
    category_path: list[str] = Field(default_factory=list, description="分类目录路径快照")
    rule_id: Optional[str] = Field(default=None, description="命中的规则 ID")
    source: Optional[str] = Field(default=None, description="分类选择来源")


class ClassificationResult(_ClassificationModel):
    """分类求值产生的推荐、生效结果和状态。"""

    recommended: Optional[ClassificationSelection] = Field(default=None, description="规则自动推荐结果")
    effective: Optional[ClassificationSelection] = Field(default=None, description="按覆盖优先级解析后的结果")
    labels: list[str] = Field(default_factory=list, description="稳定去重后的附加标签")
    policy_revision: int = Field(default=0, description="本次求值使用的策略修订号")
    state: Literal["complete", "partial", "not_evaluated", "invalid_policy"] = Field(
        default="not_evaluated",
        description="分类求值完成状态",
    )


class ClassificationConditionTrace(_ClassificationModel):
    """预览中单个叶子条件的实际求值记录。"""

    field: str = Field(description="字段 ID")
    operator: ClassificationOperator = Field(description="条件操作符")
    expected: ClassificationFactValue = Field(default=None, description="条件期望值")
    actual: ClassificationFactValue = Field(default=None, description="求值时读取的实际值")
    source: Optional[ClassificationFactSource] = Field(
        default=None,
        description="该实际值由跨来源补充时使用的 provider",
    )
    matched: bool = Field(description="条件是否匹配")
    path: list[Union[str, int]] = Field(
        default_factory=list,
        description="条件在策略 JSON 中的位置",
    )


class ClassificationRuleTrace(_ClassificationModel):
    """预览中单条规则及其叶子条件的命中记录。"""

    rule_id: str = Field(description="规则 ID")
    matched: bool = Field(description="规则是否匹配")
    conditions: list[ClassificationConditionTrace] = Field(default_factory=list, description="叶子条件求值记录")


class ClassificationEvaluation(_ClassificationModel):
    """分类预览接口返回的事实、结果、命中解释和警告。"""

    facts: ClassificationFacts = Field(description="本次求值使用的标准事实")
    result: ClassificationResult = Field(description="本次分类结果")
    trace: list[ClassificationRuleTrace] = Field(default_factory=list, description="规则命中解释")
    warnings: list["ClassificationEvaluationWarning"] = Field(
        default_factory=list,
        description="事实缺失或来源覆盖警告",
    )


class ClassificationEvaluationWarning(_ClassificationModel):
    """预览中可定位到字段和来源的结构化提示。"""

    code: str = Field(description="稳定提示代码")
    message: str = Field(description="面向用户的提示文本")
    path: list[Union[str, int]] = Field(
        default_factory=list,
        description="相关条件在策略 JSON 中的位置",
    )
    field: Optional[str] = Field(default=None, description="相关字段 ID")
    source: Optional[str] = Field(default=None, description="相关主身份数据源")


class ClassificationFieldOption(_ClassificationModel):
    """字段枚举控件使用的稳定值与显示文本。"""

    value: ClassificationFactScalar = Field(description="规则中保存的稳定值")
    label: str = Field(description="前端显示文本")


class ClassificationFieldDefinition(_ClassificationModel):
    """前端条件编辑器使用的字段能力目录项。"""

    id: str = Field(description="稳定字段 ID")
    label: str = Field(description="本地化显示名称")
    group: str = Field(default="通用", description="前端字段选择器中的分组名称")
    description: Optional[str] = Field(default=None, description="字段用途说明")
    value_type: ClassificationFieldValueType = Field(description="字段值类型")
    operators: list[ClassificationOperator] = Field(default_factory=list, description="字段允许的操作符")
    media_types: list[ClassificationMediaType] = Field(default_factory=list, description="字段适用的媒体类型")
    options: list[ClassificationFieldOption] = Field(default_factory=list, description="字段可选值目录")
    allow_custom_values: bool = Field(
        default=True,
        description="前端是否允许输入选项目录之外的值",
    )
    source_support: dict[str, ClassificationSourceSupport] = Field(
        default_factory=dict,
        description="各数据源对字段的支持等级",
    )
    selectable: bool = Field(
        default=True,
        description="是否允许在新规则的字段选择器中使用",
    )
    replacement_field: Optional[str] = Field(
        default=None,
        description="退役字段建议替换为的标准字段 ID",
    )

    @field_validator("options", mode="before")  # type: ignore[misc]
    @classmethod
    def normalize_options(cls, value: object) -> object:
        """兼容旧扩展字段的标量选项，并统一序列化为值和标签对象。"""
        if not isinstance(value, (list, tuple)):
            return value
        return [
            item
            if isinstance(item, (dict, ClassificationFieldOption))
            else {"value": item, "label": str(item)}
            for item in value
        ]


class ClassificationValidationIssue(_ClassificationModel):
    """策略校验产生的单条错误或警告。"""

    severity: Literal["error", "warning"] = Field(description="问题严重级别")
    code: str = Field(description="稳定问题代码")
    message: str = Field(description="面向用户的问题说明")
    path: list[Union[str, int]] = Field(default_factory=list, description="问题在策略 JSON 中的位置")


class ClassificationValidationResult(_ClassificationModel):
    """策略草稿的结构化校验结果。"""

    valid: bool = Field(description="策略是否可以发布")
    issues: list[ClassificationValidationIssue] = Field(default_factory=list, description="全部错误和警告")


class ClassificationPolicyPublishRequest(_ClassificationModel):
    """以客户端读取到的 revision 发布完整分类策略草稿。"""

    expected_revision: int = Field(ge=0, description="客户端读取策略时的活动 revision")
    policy: ClassificationPolicy = Field(description="待校验并整体发布的分类策略草稿")


class ClassificationPolicyRollbackRequest(_ClassificationModel):
    """以当前活动 revision 为并发前提发布一个历史策略内容。"""

    expected_revision: int = Field(ge=1, description="客户端读取策略时的活动 revision")


class ClassificationPolicyValidateRequest(_ClassificationModel):
    """请求服务端使用当前字段目录校验一个完整策略草稿。"""

    policy: ClassificationPolicy = Field(description="仅校验、不保存的完整策略草稿")


class ClassificationPolicyLimits(_ClassificationModel):
    """前端规则编辑器必须遵守的服务端策略结构限制。"""

    max_category_depth: int = Field(ge=1, description="分类路径最大层数")
    max_category_segment_length: int = Field(ge=1, description="单个分类路径段最大长度")
    max_category_path_length: int = Field(ge=1, description="分类相对路径最大总长度")
    max_condition_depth: int = Field(ge=1, description="条件树最大嵌套深度")
    max_conditions_per_rule: int = Field(ge=1, description="单条规则最大叶子条件数量")
    max_rules: int = Field(ge=1, description="策略最大规则数量")
    max_total_conditions: int = Field(ge=1, description="策略最大叶子条件总数")


class ClassificationFieldCatalog(_ClassificationModel):
    """前端规则编辑器使用的完整动态字段目录。"""

    fields: list[ClassificationFieldDefinition] = Field(
        default_factory=list,
        description="允许在新规则中选择的标准字段与来源扩展字段",
    )
    retired_fields: list[ClassificationFieldDefinition] = Field(
        default_factory=list,
        description="仅用于解析已有规则、不可新增的退役字段",
    )
    limits: ClassificationPolicyLimits = Field(description="服务端策略结构限制")


class ClassificationFactsPreviewInput(_ClassificationModel):
    """预览请求中由调用方直接提供的标准分类事实。"""

    kind: Literal["facts"] = Field(default="facts", description="预览输入类型")
    facts: ClassificationFacts = Field(description="本次预览使用的标准化分类事实")


class ClassificationMediaPreviewInput(_ClassificationModel):
    """从媒体搜索结果选择的完整媒体信息，用于直接预览分类结果。"""

    kind: Literal["media"] = Field(default="media", description="预览输入类型")
    media: dict[str, JsonData] = Field(description="从媒体搜索结果选择的媒体信息")

    @model_validator(mode="after")  # type: ignore[misc]
    def validate_media_identity(self) -> "ClassificationMediaPreviewInput":
        """确保搜索结果包含分类所需的来源、编号和媒体类型。"""
        source = str(self.media.get("media_source") or "").strip()
        media_id = str(self.media.get("media_id") or "").strip()
        media_type = str(self.media.get("type") or "").strip()
        if not source:
            raise ValueError("选择的媒体缺少数据来源")
        if not media_id:
            raise ValueError("选择的媒体缺少媒体编号")
        if media_type not in {"电影", "电视剧", "音乐"}:
            raise ValueError("选择的媒体类型不受分类规则支持")
        return self


ClassificationPreviewInput: TypeAlias = Union[
    ClassificationFactsPreviewInput,
    ClassificationMediaPreviewInput,
]
"""预览输入联合；前端通常提交搜索结果，旧调用仍可提交标准事实。"""


class ClassificationPreviewRequest(_ClassificationModel):
    """对显式事实执行活动策略或未发布草稿的只读预览。"""

    input: ClassificationPreviewInput = Field(description="可判别的预览输入")
    policy: Optional[ClassificationPolicy] = Field(
        default=None,
        description="可选未发布策略；省略时使用当前活动策略",
    )


class ClassificationImpactRequest(_ClassificationModel):
    """比较活动策略和未发布草稿对显式或近期历史样本的影响。"""

    expected_revision: int = Field(
        ge=1,
        description="本次影响分析使用的活动策略基线 revision",
    )
    policy: ClassificationPolicy = Field(description="待比较的未发布完整策略草稿")
    sample_limit: int = Field(
        default=100,
        ge=1,
        le=200,
        description="近期历史或显式事实的最大比较数量",
    )
    example_limit: int = Field(
        default=20,
        ge=0,
        le=50,
        description="响应中最多返回的变化样本明细数量",
    )
    samples: list[ClassificationFacts] = Field(
        default_factory=list,
        max_length=200,
        description="可选显式事实；为空时使用近期下载和整理历史样本",
    )


class ClassificationImpactChange(_ClassificationModel):
    """一个样本在活动策略和候选策略之间发生的分类变化。"""

    identity: ClassificationIdentityFacts = Field(description="样本的稳定媒体身份")
    media_type: ClassificationMediaType = Field(description="样本媒体类型")
    title: Optional[str] = Field(default=None, description="样本标题")
    changed_fields: list[str] = Field(
        default_factory=list,
        description="发生变化的结果字段",
    )
    previous: ClassificationResult = Field(description="活动策略分类结果")
    candidate: ClassificationResult = Field(description="候选策略分类结果")


class ClassificationImpactGroup(_ClassificationModel):
    """按媒体类型和主身份来源聚合的影响统计。"""

    media_type: ClassificationMediaType = Field(description="分组媒体类型")
    media_source: str = Field(description="分组主身份数据源")
    sampled: int = Field(ge=0, description="分组参与比较的样本数量")
    changed: int = Field(ge=0, description="分组分类结果变化数量")
    degraded: int = Field(ge=0, description="分组从 complete 降为 partial 的数量")


class ClassificationImpactAnalysis(_ClassificationModel):
    """有边界的近期样本分类影响统计与变化明细。"""

    estimated: Literal[True] = Field(
        default=True,
        description="明确标记结果为有界样本估算而非全库精确重分类",
    )
    sampled_at: datetime = Field(description="本次样本分析完成时间")
    sample_source: Literal["request", "recent_history"] = Field(
        description="样本来自请求显式事实或近期历史",
    )
    baseline_revision: int = Field(ge=1, description="活动策略基线 revision")
    candidate_revision: int = Field(ge=2, description="候选策略预计发布 revision")
    requested_limit: int = Field(ge=1, le=200, description="请求的最大样本数量")
    scanned_count: int = Field(ge=0, description="为生成样本实际扫描的记录数量")
    skipped_count: int = Field(ge=0, description="未参与比较的记录数量")
    unresolved_count: int = Field(
        ge=0,
        description="身份有效但无法重新获取完整媒体信息的记录数量",
    )
    truncated: bool = Field(description="是否因样本或示例上限截断结果")
    sample_count: int = Field(ge=0, description="实际参与比较的唯一有效样本数量")
    changed_count: int = Field(ge=0, description="分类结果发生变化的样本数量")
    unchanged_count: int = Field(ge=0, description="分类结果保持不变的样本数量")
    category_changed_count: int = Field(ge=0, description="稳定分类 ID 发生变化的样本数量")
    path_only_changed_count: int = Field(ge=0, description="分类 ID 不变但路径变化的样本数量")
    rule_changed_only_count: int = Field(ge=0, description="分类与路径不变但命中规则变化的样本数量")
    became_fallback_count: int = Field(ge=0, description="候选策略改为媒体类型默认分类的样本数量")
    partial_count: int = Field(ge=0, description="任一策略因事实缺失产生 partial 的样本数量")
    degraded_count: int = Field(
        ge=0,
        description="候选策略从 complete 降为 partial 的样本数量",
    )
    previous_categories: dict[str, int] = Field(
        default_factory=dict,
        description="活动策略按稳定分类 ID 聚合的样本数量",
    )
    candidate_categories: dict[str, int] = Field(
        default_factory=dict,
        description="候选策略按稳定分类 ID 聚合的样本数量",
    )
    groups: list[ClassificationImpactGroup] = Field(
        default_factory=list,
        description="按媒体类型和数据源聚合的影响统计",
    )
    changes: list[ClassificationImpactChange] = Field(
        default_factory=list,
        description="只包含结果发生变化的样本明细",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="样本覆盖和事实完整性的边界提示",
    )


class ClassificationRevisionConflict(_ClassificationModel):
    """分类策略发布或回滚遇到的 revision 冲突详情。"""

    code: Literal["classification_revision_conflict"] = Field(
        default="classification_revision_conflict",
        description="前端稳定识别的冲突代码",
    )
    expected_revision: int = Field(ge=0, description="请求携带的 revision")
    current_revision: int = Field(ge=0, description="服务端当前活动 revision")


class ClassificationPolicyHistory(_ClassificationModel):
    """当前 revision 与有界历史策略快照列表。"""

    active_revision: int = Field(ge=1, description="当前活动策略 revision")
    items: list[ClassificationPolicy] = Field(
        default_factory=list,
        max_length=10,
        description="按 revision 从新到旧排列且不含活动策略的历史",
    )


class ClassificationPolicyRollbackResult(_ClassificationModel):
    """把历史策略内容发布为新 revision 后的结果。"""

    restored_from_revision: int = Field(ge=1, description="被选中的历史 revision")
    policy: ClassificationPolicy = Field(description="新发布的活动策略")
