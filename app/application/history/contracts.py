"""
整理历史 DTO 与类型化仓储契约。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable, NoReturn, Optional, Protocol

from app.schemas.common import JsonData
from app.schemas.types import MediaSource


class _FrozenJsonDict(dict[str, JsonData]):
    """
    保留 JSON 字典读取与序列化行为，并拒绝常规原地修改。
    """

    def _reject_mutation(self, *args: Any, **kwargs: Any) -> NoReturn:
        """
        拒绝修改已经进入历史快照的嵌套 JSON。
        """
        raise TypeError("历史快照 JSON 不可修改")

    __setitem__ = _reject_mutation
    __delitem__ = _reject_mutation
    __ior__ = _reject_mutation
    clear = _reject_mutation
    pop = _reject_mutation
    popitem = _reject_mutation
    setdefault = _reject_mutation
    update = _reject_mutation


class _FrozenJsonList(list[JsonData]):
    """
    保留 JSON 数组读取与序列化行为，并拒绝常规原地修改。
    """

    def _reject_mutation(self, *args: Any, **kwargs: Any) -> NoReturn:
        """
        拒绝修改已经进入历史快照的嵌套 JSON。
        """
        raise TypeError("历史快照 JSON 不可修改")

    __setitem__ = _reject_mutation
    __delitem__ = _reject_mutation
    __iadd__ = _reject_mutation
    __imul__ = _reject_mutation
    append = _reject_mutation
    clear = _reject_mutation
    extend = _reject_mutation
    insert = _reject_mutation
    pop = _reject_mutation
    remove = _reject_mutation
    reverse = _reject_mutation
    sort = _reject_mutation


def _freeze_json(value: JsonData) -> JsonData:
    """
    递归复制并冻结 JSON 容器，避免快照内部仍暴露可变引用。
    """
    if isinstance(value, dict):
        return _FrozenJsonDict({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return _FrozenJsonList([_freeze_json(item) for item in value])
    return value


@dataclass(frozen=True, slots=True)
class TransferHistorySnapshot:
    """
    脱离数据库会话后供整理、Agent 和历史用例读取的完整历史快照。

    :param id: 整理历史记录的数据库主键
    :param transfer_task_id: 关联的持久整理任务标识；旧历史可能为 None
    :param transfer_settlement_revision: 对应持久任务终态回执的结算版本
    :param src: 整理前的源文件或目录路径
    :param src_storage: 源文件所在的存储配置标识
    :param src_fileitem: 源文件项的冻结 JSON 快照
    :param dest: 整理后的目标文件或目录路径
    :param dest_storage: 目标文件所在的存储配置标识
    :param dest_fileitem: 目标文件项的冻结 JSON 快照
    :param mode: 整理方式，例如移动、复制或链接
    :param type: 媒体类型，区分电影、电视剧和音乐
    :param media_category_id: 媒体类别的稳定标识
    :param category: 整理目标采用的媒体类别路径
    :param classification_rule_id: 当次分类命中的规则标识
    :param classification_policy_revision: 当次执行使用的分类策略版本
    :param classification_source: 当次最终分类的依据来源
    :param title: 识别出的标准媒体标题
    :param year: 媒体发行年份；未知时为 None
    :param media_source: 标准媒体来源，与 media_id 配对使用
    :param media_id: 当前来源中的原生媒体标识
    :param music_type: 音乐实体类型，区分单曲录音与专辑
    :param total_tracks: 音乐专辑预期的总曲目数
    :param audio_format: 音乐资源的音频格式
    :param audio_lossless: 是否为无损音频；未知时为 None
    :param bit_depth: 音频采样位深，单位为位
    :param sample_rate: 音频采样率，单位为赫兹
    :param bitrate: 音频码率，单位为比特每秒
    :param seasons: 已识别的季号或季范围文本
    :param episodes: 识别出的集号或集范围文本
    :param image: 通知和历史展示使用的图片地址
    :param downloader: 下载器配置名称
    :param download_hash: 下载器中的任务哈希标识
    :param status: 本次整理是否成功，默认 True
    :param errmsg: 整理失败的可读错误说明；无错误时为 None
    :param failure_stage: 整理失败发生的业务阶段
    :param recovery_action: 允许用户或后台执行的恢复动作
    :param retry_count: 关联任务累计的自动重试次数
    :param retry_exhausted: 自动重试预算是否已经耗尽
    :param auto_paused: 是否因自动恢复策略暂停任务
    :param cleanup_status: 媒体入库后下载器清理的当前状态
    :param cleanup_error: 下载器清理失败的错误说明
    :param date: 历史记录的时间文本
    :param files: 本次整理关联文件清单的冻结 JSON 快照
    :param episode_group: 当前媒体使用的剧集组标识
    """

    id: int
    transfer_task_id: Optional[str] = None
    transfer_settlement_revision: Optional[int] = None
    src: Optional[str] = None
    src_storage: Optional[str] = None
    src_fileitem: Optional[JsonData] = None
    dest: Optional[str] = None
    dest_storage: Optional[str] = None
    dest_fileitem: Optional[JsonData] = None
    mode: Optional[str] = None
    type: Optional[str] = None
    media_category_id: Optional[str] = None
    category: Optional[str] = None
    classification_rule_id: Optional[str] = None
    classification_policy_revision: Optional[int] = None
    classification_source: Optional[str] = None
    title: Optional[str] = None
    year: Optional[str] = None
    media_source: Optional[MediaSource] = None
    media_id: Optional[str] = None
    music_type: Optional[str] = None
    total_tracks: Optional[int] = None
    audio_format: Optional[str] = None
    audio_lossless: Optional[bool] = None
    bit_depth: Optional[int] = None
    sample_rate: Optional[int] = None
    bitrate: Optional[int] = None
    seasons: Optional[str] = None
    episodes: Optional[str] = None
    image: Optional[str] = None
    downloader: Optional[str] = None
    download_hash: Optional[str] = None
    status: bool = True
    errmsg: Optional[str] = None
    failure_stage: Optional[str] = None
    recovery_action: Optional[str] = None
    retry_count: Optional[int] = None
    retry_exhausted: bool = False
    auto_paused: bool = False
    cleanup_status: Optional[str] = None
    cleanup_error: Optional[str] = None
    date: Optional[str] = None
    files: Optional[JsonData] = None
    episode_group: Optional[str] = None

    def __post_init__(self) -> None:
        """
        递归冻结历史 JSON 字段，避免跨层共享可变 ORM 列值。
        """
        object.__setattr__(self, "src_fileitem", _freeze_json(self.src_fileitem))
        object.__setattr__(self, "dest_fileitem", _freeze_json(self.dest_fileitem))
        object.__setattr__(self, "files", _freeze_json(self.files))


@dataclass(frozen=True, slots=True)
class TransferHistoryWrite:
    """
    替换同源整理历史所需的完整稳定写入数据。

    :param src: 整理前的源文件或目录路径
    :param src_storage: 源文件所在的存储配置标识
    :param src_fileitem: 源文件项的JSON 快照
    :param dest: 整理后的目标文件或目录路径
    :param dest_storage: 目标文件所在的存储配置标识
    :param dest_fileitem: 目标文件项的JSON 快照
    :param mode: 整理方式，例如移动、复制或链接
    :param type: 媒体类型，区分电影、电视剧和音乐
    :param media_category_id: 媒体类别的稳定标识
    :param category: 整理目标采用的媒体类别路径
    :param classification_rule_id: 当次分类命中的规则标识
    :param classification_policy_revision: 当次执行使用的分类策略版本
    :param classification_source: 当次最终分类的依据来源
    :param title: 识别出的标准媒体标题
    :param year: 媒体发行年份；未知时为 None
    :param media_source: 标准媒体来源，与 media_id 配对使用
    :param media_id: 当前来源中的原生媒体标识
    :param music_type: 音乐实体类型，区分单曲录音与专辑
    :param total_tracks: 音乐专辑预期的总曲目数
    :param audio_format: 音乐资源的音频格式
    :param audio_lossless: 是否为无损音频；未知时为 None
    :param bit_depth: 音频采样位深，单位为位
    :param sample_rate: 音频采样率，单位为赫兹
    :param bitrate: 音频码率，单位为比特每秒
    :param seasons: 已识别的季号或季范围文本
    :param episodes: 识别出的集号或集范围文本
    :param image: 通知和历史展示使用的图片地址
    :param downloader: 下载器配置名称
    :param download_hash: 下载器中的任务哈希标识
    :param status: 本次整理是否成功，默认 True
    :param errmsg: 整理失败的可读错误说明；无错误时为 None
    :param failure_stage: 整理失败发生的业务阶段
    :param recovery_action: 允许用户或后台执行的恢复动作
    :param retry_count: 关联任务累计的自动重试次数，持久化后用于跨重启恢复重试预算
    :param auto_paused: 是否因自动恢复策略暂停任务
    :param cleanup_status: 媒体入库后下载器清理的当前状态
    :param cleanup_error: 下载器清理失败的错误说明
    :param files: 本次整理关联文件清单的JSON 快照
    :param episode_group: 当前媒体使用的剧集组标识
    """

    src: str
    src_storage: Optional[str] = None
    src_fileitem: Optional[JsonData] = None
    dest: Optional[str] = None
    dest_storage: Optional[str] = None
    dest_fileitem: Optional[JsonData] = None
    mode: Optional[str] = None
    type: Optional[str] = None
    media_category_id: Optional[str] = None
    category: Optional[str] = None
    classification_rule_id: Optional[str] = None
    classification_policy_revision: Optional[int] = None
    classification_source: Optional[str] = None
    title: Optional[str] = None
    year: Optional[str] = None
    media_source: Optional[MediaSource] = None
    media_id: Optional[str] = None
    music_type: Optional[str] = None
    total_tracks: Optional[int] = None
    audio_format: Optional[str] = None
    audio_lossless: Optional[bool] = None
    bit_depth: Optional[int] = None
    sample_rate: Optional[int] = None
    bitrate: Optional[int] = None
    seasons: Optional[str] = None
    episodes: Optional[str] = None
    image: Optional[str] = None
    downloader: Optional[str] = None
    download_hash: Optional[str] = None
    status: bool = True
    errmsg: Optional[str] = None
    failure_stage: Optional[str] = None
    recovery_action: Optional[str] = None
    retry_count: Optional[int] = None
    auto_paused: bool = False
    cleanup_status: Optional[str] = None
    cleanup_error: Optional[str] = None
    files: Optional[JsonData] = None
    episode_group: Optional[str] = None

    def to_payload(self) -> dict[str, object]:
        """
        返回仅含持久化字段的独立副本。
        """
        payload: dict[str, object] = asdict(self)
        if self.media_source is not None:
            payload["media_source"] = str(self.media_source)
        return payload


@dataclass(frozen=True, slots=True)
class TransferHistoryStatisticSnapshot:
    """
    单日整理历史数量统计。

    :param date: 按日统计对应的日期文本
    :param count: 对应日期的整理记录数量
    """

    date: str
    count: int


@dataclass(frozen=True, slots=True)
class TransferHistoryMonthlyStatistics:
    """
    本月按媒体类别聚合的整理历史数量。

    :param movies: 本月整理的电影数量
    :param tv_shows: 本月整理的电视剧数量
    :param episodes: 本月整理的电视剧单集数量
    :param music: 本月整理的音乐数量
    """

    movies: int
    tv_shows: int
    episodes: int
    music: int


class TransferHistoryQueryPort(Protocol):
    """
    宿主整理、历史、Agent 和工作流所需的类型化查询端口。
    """

    def get(self, history_id: int) -> Optional[TransferHistorySnapshot]:
        """
        按主键返回整理历史快照。

        :param history_id: 目标历史记录的数据库主键
        :return: 匹配的整理历史快照；不存在时返回 None
        """
        ...

    def get_by_src(
        self,
        src: str,
        storage: Optional[str] = None,
    ) -> Optional[TransferHistorySnapshot]:
        """
        按源路径和可选存储返回最新历史快照。

        :param src: 源文件或目录路径
        :param storage: 存储配置标识；None 表示不限定存储
        :return: 匹配源路径的最新整理历史；不存在时返回 None
        """
        ...

    def get_success_by_src(
        self,
        src: str,
        storage: Optional[str] = None,
    ) -> Optional[TransferHistorySnapshot]:
        """
        按源路径和可选存储返回最新成功历史快照。

        :param src: 源文件或目录路径
        :param storage: 存储配置标识；None 表示不限定存储
        :return: 匹配源路径的最新成功整理历史；不存在时返回 None
        """
        ...

    def get_by_dest(
        self,
        dest: str,
        storage: Optional[str] = None,
    ) -> Optional[TransferHistorySnapshot]:
        """
        按目标路径和可选存储返回最新历史快照。

        :param dest: 目标文件或目录路径
        :param storage: 存储配置标识；None 表示不限定存储
        :return: 匹配目标路径的最新整理历史；不存在时返回 None
        """
        ...

    def get_by_transfer_task_id(
        self,
        *,
        task_id: str,
    ) -> Optional[TransferHistorySnapshot]:
        """
        按持久整理任务标识返回终态历史快照。

        :param task_id: 持久整理任务的稳定标识
        :return: 该持久任务的终态历史；未结算历史时返回 None
        """
        ...

    async def async_get_by_transfer_task_id(
        self,
        *,
        task_id: str,
    ) -> Optional[TransferHistorySnapshot]:
        """
        异步按持久整理任务标识返回终态历史快照。

        :param task_id: 持久整理任务的稳定标识
        :return: 该持久任务的终态历史；未结算历史时返回 None
        """
        ...

    def get_by_media_identity(
        self,
        media_source: MediaSource,
        media_id: str,
        mtype: Optional[str] = None,
    ) -> Optional[TransferHistorySnapshot]:
        """
        按规范媒体身份和可选媒体类型返回历史快照。

        :param media_source: 标准媒体来源，与 media_id 配对使用
        :param media_id: 当前来源中的原生媒体标识
        :param mtype: 媒体类型筛选；None 表示不限类型
        :return: 匹配规范媒体身份和可选类型的历史快照；不存在时返回 None
        """
        ...

    def list_success_by_src(
        self,
        src: str,
        storage: Optional[str] = None,
        recursive: bool = False,
    ) -> list[TransferHistorySnapshot]:
        """
        按源路径返回成功整理历史快照。

        :param src: 源文件或目录路径
        :param storage: 存储配置标识；None 表示不限定存储
        :param recursive: 是否同时匹配指定目录的下级路径
        :return: 指定源路径范围内成功整理的历史列表
        """
        ...

    def list_success_move_by_dest(
        self,
        dest: str,
        storage: Optional[str] = None,
        recursive: bool = False,
    ) -> list[TransferHistorySnapshot]:
        """
        按目标路径返回成功移动历史快照。

        :param dest: 目标文件或目录路径
        :param storage: 存储配置标识；None 表示不限定存储
        :param recursive: 是否同时匹配指定目录的下级路径
        :return: 指定目标路径范围内成功移动的历史列表
        """
        ...

    def list_by_hash(self, download_hash: str) -> list[TransferHistorySnapshot]:
        """
        按下载任务 Hash 返回历史快照。

        :param download_hash: 下载器中的任务哈希标识
        :return: 关联下载任务的整理历史列表
        """
        ...

    async def async_get(
        self,
        history_id: int,
    ) -> Optional[TransferHistorySnapshot]:
        """
        异步按主键返回整理历史快照。

        :param history_id: 目标历史记录的数据库主键
        :return: 匹配的整理历史快照；不存在时返回 None
        """
        ...

    async def async_list_by_title(
        self,
        title: str,
        page: int = 1,
        count: int = 30,
        status: Optional[bool] = None,
        wildcard: bool = False,
    ) -> list[TransferHistorySnapshot]:
        """
        异步按标题或路径分页返回历史快照。

        :param title: 用于匹配媒体标题或文件路径的查询文本
        :param page: 页码，从 1 开始
        :param count: 每页最多返回的记录数
        :param status: 整理成功状态筛选；None 表示不限状态
        :param wildcard: 是否将查询文本按通配规则匹配
        :return: 匹配查询文本与状态的当前页整理历史
        """
        ...

    async def async_list_by_page(
        self,
        page: int = 1,
        count: int = 30,
        status: Optional[bool] = None,
    ) -> list[TransferHistorySnapshot]:
        """
        异步按时间倒序分页返回历史快照。

        :param page: 页码，从 1 开始
        :param count: 每页最多返回的记录数
        :param status: 整理成功状态筛选；None 表示不限状态
        :return: 按时间倒序排列且满足状态条件的当前页历史
        """
        ...

    async def async_count(self, status: Optional[bool] = None) -> int:
        """
        异步统计指定状态的整理历史数量。

        :param status: 整理成功状态筛选；None 表示不限状态
        :return: 满足成功状态筛选的整理历史数量
        """
        ...

    async def async_count_by_title(
        self,
        title: str,
        status: Optional[bool] = None,
        wildcard: bool = False,
    ) -> int:
        """
        异步统计匹配标题或路径的整理历史数量。

        :param title: 用于匹配媒体标题或文件路径的查询文本
        :param status: 整理成功状态筛选；None 表示不限状态
        :param wildcard: 是否将查询文本按通配规则匹配
        :return: 满足查询文本和状态筛选的整理历史数量
        """
        ...

    async def async_statistic(
        self,
        days: int = 7,
    ) -> list[TransferHistoryStatisticSnapshot]:
        """
        异步返回最近若干天的每日整理数量。

        :param days: 统计最近的天数，默认 7 天
        :return: 统计范围内按日期聚合的整理数量
        """
        ...

    def monthly_media_statistics(self) -> TransferHistoryMonthlyStatistics:
        """
        返回本月电影、剧集、单集和音乐整理数量。

        :return: 本月电影、电视剧、单集与音乐的分类数量
        """
        ...


class TransferHistoryWritePort(Protocol):
    """
    整理历史替换、删除与维护所需的类型化事务端口。
    """

    def replace(self, history: TransferHistoryWrite) -> TransferHistorySnapshot:
        """
        在独立事务中替换同源历史并返回快照。

        :param history: 待持久化的完整历史字段快照
        :return: 替换完成并提交后的独立历史快照
        """
        ...

    def delete(self, history_id: int) -> None:
        """
        在独立事务中删除一条旧历史。

        :param history_id: 目标历史记录的数据库主键
        """
        ...

    async def async_delete(self, history_id: int) -> None:
        """
        在独立异步事务中删除一条旧历史。

        :param history_id: 目标历史记录的数据库主键
        """
        ...

    def truncate(self) -> None:
        """
        在独立事务中清空没有持久任务映射的历史。
        """
        ...

    def update_download_hash(self, history_id: int, download_hash: str) -> None:
        """
        在独立事务中补充整理历史的下载任务 Hash。

        :param history_id: 目标历史记录的数据库主键
        :param download_hash: 下载器中的任务哈希标识
        """
        ...

    def update_cleanup_status(
            self,
            history_id: int,
            status: str,
            error: Optional[str] = None,
    ) -> None:
        """
        在独立事务中记录媒体入库后的下载器清理结果。

        :param history_id: 目标历史记录的数据库主键
        :param status: 拟写入的下载器清理状态
        :param error: 下载器清理失败的可读说明；无错误时为 None
        """
        ...


class TransferHistoryReplacePort(Protocol):
    """
    整理历史业务写入规则需要的最小替换端口。
    """

    def replace(self, history: TransferHistoryWrite) -> TransferHistorySnapshot:
        """
        替换同源历史并返回冻结快照。

        :param history: 待持久化的完整历史字段快照
        :return: 替换后的冻结历史快照
        """
        ...


class TransferHistoryStagingPort(TransferHistoryReplacePort, Protocol):
    """
    持久整理结算事务内查询并替换历史的暂存端口。

    查询与替换使用调用方的同一事务，不自行提交或回滚。
    """

    def get_by_src(
        self,
        src: str,
        storage: Optional[str] = None,
    ) -> Optional[TransferHistorySnapshot]:
        """
        在调用方 Session 内按源路径读取历史快照。

        :param src: 源文件或目录路径
        :param storage: 存储配置标识；None 表示不限定存储
        :return: 调用方事务中读取的匹配历史；不存在时返回 None
        """
        ...

    def get_success_by_src(
        self,
        src: str,
        storage: Optional[str] = None,
    ) -> Optional[TransferHistorySnapshot]:
        """
        在调用方 Session 内按源路径读取成功历史快照。

        :param src: 源文件或目录路径
        :param storage: 存储配置标识；None 表示不限定存储
        :return: 调用方事务中读取的成功历史；不存在时返回 None
        """
        ...


class TransferHistoryRepository(
    TransferHistoryQueryPort,
    TransferHistoryWritePort,
    Protocol,
):
    """
    组合宿主所需全部整理历史查询和变更能力。
    """


_configured_transfer_history_repository: (
    Callable[[], TransferHistoryRepository] | None
) = None


def configure_transfer_history_repository(
    provider: Callable[[], TransferHistoryRepository],
) -> None:
    """
    由启动组合根登记类型化整理历史仓储提供器。
    """
    global _configured_transfer_history_repository
    _configured_transfer_history_repository = provider


def reset_transfer_history_repository() -> None:
    """
    清除当前 lifespan 的整理历史仓储提供器。
    """
    global _configured_transfer_history_repository
    _configured_transfer_history_repository = None


def get_transfer_history_repository() -> TransferHistoryRepository:
    """
    返回启动组合根登记的类型化整理历史仓储。
    """
    if _configured_transfer_history_repository is None:
        raise RuntimeError("类型化整理历史仓储尚未配置")
    return _configured_transfer_history_repository()
