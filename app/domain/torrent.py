"""
种子身份、下载链接与剧集文件选择的纯领域判断规则。
"""

import base64
import binascii
import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Union
from urllib.parse import parse_qs, urlsplit

from app.domain.metainfo import MetaInfo
from app.schemas.download import DownloadTaskAction, ExistingTaskRejection

if TYPE_CHECKING:
    from app.domain.context import Context, TorrentInfo


@dataclass(frozen=True)
class EpisodeSelection:
    """
    下载目标季与识别阶段已经确认的季集对应关系，不推算未确认的集数。

    :param season: 目标季号，0 表示特别篇
    :param source_season: 种子标题识别修正前的季号；None 表示未标季
    :param episode_map: 标题原集号与目标集号的对应项；空元组表示没有额外换算依据
    """

    season: int
    source_season: Optional[int] = None
    episode_map: tuple[tuple[int, int], ...] = ()


def episode_selection_for_context(context: "Context") -> Optional[EpisodeSelection]:
    """
    从单季候选冻结季集依据，只有等长的明确集号列表才建立逐集对应关系。

    不根据目标季号或单个差值外推整包偏移。来源识别词由文件解析自行应用，
    此处保留应用来源临时词之前的标题坐标，避免标题专用词无法命中文件名。
    """
    meta, torrent = context.meta_info, context.torrent_info
    if meta is None or len(meta.season_list) != 1:
        return None
    season = meta.season_list[0]
    original = MetaInfo(torrent.title or "", torrent.description) if torrent else None
    source_season = original.begin_season if original else None
    source_episodes = original.episode_list if original else []
    target_episodes = meta.episode_list
    mapping: tuple[tuple[int, int], ...] = ()
    if original and len(original.season_list) == 1 and source_episodes and len(source_episodes) == len(target_episodes) and (
            source_season != season or source_episodes != target_episodes
    ):
        mapping = tuple(zip(source_episodes, target_episodes))
    return EpisodeSelection(season, source_season, mapping)


def _file_episode_coordinates(
        name: str, custom_words: Optional[list[str]], selection: Optional[EpisodeSelection],
) -> tuple[Optional[set[int]], bool]:
    """
    解析文件并应用明确映射；None 表示季集冲突，布尔值区分无集号附件。
    """
    original = MetaInfo(Path(name).name)
    meta = MetaInfo(Path(name).name, custom_words=custom_words) if custom_words else original
    episodes = set(meta.episode_list)
    numbered = bool(episodes)
    if selection is None:
        return episodes, numbered
    corrected = (meta.begin_season, meta.episode_list) != (original.begin_season, original.episode_list)
    if not corrected and selection.episode_map and original.begin_season in (None, selection.source_season):
        mapping = dict(selection.episode_map)
        mapped = {mapping[episode] for episode in episodes if episode in mapping}
        return mapped or None, numbered
    if meta.begin_season is not None and meta.season_list != [selection.season]:
        return None, numbered
    return episodes, numbered


def torrent_file_episodes(
        name: str, custom_words: Optional[list[str]] = None,
        episode_selection: Optional[EpisodeSelection] = None,
) -> set[int]:
    """
    返回文件在目标季中的已确认集号，供下载候选检查复用；冲突不贡献集数。
    """
    episodes, _numbered = _file_episode_coordinates(name, custom_words, episode_selection)
    return episodes or set()


def select_torrent_files(
        files: Iterable[tuple[int, str]], episodes: set[int], media_extensions: Iterable[str],
        custom_words: Optional[str] = None, episode_selection: Optional[EpisodeSelection] = None,
        *, allow_single_fallback: bool = True,
) -> tuple[list[int], list[int], set[int]]:
    """
    按目标季和已确认映射选择真实文件，下载器与历史结算共用相同规则。

    :param files: 下载器文件索引和路径；仅解析文件名，父目录不作为季集依据
    :param episodes: 必须由视频完整覆盖的目标集号
    :param media_extensions: 视频后缀；历史调用方可传入其媒体后缀集合
    :param custom_words: 来源临时识别词，先于明确映射应用，避免重复换算
    :param episode_selection: 冻结的目标季与映射；None 保留旧调用方的集号选集行为
    :param allow_single_fallback: 历史去重后不能用剩余唯一文件冒充原始单文件种子
    :return: 需要下载的索引、不下载的索引和已选视频覆盖的目标集号
    """
    extensions = {extension.lower() for extension in media_extensions}
    words = custom_words.splitlines() if custom_words else None
    entries: list[tuple[int, set[int], bool]] = []
    excluded: set[int] = set()
    for file_id, name in files:
        is_video = Path(name).suffix.lower() in extensions
        mapped, numbered = _file_episode_coordinates(name, words, episode_selection)
        if mapped is None and (is_video or numbered):
            excluded.add(file_id)
        entries.append((file_id, mapped or set(), is_video))
    return select_episode_files(entries, episodes, excluded=excluded, allow_single_fallback=allow_single_fallback)


def select_episode_files(
    files: Iterable[tuple[int, set[int], bool]], episodes: set[int],
    *, excluded: Optional[set[int]] = None, allow_single_fallback: bool = True,
) -> tuple[list[int], list[int], set[int]]:
    """
    按视频覆盖集数选择文件，保留无集号的非视频附属文件。

    多集文件与目标集有交集即选中，允许一并下载同文件中的其它集。仅请求一集且
    只有一个单集或无集号视频时，沿用识别修正后的唯一视频兜底；附属文件不证明视频覆盖。

    :param files: 文件索引、已按来源识别词解析的集数、是否为视频组成的条目
    :param episodes: 本次需要完整覆盖的目标集数
    :param excluded: 季集依据冲突的文件，不能参与匹配或唯一视频兜底
    :param allow_single_fallback: 是否允许唯一视频兜底
    :return: 需要下载的索引、不下载的索引、已选视频覆盖的集数；输入条目不会被修改
    """
    wanted: list[int] = []
    unwanted: list[int] = []
    matched: set[int] = set()
    media_files: list[tuple[int, set[int]]] = []
    for file_id, file_episodes, is_video in files:
        if is_video:
            media_files.append((file_id, file_episodes))
        if excluded and file_id in excluded:
            unwanted.append(file_id)
        elif file_episodes.intersection(episodes):
            wanted.append(file_id)
            if is_video:
                matched.update(file_episodes)
        elif not file_episodes and not is_video:
            wanted.append(file_id)
        else:
            unwanted.append(file_id)
    if allow_single_fallback and not episodes.issubset(matched) and len(episodes) == 1 and len(media_files) == 1:
        file_id, file_episodes = media_files[0]
        # 无交集的多集视频不能用唯一视频规则冒充目标单集。
        if len(file_episodes) <= 1 and (not excluded or file_id not in excluded):
            unwanted.remove(file_id)
            wanted.append(file_id)
            matched.update(episodes)
    return wanted, unwanted, matched


# ---------------------------------------------------------------------------
# 已有任务：MP 确认选中过的文件
#
# 下载器里"取消选择"和"从未选中"是同一个状态（qB/rT 优先级为 0，TR 未选中），只看下载器无法区分。
# MP 在每次受理成功后，把"本次在该任务上确认选中的目标主文件"累积写进下载历史的 note；之后同一任务上
# 某个目标主文件不再下载，而它在这份记录里，就说明是被别人取消的，订阅不会重新选中它。
#
# 记录的是文件身份（种子内的文件索引 + 相对路径），按下载器分开，而不是集号：
# - 集号要靠当时的季号、识别词、季集映射才能对应到文件，换一个季、改一次识别词，同样的集号就会
#   对应到别的文件（例如多季包里 S01E01 选过，S02E01 会被误判为"选过又被取消"）；
# - 同一种子可能同时在多台下载器里，各台的选择互不相干。
# 已知近似：
# - 文件身份由下载链从种子文件按与下载器相同的选集规则算出（不额外查询下载器）。磁力链接没有种子文件，
#   记不下文件身份，这类任务不识别"被取消"；
# - 带填充文件（BEP 47）的种子，qBittorrent 的文件索引会跳过填充文件，与种子内顺序对不上，对不上的文件
#   按"从未选中"处理（宁可重新选中，也不误拒正常请求）；
# - 任务在 MP 之外被删除后又在 MP 之外重新添加时，旧记录仍然有效，新任务上从未选中的文件会被当成
#   "被取消"而拒绝（订阅换种子；用户手动指定该种子时不受影响）。
# ---------------------------------------------------------------------------

CONFIRMED_SELECTION_NOTE_KEY = "confirmed_selection"
# note 中确认记录的格式版本；读取时版本不符一律按"没有记录"处理。
_CONFIRMED_SELECTION_VERSION = 2


def _path_parts(name: str) -> tuple[str, ...]:
    """
    把种子内路径或下载器报告的文件名拆成路径分段，兼容正反斜杠，忽略空段。
    """
    return tuple(part for part in str(name or "").replace("\\", "/").split("/") if part)


@dataclass(frozen=True)
class ConfirmedFile:
    """
    MP 在某个下载任务上确认选中过的一个目标主文件。

    :param index: 文件在种子内的顺序号（从 0 开始），对应下载器的文件索引
    :param path: 文件在种子内的相对路径分段，不含多文件种子的根目录；单文件种子为文件名
    """

    index: int
    path: tuple[str, ...]

    def matches(self, file_id: int, name: str) -> bool:
        """
        判断下载器报告的文件是否就是这个文件：索引相同，且路径相同或只多出最前面一层目录。

        各下载器报告的文件名前缀不同：qBittorrent"原始"布局与 Transmission 带种子根目录，qBittorrent
        "不创建子文件夹"布局与 rTorrent 不带根目录，qBittorrent"创建子文件夹"布局会给单文件种子加一层目录。
        同时要求索引相同，避免不同目录下的同名文件互相冒认。

        :param file_id: 下载器内的文件索引
        :param name: 下载器报告的文件相对路径
        :return: 是同一个文件时返回 True
        """
        if file_id != self.index:
            return False
        parts = _path_parts(name)
        return parts == self.path or parts[1:] == self.path


@dataclass(frozen=True)
class ConfirmedSelection:
    """
    MP 在同一种子（同一 hash）的各台下载器任务上确认选中过的目标主文件，按下载器名称分开记录。

    :param downloaders: 下载器配置名称到确认文件的映射；没有记录的下载器视为从未确认选中任何文件
    """

    downloaders: tuple[tuple[str, frozenset[ConfirmedFile]], ...] = ()

    def files_for(self, downloader: str) -> frozenset[ConfirmedFile]:
        """
        取出某台下载器上的确认文件。

        :param downloader: 下载器配置名称
        :return: 确认文件；没有记录时为空集合
        """
        return next((files for name, files in self.downloaders if name == downloader), frozenset())

    def record(self, downloader: str, files: frozenset[ConfirmedFile], *, replace: bool) -> "ConfirmedSelection":
        """
        记录某台下载器上本次确认选中的文件，其他下载器的记录保持不变。

        :param downloader: 本次受理的下载器配置名称
        :param files: 本次确认选中的目标主文件
        :param replace: 为 True 时替换该下载器的旧记录（新建任务：同 hash 的旧任务可能已被删除后重新添加，
            旧记录里的文件在新任务上从未选中），否则与旧记录合并（在已有任务上追加）
        :return: 新的确认记录
        """
        merged = files if replace else self.files_for(downloader) | files
        others = tuple((name, items) for name, items in self.downloaders if name != downloader)
        return ConfirmedSelection(downloaders=tuple(sorted(others + ((downloader, merged),), key=lambda item: item[0])))

    def to_note(self) -> dict[str, Any]:
        """
        转为写入下载历史 note 的 JSON 值。
        """
        return {
            "version": _CONFIRMED_SELECTION_VERSION,
            "downloaders": {
                name: [{"index": item.index, "path": list(item.path)} for item in sorted(files, key=lambda f: f.index)]
                for name, files in self.downloaders
            },
        }

    @classmethod
    def from_note(cls, note: object) -> "ConfirmedSelection":
        """
        从下载历史 note 读取确认记录；没有记录、版本不符或格式不符时返回空记录（视为从未确认选中）。
        """
        payload = note.get(CONFIRMED_SELECTION_NOTE_KEY) if isinstance(note, dict) else None
        if not isinstance(payload, dict) or payload.get("version") != _CONFIRMED_SELECTION_VERSION:
            return cls()
        downloaders = payload.get("downloaders")
        if not isinstance(downloaders, dict):
            return cls()
        records: list[tuple[str, frozenset[ConfirmedFile]]] = []
        for name, items in downloaders.items():
            if not isinstance(name, str) or not isinstance(items, list):
                continue
            files = frozenset(
                ConfirmedFile(index=item["index"], path=tuple(item["path"]))
                for item in items
                if isinstance(item, dict)
                and isinstance(item.get("index"), int) and not isinstance(item.get("index"), bool)
                and isinstance(item.get("path"), list) and item["path"]
                and all(isinstance(part, str) and part for part in item["path"])
            )
            records.append((name, files))
        return cls(downloaders=tuple(sorted(records, key=lambda item: item[0])))


# ---------------------------------------------------------------------------
# 已有任务：受理决策
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExistingTaskFile:
    """
    下载器中已有任务的单个文件当前状态，由下载器模块从实时文件列表投影而来。

    :param file_id: 下载器内的文件索引，只在同一任务内有效
    :param name: 文件在种子内的相对路径；只解析文件名，父目录不作为季集依据
    :param wanted: 当前是否已设为需要下载（qB/rT 优先级大于 0，TR 为 selected）
    :param completed: 文件数据是否已全部下载完成；取消选择后仍保留数据的文件也算完成
    """

    file_id: int
    name: str
    wanted: bool
    completed: bool


class ExistingTaskState(str, Enum):
    """
    已有任务的运行状态，由各下载器模块从原始状态归一而来。
    """

    # 正在下载、排队、校验或做种，追加的文件会自然开始下载
    RUNNING = "running"
    # 已暂停或停止（qB paused*/stopped*、TR stopped、rT 停止或暂停）
    PAUSED = "paused"
    # 下载器报告错误（qB error/missingFiles、TR 本地错误），数据不可信
    ERROR = "error"
    # 读取失败或无法识别
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ExistingTaskSnapshot:
    """
    做受理决策所需的已有任务实时状态，由下载器模块一次读取后交给 ``decide_existing_task``。

    :param files: 实时文件列表；为空表示读取失败或尚无元数据
    :param state: 归一后的运行状态
    """

    files: tuple[ExistingTaskFile, ...]
    state: ExistingTaskState


@dataclass(frozen=True)
class ExistingTaskDecision:
    """
    已有任务能否受理本次请求，以及受理时下载器模块要做的最小改动。

    只描述"追加"，从不包含"取消选择""暂停"或"恢复"：已有任务上的其他文件、优先级和启停状态都保持原样（#7362）。

    :param action: 受理时下载器对任务的动作；拒绝时为 None
    :param rejection: 拒绝原因；受理时为 None
    :param detail: 拒绝时面向用户的细节与处理建议
    :param enable: 需要从不下载改为下载的文件索引
    :param required: 受理后必须处于选中状态的全部目标文件索引（含本次追加的），写入后据此回读确认
    :param pending: 目标中是否仍有未下载完成的文件；为 True 时 rTorrent 需要刷新优先级
    """

    action: Optional[DownloadTaskAction] = None
    rejection: Optional[ExistingTaskRejection] = None
    detail: str = ""
    enable: tuple[int, ...] = ()
    required: tuple[int, ...] = ()
    pending: bool = False


def _reject(rejection: ExistingTaskRejection, detail: str) -> ExistingTaskDecision:
    """
    构造拒绝决策；拒绝时下载器模块不得对任务做任何改动。
    """
    return ExistingTaskDecision(rejection=rejection, detail=detail)


def _is_media_file(name: str, media_extensions: set[str]) -> bool:
    """
    按后缀判断是否为视频文件。
    """
    return Path(name).suffix.lower() in media_extensions


def _whole_torrent_targets(files: dict[int, ExistingTaskFile], media_extensions: set[str]) -> list[int]:
    """
    不带集数的请求在已有任务上的目标：全部视频文件；没有视频时为全部文件。

    新任务整包添加时下载器默认选中全部文件，但已有任务上字幕、样片等非视频文件可能是用户特意
    取消的，这里只确保视频都已选中，不去碰非视频文件。
    """
    media = [file_id for file_id, item in files.items() if _is_media_file(item.name, media_extensions)]
    return media or list(files)


def _episode_targets(
        files: dict[int, ExistingTaskFile], episodes: set[int], media_extensions: set[str],
        custom_words: Optional[str], episode_selection: Optional[EpisodeSelection],
) -> tuple[list[int], set[int]]:
    """
    按与新任务选集完全相同的规则（``select_torrent_files``）找出目标集对应的文件。

    :return: 目标文件索引与被视频覆盖到的集号
    """
    wanted, _unwanted, matched = select_torrent_files(
        ((file_id, item.name) for file_id, item in files.items()),
        episodes, media_extensions, custom_words, episode_selection,
    )
    return wanted, matched


def _request_targets(
        files: dict[int, ExistingTaskFile], episodes: Optional[set[int]], media_extensions: set[str],
        custom_words: Optional[str], episode_selection: Optional[EpisodeSelection],
) -> tuple[list[int], set[int], set[int]]:
    """
    计算一次请求在文件列表上的目标文件与主文件，新任务记录与已有任务决策共用。

    主文件：有视频的种子是全部视频，没有视频的种子（例如音乐专辑）是全部文件，与整包目标的口径一致。

    :return: 目标文件索引、被视频覆盖到的集号（整包请求为空集合）和主文件索引
    """
    primary = set(_whole_torrent_targets(files, media_extensions))
    if episodes:
        target, matched = _episode_targets(files, episodes, media_extensions, custom_words, episode_selection)
        return target, matched, primary
    return sorted(primary), set(), primary


def confirmed_target_files(
        files: Iterable[str], *, episodes: Optional[set[int]], media_extensions: Iterable[str],
        custom_words: Optional[str] = None, episode_selection: Optional[EpisodeSelection] = None,
) -> frozenset[ConfirmedFile]:
    """
    按与下载器完全相同的选集规则，从种子文件清单算出本次受理确认选中的目标主文件。

    下载器只有在目标确认选中后才返回 hash，因此由同一规则算出的目标主文件就是这次确认选中的文件。
    只记主文件：被取消的字幕等附属文件在决策中本来就静默跳过，不需要记录。

    :param files: 种子内文件的相对路径，按种子内顺序排列（下标即文件索引）；磁力链接为空
    :param episodes: 本次目标集号；None 或空集合表示整个种子
    :param media_extensions: 视频后缀集合
    :param custom_words: 来源识别词，与下载器选集使用同一份
    :param episode_selection: 目标季与已确认的季集映射，与下载器选集使用同一份
    :return: 确认选中的目标主文件；目标集对应不上（不应发生）或没有文件时为空集合
    """
    indexed = {index: ExistingTaskFile(index, name, True, False) for index, name in enumerate(files)}
    if not indexed:
        return frozenset()
    extensions = {str(extension).lower() for extension in media_extensions}
    target, matched, primary = _request_targets(indexed, episodes, extensions, custom_words, episode_selection)
    if episodes and not episodes.issubset(matched):
        return frozenset()
    return frozenset(
        ConfirmedFile(index=file_id, path=_path_parts(indexed[file_id].name))
        for file_id in target if file_id in primary and _path_parts(indexed[file_id].name)
    )


def _decide_pending_task(
        snapshot: ExistingTaskSnapshot, enable: tuple[int, ...], required: tuple[int, ...],
) -> ExistingTaskDecision:
    """
    目标仍有未下载完成的文件时，按任务运行状态决定能否受理。

    MoviePilot 从不替别人恢复任务，也不往暂停的任务里追加：
    - 运行中：追加后自然开始下载，受理；
    - 已暂停：不论是谁暂停的（MP 里的用户、插件、下载器界面、分享率规则），都不恢复、不改动，拒绝；
      订阅当轮改用其他资源，并只对这台下载器里的这个任务短冷却。暂停方恢复任务后，下次就能接着使用；
    - 状态读不到：拒绝，稍后重试。
    """
    if snapshot.state is ExistingTaskState.RUNNING:
        return ExistingTaskDecision(
            action=DownloadTaskAction.APPENDED if enable else DownloadTaskAction.REUSED,
            enable=enable, required=required, pending=True,
        )
    if snapshot.state is ExistingTaskState.PAUSED:
        return _reject(
            ExistingTaskRejection.PAUSED,
            "任务已暂停，目标文件尚未下载完成；MP 不恢复也不改动暂停的任务，本次改用其他资源。"
            "如需继续使用该任务，请先在下载器或 MoviePilot 下载管理中恢复",
        )
    return _reject(ExistingTaskRejection.STATE_UNAVAILABLE, "读取任务运行状态失败，未改动该任务，稍后重试")


def decide_existing_task(
        snapshot: ExistingTaskSnapshot, *, episodes: Optional[set[int]], confirmed: Optional[frozenset[ConfirmedFile]],
        media_extensions: Iterable[str], custom_words: Optional[str] = None,
        episode_selection: Optional[EpisodeSelection] = None,
) -> ExistingTaskDecision:
    """
    同一 hash 已在下载器中时，判断能否受理本次请求，以及需要追加选择哪些文件。

    同一个整季包可以被多次请求不同的集（试看 E01、再要 E02、最后追整季），每次请求都要在原任务上
    追加本次目标，而不是把已有任务原样当成"已受理"（#7405 的问题）。决策顺序：
    1. 读不到文件列表，或任务出错：拒绝，不改动；
    2. 目标集找不到对应视频：拒绝；
    3. 目标主文件（视频；没有视频的种子为全部文件）曾被 MP 确认选中、现在被取消：拒绝，不重新选中
       （别人取消的）；被取消的字幕等附属文件静默跳过；
    4. 目标已全部下载完成：受理，不论任务是否暂停都不改启停状态；
    5. 否则按任务运行状态决定，见 ``_decide_pending_task``。

    目标文件的匹配规则与新任务选集完全相同（``select_torrent_files``），区别只在于：新任务会把
    非目标文件设为不下载，已有任务绝不取消任何文件的选择。

    :param snapshot: 下载器实时读取的任务状态；不能用下载历史或种子文件代替
    :param episodes: 本次目标集号；None 或空集合表示整个种子，目标为全部视频文件
    :param confirmed: MP 此前在这台下载器的该任务上确认选中过的文件。None 表示本次不识别"被取消"：
        MP 没有该 hash 的下载历史（外部添加的任务），或用户手动指定了这个种子（明确要下载，覆盖之前的取消）
    :param media_extensions: 视频后缀集合
    :param custom_words: 来源识别词，与新任务选集使用同一份
    :param episode_selection: 目标季与已确认的季集映射，与新任务选集使用同一份
    :return: 受理或拒绝决策
    """
    files = {item.file_id: item for item in snapshot.files}
    if not files:
        return _reject(ExistingTaskRejection.FILES_UNAVAILABLE, "读取文件列表失败（磁力任务可能尚未获取元数据），未改动该任务，稍后重试")
    if snapshot.state is ExistingTaskState.ERROR:
        return _reject(
            ExistingTaskRejection.TASK_ERROR,
            "下载器报告该任务出错（例如数据文件缺失），未改动该任务，本次改用其他资源；请先在下载器中处理",
        )
    extensions = {str(extension).lower() for extension in media_extensions}
    target, matched, primary = _request_targets(files, episodes, extensions, custom_words, episode_selection)
    if episodes and not episodes.issubset(matched):
        return _reject(
            ExistingTaskRejection.EPISODES_NOT_FOUND,
            f"未找到能覆盖目标集数 {sorted(episodes)} 的视频，未改动该任务，请检查季集映射",
        )
    # 确认记录存的是文件身份（索引 + 种子内路径），直接与下载器报告的文件比对，不按当前请求的季集规则重新换算。
    previously_confirmed = {
        file_id for file_id, item in files.items()
        if any(record.matches(file_id, item.name) for record in confirmed or ())
    }
    cancelled = {file_id for file_id in target if file_id in previously_confirmed and not files[file_id].wanted}
    # 只按"视频"判断主文件的话，纯音频种子的音轨全部被取消后会被判成"目标已完成"而复用成功，缺轨记成已下载。
    cancelled_primary = sorted(files[file_id].name for file_id in cancelled if file_id in primary)
    if cancelled_primary:
        return _reject(
            ExistingTaskRejection.FILES_CANCELLED,
            f"目标文件 {'、'.join(cancelled_primary)} 曾由 MoviePilot 选中，现已在下载器中被取消选择，"
            "MP 不会重新选中，本次改用其他资源；如确实需要，请手动指定该种子下载",
        )
    # 追加范围：目标中尚未选中、且不是被别人取消的文件。被取消的只可能是非主文件（字幕等附属文件，主文件已在
    # 上面拒绝），静默跳过，不重新选中，也不计入待下载。
    # 外部添加的任务（confirmed 为 None）没有确认记录，按"从未选中"允许追加，与升级前的旧任务一致。
    # 如果以后发现往外部任务里追加文件会打乱用户在下载器中的安排，只需在这里对 confirmed is None
    # 返回拒绝（或清空 enable），其余流程不受影响；注意用户手动指定种子时 confirmed 也是 None。
    required = tuple(file_id for file_id in target if file_id not in cancelled)
    enable = tuple(file_id for file_id in required if not files[file_id].wanted)
    if all(files[file_id].completed for file_id in required):
        return ExistingTaskDecision(
            action=DownloadTaskAction.APPENDED if enable else DownloadTaskAction.REUSED,
            enable=enable, required=required,
        )
    return _decide_pending_task(snapshot, enable, required)


def is_magnet_link(content: Union[str, bytes]) -> bool:
    """
    判断字符串或字节内容是否为磁力链接。
    """
    if not content:
        return False
    if isinstance(content, str):
        return content.startswith("magnet:")
    if isinstance(content, bytes):
        return content.startswith(b"magnet:")
    return False


def _bencoded_value_end(content: bytes, offset: int) -> int:
    """
    跳过一个完整 bencode 值，保留 info 原始字节以避免重编码改变哈希。
    """
    marker = content[offset:offset + 1]
    if marker == b"i":
        end = content.index(b"e", offset + 1)
        if not re.fullmatch(rb"0|-?[1-9][0-9]*", content[offset + 1:end]):
            raise ValueError("bencode 整数无效")
        return end + 1
    if marker in (b"d", b"l"):
        offset += 1
        keys: set[bytes] = set()
        while content[offset:offset + 1] != b"e":
            if marker == b"d":
                if not content[offset:offset + 1].isdigit():
                    raise ValueError("bencode 字典键无效")
                separator = content.index(b":", offset)
                key_end = _bencoded_value_end(content, offset)
                key = content[separator + 1:key_end]
                if key in keys:
                    raise ValueError("bencode 字典键重复")
                keys.add(key)
                offset = key_end
            offset = _bencoded_value_end(content, offset)
        return offset + 1
    if marker.isdigit():
        separator = content.index(b":", offset)
        if not re.fullmatch(rb"0|[1-9][0-9]*", content[offset:separator]):
            raise ValueError("bencode 字符串长度无效")
        length = int(content[offset:separator])
        end = separator + 1 + length
        if end > len(content):
            raise ValueError("bencode 字符串不完整")
        return end
    raise ValueError("bencode 值无效")


def _bencoded_dictionary(content: bytes) -> dict[bytes, bytes]:
    """
    提取字典直属键值的原始切片，不解码二进制文件树或 pieces root。
    """
    if not content.startswith(b"d") or _bencoded_value_end(content, 0) != len(content):
        raise ValueError("bencode 字典无效")
    result: dict[bytes, bytes] = {}
    offset = 1
    while content[offset:offset + 1] != b"e":
        if not content[offset:offset + 1].isdigit():
            raise ValueError("bencode 字典键无效")
        separator = content.index(b":", offset)
        key_end = _bencoded_value_end(content, offset)
        key = content[separator + 1:key_end]
        value_end = _bencoded_value_end(content, key_end)
        if key in result:
            raise ValueError("bencode 字典键重复")
        result[key] = content[key_end:value_end]
        offset = value_end
    return result


def download_hashes(content: Union[str, bytes]) -> frozenset[str]:
    """
    读取实际种子或磁力的 v1/v2 身份；v2 同时接受下载器使用的 40 位截断哈希。

    哈希只由原始 info 字节或明确的磁力标识构造，名称和大小不构成身份。
    解析失败返回空集合，调用方不得据此接管其它已有任务。
    """
    try:
        hashes: set[str] = set()
        if is_magnet_link(content):
            url = content.decode("utf-8") if isinstance(content, bytes) else content
            for value in parse_qs(urlsplit(url).query).get("xt", []):
                if match := re.fullmatch(r"urn:btih:([a-fA-F0-9]{40})", value):
                    hashes.add(match.group(1).lower())
                elif match := re.fullmatch(r"urn:btih:([a-zA-Z2-7]{32})", value):
                    hashes.add(base64.b32decode(match.group(1).upper()).hex())
                elif match := re.fullmatch(r"urn:btmh:1220([a-fA-F0-9]{64})", value):
                    digest = match.group(1).lower()
                    hashes.update((digest, digest[:40]))
        elif isinstance(content, bytes):
            info = _bencoded_dictionary(content).get(b"info", b"")
            fields = _bencoded_dictionary(info)
            if b"pieces" in fields:
                hashes.add(hashlib.sha1(info).hexdigest())
            if fields.get(b"meta version") == b"i2e":
                digest = hashlib.sha256(info).hexdigest()
                hashes.update((digest, digest[:40]))
        return frozenset(hashes)
    except (ValueError, TypeError, UnicodeError, binascii.Error, RecursionError):
        return frozenset()


def resource_identity(torrent: "TorrentInfo") -> Optional[tuple[str, str]]:
    """
    按显式字段和公开查询参数匹配资源；专站路径由 Module 解释。

    :param torrent: 候选的站点、显式身份和公开定位地址
    :return: 站点及明确资源标识；标题、票据或含糊的地址不产生身份
    """
    site = str(torrent.site or torrent.site_name or "")
    for name, prefix in (("torrent_id", "id"), ("info_hash", "hash")):
        value = getattr(torrent, name, None)
        if site and isinstance(value, (str, int)) and value:
            return site, f"{prefix}={value}"
    for url in (torrent.page_url, torrent.enclosure):
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            continue
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        params = parse_qs(parsed.query)
        # Gazelle 详情页的 id 是分组 ID，torrentid 才对应可下载的具体资源。
        for name in ("torrentid", "torrent_id", "tid", "id", "hash"):
            values = params.get(name)
            if values and len(values) == 1:
                prefix = "hash" if name == "hash" else "id"
                return site or parsed.netloc, f"{prefix}={values[0]}"
    return None


def is_expiring_download_url(url: Optional[str]) -> bool:
    """
    识别已知的短时签名下载链接；不推断签名的具体有效期。

    只识别查询参数中的时间戳加签名（t、sign）；专站路径票据由 Module 解释。

    :param url: 下载地址或间接配方
    :return: 是否带有完整的通用时间戳和签名参数
    """
    if not url or not url.startswith(("http://", "https://")):
        return False
    try:
        parsed = urlsplit(url)
        params = parse_qs(parsed.query)
    except ValueError:
        return False
    return bool(params.get("t") and params.get("sign"))


def rate_limit_cooldown(message: Optional[str]) -> Optional[int]:
    """
    识别种子下载限流语义并返回冷却秒数；未知错误返回空。

    每日下载配额采用完整 24 小时以覆盖站点时区；明确的频控秒数按原值冷却。
    其他短期限流沿用至少一小时的订阅冷却，不在下载调用内立即重试。
    """
    text = str(message or "").lower()
    daily_quota = (
        re.search(r"[当當]天|每天|每日|今日|daily", text)
        and re.search(r"下载|下載|download", text)
        and re.search(r"最多|上限|限[额額]|次[数數]|limit|quota", text)
    )
    if daily_quota:
        return 86400
    frequency_limit = re.search(r"request frequency limit,?\s*please try in\s+(\d+)\s+seconds?", text)
    if frequency_limit:
        return max(1, int(frequency_limit.group(1)))
    keywords = ("限流", "流控", "请求太多", "请求过多", "请求频繁", "请求过于频繁",
                "請求太多", "請求過多", "請求頻繁", "請求過於頻繁", "too many requests", "rate limit")
    if not any(keyword in text for keyword in keywords):
        return None
    wait = re.search(r"(\d+)\s*(秒|分[钟鐘]?|小[时時]|seconds?|minutes?|hours?)", text)
    if not wait:
        return 3600
    unit = wait.group(2)
    multiplier = 3600 if unit.startswith(("小", "hour")) else (
        60 if unit.startswith(("分", "minute")) else 1
    )
    return max(3600, int(wait.group(1)) * multiplier)
