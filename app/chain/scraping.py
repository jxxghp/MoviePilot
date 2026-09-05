import os
import re
import threading
import time
from collections.abc import Iterator, Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from threading import Lock
from typing import Any, Iterable, List, Optional, Protocol, Self, Tuple, Union

from app.application.audio import AudioMetadataHelper
from app.application.configuration import (
    get_chain_runtime_config_snapshot,
    get_configured_system_config,
)
from app.chain.base import ChainBase
from app.chain.lyrics import LyricsChain
from app.chain.media import MediaChain
from app.chain.storage import StorageChain
from app.domain.context import (
    MediaInfo,
    MusicAlbumInfo,
    MusicInfo,
    MusicLyrics,
)
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.domain.metainfo import MetaInfo, MetaInfoPath
from app.foundation.singleton import Singleton
from app.runtime.cache import cached
from app.runtime.events import Event, eventmanager
from app.runtime.log import logger
from app.runtime.reload import ConfigReloadMixin
from app.schemas.media import resolve_media_identity
from app.schemas.types import (
    MUSIC_ENTITY_ALBUM,
    MUSIC_ENTITY_RECORDING,
    EventType,
    MediaSource,
    MediaType,
    ScrapingMetadata,
    ScrapingPolicy,
    ScrapingTarget,
    SystemConfigKey,
)
from app.schemas.workflow import FileItem
from app.schemas.workflow import FileItem as _SchemaFileItem


class ScrapingResponsePort(Protocol):
    """刮削链读取封面所需的最小同步 HTTP 响应契约。"""

    status_code: int
    content: bytes
    headers: Mapping[str, str]

    def close(self) -> None:
        """释放响应与连接资源。"""
        ...


class ScrapingStreamResponsePort(Protocol):
    """刮削链流式保存图片所需的最小响应契约。"""

    status_code: int

    def __enter__(self) -> Self:
        """进入响应所有权上下文。"""
        ...

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        """退出上下文并释放响应资源。"""
        ...

    def iter_content(self, *, chunk_size: int) -> Iterator[bytes]:
        """按固定块大小迭代响应字节。"""
        ...


class ScrapingHttpPort(Protocol):
    """刮削链下载普通封面与流式图片所需的同步 HTTP 端口。"""

    def get(
        self,
        url: str,
        *,
        proxies: Optional[dict[str, str]],
        ua: str,
        timeout: int,
    ) -> Optional[ScrapingResponsePort]:
        """读取小型封面响应。"""
        ...

    def stream(
        self,
        url: str,
        *,
        proxies: Optional[dict[str, str]],
        ua: str,
    ) -> ScrapingStreamResponsePort:
        """打开由上下文负责释放的流式图片响应。"""
        ...


_scraping_http_lock = threading.RLock()
_scraping_http_port: Optional[ScrapingHttpPort] = None


def configure_scraping_http_port(http: ScrapingHttpPort) -> Optional[ScrapingHttpPort]:
    """由启动组合根装配刮削 HTTP 端口，并返回旧实现。"""
    global _scraping_http_port
    with _scraping_http_lock:
        previous = _scraping_http_port
        _scraping_http_port = http
        return previous


def reset_scraping_http_port(http: Optional[ScrapingHttpPort] = None) -> None:
    """恢复指定刮削 HTTP 端口；省略参数时回到未装配状态。"""
    global _scraping_http_port
    with _scraping_http_lock:
        _scraping_http_port = http


def _scraping_http_snapshot() -> ScrapingHttpPort:
    """读取刮削 HTTP 端口快照，未装配时稳定失败。"""
    with _scraping_http_lock:
        http = _scraping_http_port
    if http is None:
        raise RuntimeError("刮削 HTTP 端口尚未由启动组合根装配")
    return http

scraping_lock = Lock()

current_umask = os.umask(0)
os.umask(current_umask)


@dataclass
class _MusicScrapeFileResult:
    """记录单个音轨的标签刮削结果和歌词处理状态。"""

    metadata_success: bool = True
    lyrics_status: str = "disabled"

class ScrapingOption:
    """刮削选项"""

    type: ScrapingTarget = ScrapingTarget.TV
    metadata: ScrapingMetadata = ScrapingMetadata.NFO
    policy: ScrapingPolicy = ScrapingPolicy.MISSINGONLY

    def __init__(
            self,
            type: Union[str, ScrapingTarget],
            metadata: Union[str, ScrapingMetadata],
            value: Union[ScrapingPolicy, bool, str],
    ):
        if isinstance(type, ScrapingTarget):
            self.type = type
        elif isinstance(type, str):
            self.type = ScrapingTarget(type)
        if isinstance(metadata, ScrapingMetadata):
            self.metadata = metadata
        elif isinstance(metadata, str):
            self.metadata = ScrapingMetadata(metadata)
        if isinstance(value, bool):
            # 兼容旧的布尔值格式
            self.policy = ScrapingPolicy.MISSINGONLY if value else ScrapingPolicy.SKIP
        elif isinstance(value, ScrapingPolicy):
            self.policy = value
        elif isinstance(value, str):
            self.policy = ScrapingPolicy(value)
        else:
            logger.error(
                f"无效的刮削选项：type={type}, metadata={metadata}, value={value}"
            )

    @property
    def is_skip(self) -> bool:
        """是否跳过"""
        return self.policy == ScrapingPolicy.SKIP

    @property
    def is_overwrite(self) -> bool:
        """是否覆盖模式"""
        return self.policy == ScrapingPolicy.OVERWRITE

    @property
    def is_upgrade(self) -> bool:
        """是否只在歌词等产物质量更高时替换。"""
        return self.policy == ScrapingPolicy.UPGRADE

class ScrapingConfig:
    """媒体刮削配置"""

    def __init__(self, config_dict: dict[str, str] = None):
        """
        初始化配置对象
        :param config_dict: 用户配置字典（扁平化格式），为 None 时使用默认配置
        """
        self._policies: dict[tuple[str, str], ScrapingOption] = {}
        # 合并用户配置和默认配置
        if config_dict is None:
            config_dict = {}

        # 以默认配置为基础，用用户配置覆盖
        _config = self.get_default_config()
        for key, value in config_dict.items():
            _config[key] = value

        for key, value in _config.items():
            if "_" in key:
                items = key.split("_", 1)
                self._policies[tuple(items)] = ScrapingOption(*items, value)

    def option(
            self, item: Union[str, ScrapingTarget], metadata: Union[str, ScrapingMetadata]
    ) -> ScrapingOption:

        if isinstance(item, ScrapingTarget):
            item = item.name.lower()
        if isinstance(metadata, ScrapingMetadata):
            metadata = metadata.name.lower()

        return self._policies.get(
            (item, metadata), ScrapingOption(item, metadata, ScrapingPolicy.SKIP)
        )

    @classmethod
    def from_system_config(cls) -> "ScrapingConfig":
        """
        从系统配置加载

        :return: MediaScrapingConfig 实例
        """
        user_config = get_configured_system_config().get(SystemConfigKey.ScrapingSwitchs) or {}
        return cls(user_config)

    @staticmethod
    def get_default_config() -> dict[str, str]:
        """获取默认配置字典"""
        config_items = [
            f"{mt}_{md}"
            for mt, mds in [
                (
                    "movie",
                    ["nfo", "poster", "backdrop", "logo", "disc", "banner", "thumb", "clearart", "landscape"],
                ),
                ("tv", ["nfo", "poster", "backdrop", "logo", "banner", "thumb", "clearart", "landscape"]),
                ("season", ["nfo", "poster", "backdrop", "banner", "thumb", "landscape"]),
                ("episode", ["nfo", "thumb"]),
                ("music", ["nfo", "poster", "lyrics"]),
            ]
            for md in mds
        ]
        defaults = {item: ScrapingPolicy.MISSINGONLY for item in config_items}
        defaults["music_lyrics"] = ScrapingPolicy.UPGRADE
        return defaults


class ScrapingChain(ChainBase, ConfigReloadMixin, metaclass=Singleton):
    """统一处理影视和音乐元数据刮削，不承担媒体识别职责。"""

    CONFIG_WATCH = {SystemConfigKey.ScrapingSwitchs.value}

    IMAGE_METADATA_MAP = {
        "poster": ScrapingMetadata.POSTER,
        "backdrop": ScrapingMetadata.BACKDROP,
        "fanart": ScrapingMetadata.BACKDROP,
        "background": ScrapingMetadata.BACKDROP,
        "logo": ScrapingMetadata.LOGO,
        "disc": ScrapingMetadata.DISC,
        "cdart": ScrapingMetadata.DISC,
        "banner": ScrapingMetadata.BANNER,
        "thumb": ScrapingMetadata.THUMB,
        "landscape": ScrapingMetadata.LANDSCAPE,
        "clearart": ScrapingMetadata.CLEARART,
    }

    IMAGE_ALIASES = {
        "backdrop": ["fanart"],
        "fanart": ["backdrop"],
        "thumb": ["landscape"],
        "landscape": ["thumb"],
    }

    MUSIC_LYRICS_EXTENSIONS = (".lyricsfile.yaml", ".lrc", ".txt")
    _music_track_prefix_pattern = re.compile(
        r"^\s*(?:(?:cd|disc)\s*\d+\s*[-_. ]+)?(?:\d+\s*[-_. ]+)+",
        flags=re.IGNORECASE,
    )

    def __init__(self):
        """初始化存储访问和当前刮削策略。"""
        super().__init__()
        self.storagechain = StorageChain()
        self.scraping_policies = ScrapingConfig.from_system_config()

    def on_config_changed(self):
        self.scraping_policies = ScrapingConfig.from_system_config()

    @staticmethod
    def _cleanup_temp_file(path: Optional[Path]):
        """
        清理临时刮削文件

        :param path: 临时文件路径
        """
        if not path or not path.exists():
            return
        try:
            path.unlink()
        except OSError as err:
            logger.warn(f"临时文件清理失败：{path} - {err}")

    @staticmethod
    def _should_scrape(
            scraping_option: ScrapingOption,
            file_exists: bool,
            global_overwrite: bool = False,
    ) -> bool:
        """
        判断是否应该执行刮削操作

        :param scraping_option: 刮削选项对象
        :param file_exists: 文件是否已存在
        :param global_overwrite: 全局覆盖标志
        :return bool: 是否应该刮削
        """
        if scraping_option.is_skip:
            logger.info(
                f"{scraping_option.type.value} {scraping_option.metadata.value} 刮削策略 {scraping_option.policy.value}"
            )
            return False

        if not file_exists:
            # 文件不存在
            return True

        # 文件存在的情况
        if scraping_option.is_overwrite or global_overwrite:
            logger.info(
                f"{scraping_option.type.value} {scraping_option.metadata.value} 文件存在，"
                f"{'配置为覆盖' if scraping_option.is_overwrite else '配置为全局覆盖'}"
            )
            return True
        else:
            logger.info(
                f"{scraping_option.type.value} {scraping_option.metadata.value} 文件已存在，跳过"
            )
            return False

    def _save_file(
            self, fileitem: _SchemaFileItem, path: Path, content: Union[bytes, str]
    ):
        """
        保存或上传文件

        :param fileitem: 关联的媒体文件项
        :param path: 元数据文件路径
        :param content: 文件内容
        """
        if not fileitem or not content or not path:
            return
        tmp_file_path = None
        try:
            # delete_on_close 是 Python 3.12 才支持的参数，使用 delete=False 后手动清理以兼容低版本。
            with NamedTemporaryFile(delete=False, suffix=path.suffix) as tmp_file:
                tmp_file_path = Path(tmp_file.name)
                # 写入内容
                if isinstance(content, bytes):
                    tmp_file.write(content)
                else:
                    tmp_file.write(content.encode("utf-8"))
                tmp_file.flush()

            # 刮削文件只需要读写权限
            tmp_file_path.chmod(0o666 & ~current_umask)

            # 上传文件
            item = self.storagechain.upload_file(
                fileitem=fileitem, path=tmp_file_path, new_name=path.name
            )
            if item:
                logger.info(f"已保存文件：{item.path}")
            else:
                logger.warn(f"文件保存失败：{path}")
        finally:
            self._cleanup_temp_file(tmp_file_path)

    def _download_and_save_image(
            self, fileitem: _SchemaFileItem, path: Path, url: str
    ):
        """
        流式下载图片并保存到文件

        :param fileitem: 关联的媒体文件项
        :param path: 图片文件路径
        :param url: 图片下载URL
        """
        if not fileitem or not url or not path:
            return
        try:
            logger.info(f"正在下载图片：{url} ...")
            http = _scraping_http_snapshot()
            response = http.stream(
                url,
                proxies=self.runtime_config.proxy,
                ua=self.runtime_config.normal_user_agent,
            )
            with response as r:
                if r and r.status_code == 200:
                    tmp_file_path = None
                    try:
                        # delete_on_close 是 Python 3.12 才支持的参数，使用 delete=False 后手动清理以兼容低版本。
                        with NamedTemporaryFile(delete=False, suffix=path.suffix) as tmp_file:
                            tmp_file_path = Path(tmp_file.name)
                            # 流式写入文件
                            for chunk in r.iter_content(chunk_size=8192):
                                if chunk:
                                    tmp_file.write(chunk)
                            tmp_file.flush()

                        # 刮削的图片只需要读写权限
                        tmp_file_path.chmod(0o666 & ~current_umask)

                        # 上传文件
                        item = self.storagechain.upload_file(
                            fileitem=fileitem, path=tmp_file_path, new_name=path.name
                        )
                        if item:
                            logger.info(f"已保存图片：{item.path}")
                        else:
                            logger.warn(f"图片保存失败：{path}")
                    finally:
                        self._cleanup_temp_file(tmp_file_path)
                else:
                    logger.info(f"{url} 图片下载失败")
        except Exception as err:
            logger.error(f"{url} 图片下载失败：{str(err)}！")

    def _get_target_fileitem_and_path(
            self,
            current_fileitem: _SchemaFileItem,
            item_type: ScrapingTarget,
            metadata_type: ScrapingMetadata,
            filename_hint: Optional[str] = None,
            parent_fileitem: Optional[_SchemaFileItem] = None,
    ) -> Tuple[_SchemaFileItem, Optional[Path]]:
        """
        根据当前上下文、刮削项类型和元数据类型生成目标 FileItem 和 Path
        处理 NFO 和图片文件的命名约定及存储位置
        """
        # 默认保存的目录是当前文件项的目录
        target_dir_item = current_fileitem
        target_dir_path = Path(current_fileitem.path)
        final_filename = filename_hint  # 如果提供了 filename_hint，优先使用

        # 针对 NFO 文件的特殊命名和存储逻辑
        if metadata_type == ScrapingMetadata.NFO:
            if item_type == ScrapingTarget.MOVIE:
                if current_fileitem.type == "file":
                    # 电影文件NFO: 放在电影文件同级目录，名称与电影文件主体一致，后缀.nfo
                    final_filename = f"{target_dir_path.stem}.nfo"
                    target_dir_item = (
                            parent_fileitem
                            or self.storagechain.get_parent_item(current_fileitem)
                    )
                    if not target_dir_item:
                        logger.error(
                            f"无法获取文件 {current_fileitem.path} 的父目录项。"
                        )
                        return (
                            current_fileitem,
                            None,
                        )  # 返回一个表示失败的FileItem和None
                    target_dir_path = Path(target_dir_item.path)
                else:  # current_fileitem.type == "dir"
                    # 电影目录NFO (例如蓝光原盘): 放在电影目录内，名称与目录名主体一致，后缀.nfo
                    final_filename = f"{target_dir_path.name}.nfo"
                    # target_dir_item 保持为 current_fileitem
                    # target_dir_path 保持为 Path(current_fileitem.path)
            elif item_type == ScrapingTarget.TV:
                # 电视剧根目录NFO: 放在剧集根目录内，命名为 tvshow.nfo
                final_filename = "tvshow.nfo"
            elif item_type == ScrapingTarget.SEASON:
                # 电视剧季目录NFO: 放在季目录内，命名为 season.nfo
                final_filename = "season.nfo"
            elif item_type == ScrapingTarget.EPISODE:
                # 电视剧集文件NFO: 放在集文件同级目录，名称与集文件主体一致，后缀.nfo
                final_filename = f"{target_dir_path.stem}.nfo"
                target_dir_item = parent_fileitem or self.storagechain.get_parent_item(
                    current_fileitem
                )
                if not target_dir_item:
                    logger.error(f"无法获取文件 {current_fileitem.path} 的父目录项。")
                    return current_fileitem, None  # 返回一个表示失败的FileItem和None
                target_dir_path = Path(target_dir_item.path)
        # 图片通常是放在当前目录 (current_fileitem) 下
        # Jellyfin/Kodi 等在季目录内使用通用图片名，而不是 season01-poster.jpg
        elif item_type == ScrapingTarget.SEASON:
            season_image_name_map = {
                ScrapingMetadata.POSTER: "poster",
                ScrapingMetadata.BANNER: "banner",
                ScrapingMetadata.THUMB: "thumb",
                ScrapingMetadata.BACKDROP: "backdrop",
                ScrapingMetadata.LANDSCAPE: "landscape",
            }
            if season_image_name := season_image_name_map.get(metadata_type):
                hint_ext = Path(filename_hint).suffix if filename_hint else ".jpg"
                final_filename = f"{season_image_name}{hint_ext}"
        elif item_type == ScrapingTarget.MOVIE and current_fileitem.type == "file":
            # 电影文件的图片应与视频文件同级保存，避免把图片路径拼到文件名下面。
            target_dir_item = parent_fileitem or self.storagechain.get_parent_item(
                current_fileitem
            )
            if not target_dir_item:
                logger.error(f"无法获取文件 {current_fileitem.path} 的父目录项。")
                return current_fileitem, None
            target_dir_path = Path(target_dir_item.path)
        # 如果是 EPISODE 类型的图片（如thumb），通常也是放在文件同级目录，文件名与视频文件一致
        elif (
                metadata_type in [ScrapingMetadata.THUMB]
                and item_type == ScrapingTarget.EPISODE
        ):
            hint_ext = Path(filename_hint).suffix if filename_hint else ".jpg"
            final_filename = f"{target_dir_path.stem}{hint_ext}"
            target_dir_item = parent_fileitem or self.storagechain.get_parent_item(
                current_fileitem
            )
            if not target_dir_item:
                logger.error(f"无法获取文件 {current_fileitem.path} 的父目录项。")
                return current_fileitem, None  # 返回一个表示失败的FileItem和None
            target_dir_path = Path(target_dir_item.path)
        # TODO: 考虑其他图片类型是否也需要保存到父目录

        # 确保最终有文件名
        if not final_filename:
            logger.error(
                f"无法为 {item_type.value} - {metadata_type.value} 确定文件名。filename_hint: {filename_hint}"
            )
            # 返回一个表示失败的FileItem和None
            return current_fileitem, None

        target_full_path = target_dir_path / final_filename
        return target_dir_item, target_full_path

    def _get_target_fileitems_and_paths(
            self,
            current_fileitem: _SchemaFileItem,
            item_type: ScrapingTarget,
            metadata_type: ScrapingMetadata,
            filename_hint: Optional[str] = None,
            parent_fileitem: Optional[_SchemaFileItem] = None,
    ) -> List[Tuple[_SchemaFileItem, Path]]:
        """
        根据刮削上下文生成一个或多个保存目标。
        季图片需要同时兼容根目录 seasonxx-poster 和季目录 poster 两种命名。
        """
        target_item, target_path = self._get_target_fileitem_and_path(
            current_fileitem=current_fileitem,
            item_type=item_type,
            metadata_type=metadata_type,
            filename_hint=filename_hint,
            parent_fileitem=parent_fileitem,
        )
        targets = [(target_item, target_path)] if target_path else []

        if (
                item_type != ScrapingTarget.SEASON
                or not filename_hint
                or not filename_hint.lower().startswith("season")
                or metadata_type not in {
                    ScrapingMetadata.POSTER,
                    ScrapingMetadata.BANNER,
                    ScrapingMetadata.THUMB,
                    ScrapingMetadata.BACKDROP,
                    ScrapingMetadata.LANDSCAPE,
                }
        ):
            return targets

        season_parent_item = parent_fileitem or self.storagechain.get_parent_item(
            current_fileitem
        )
        if not season_parent_item:
            logger.warn(f"无法获取季目录 {current_fileitem.path} 的父目录项，跳过根目录季图片")
            return targets

        season_root_path = Path(current_fileitem.path).with_name(filename_hint)
        root_target = (season_parent_item, season_root_path)
        if root_target not in targets:
            targets.insert(0, root_target)
        return targets

    def _expand_with_aliases(
            self,
            targets: List[Tuple[_SchemaFileItem, Path]],
            item_type: ScrapingTarget,
    ) -> List[Tuple[_SchemaFileItem, Path]]:
        """
        为兼容多媒体服务器，扩展图片保存目标列表，添加别名文件。
        例如 backdrop.jpg 同时保存为 fanart.jpg，thumb.jpg 同时保存为 landscape.jpg。
        """
        expanded = list(targets)
        for base_item, image_path in list(targets):
            if not image_path:
                continue
            stem = image_path.stem.lower()
            ext = image_path.suffix
            # 跳过 season 前缀文件（如 season01-poster.jpg）
            if stem.startswith("season"):
                continue
            aliases = self.IMAGE_ALIASES.get(stem)
            if not aliases:
                continue
            for alias in aliases:
                alias_meta_type = self.IMAGE_METADATA_MAP.get(alias)
                if alias_meta_type:
                    alias_option = self.scraping_policies.option(item_type, alias_meta_type)
                    if alias_option.is_skip:
                        continue
                alias_path = image_path.with_name(f"{alias}{ext}")
                alias_target = (base_item, alias_path)
                if alias_target not in expanded:
                    expanded.append(alias_target)
        return expanded

    def metadata_nfo(
            self,
            meta: MetaBase,
            mediainfo: MediaInfo,
            season: Optional[int] = None,
            episode: Optional[int] = None,
    ) -> Optional[str]:
        """
        获取NFO文件内容文本

        :param meta: 元数据
        :param mediainfo: 媒体信息
        :param season: 季号
        :param episode: 集号
        """
        return self.run_module(
            "metadata_nfo",
            meta=meta,
            mediainfo=mediainfo,
            season=season,
            episode=episode,
        )

    def metadata_img(
            self,
            mediainfo: MediaInfo,
            season: Optional[int] = None,
            episode: Optional[int] = None,
    ) -> Optional[dict]:
        """
        获取图片名称和url，合并所有模块的结果。
        优先使用高优先级模块的图片，低优先级模块补充缺失的图片类型。
        """
        # 插件扩展来源没有宿主内置刮削器，优先让插件按自身来源返回图片地址；
        # 内置来源仍保留原有多模块合并逻辑，避免改变既有图片补全顺序。
        if mediainfo and mediainfo.media_source not in tuple(MediaSource):
            plugin_images = self.run_module(
                "metadata_img",
                mediainfo=mediainfo,
                season=season,
                episode=episode,
            )
            if isinstance(plugin_images, dict):
                return plugin_images or None
        merged = {}
        for module in sorted(
            self.modulemanager.get_running_modules("metadata_img"),
            key=lambda x: x.get_priority(),
        ):
            try:
                result = module.metadata_img(
                    mediainfo=mediainfo, season=season, episode=episode
                )
                if result and isinstance(result, dict):
                    for name, url in result.items():
                        merged.setdefault(name, url)
            except Exception as err:
                logger.error(f"获取 {module.get_name()} 图片失败：{str(err)}")
        return merged or None

    @eventmanager.register(EventType.MetadataScrape)
    def scrape_metadata_event(self, event: Event):
        """监控手动刮削事件"""
        if not event:
            return
        event_data = event.event_data or {}
        # 读取事件载荷
        fileitem: FileItem = event_data.get("fileitem")
        file_list: List[str] = list(dict.fromkeys(event_data.get("file_list") or []))
        meta: MetaBase = event_data.get("meta")
        mediainfo: MediaInfo = event_data.get("mediainfo")
        overwrite = event_data.get("overwrite", False)
        if not fileitem:
            return

        # 刮削锁
        with scraping_lock:
            # 音乐刮削与影视共用 MediaChain 入口，按 ScrapingConfig 的音乐项写入标签与封面
            if getattr(mediainfo, "type", None) == MediaType.MUSIC:
                scrape_kwargs: dict[str, Any] = {}
                if file_list:
                    scrape_kwargs["audio_files"] = self._music_event_audio_fileitems(
                        root=fileitem,
                        file_list=file_list,
                    )
                    scrape_kwargs["media_by_path"] = {
                        Path(context.get("path")).as_posix(): context.get("mediainfo")
                        for context in event_data.get("file_contexts") or []
                        if (
                                isinstance(context, dict)
                                and context.get("path")
                                and isinstance(context.get("mediainfo"), MusicInfo)
                        )
                    }
                _, message = self.scrape_metadata(
                    fileitem=fileitem,
                    mediainfo=mediainfo,
                    overwrite=overwrite,
                    **scrape_kwargs,
                )
                if message:
                    logger.info(f"音乐刮削：{message}")
                return
            # 检查文件项是否存在
            if not self.storagechain.get_item(fileitem):
                logger.warn(f"文件项不存在：{fileitem.path}")
                return
            # 检查是否为目录
            if fileitem.type == "file":
                # 单个文件刮削
                self.scrape_metadata(
                    fileitem=fileitem,
                    mediainfo=mediainfo,
                    init_folder=True,
                    parent=self.storagechain.get_parent_item(fileitem),
                    overwrite=overwrite,
                )
            else:
                if file_list:
                    # 如果是BDMV原盘目录，只对根目录进行刮削，不处理子目录
                    if self.storagechain.is_bluray_folder(fileitem):
                        logger.info(
                            f"检测到BDMV原盘目录，只对根目录进行刮削：{fileitem.path}"
                        )
                        self.scrape_metadata(
                            fileitem=fileitem,
                            mediainfo=mediainfo,
                            init_folder=True,
                            recursive=False,
                            overwrite=overwrite,
                        )
                    else:
                        all_dirs: set[Path] = set()
                        root_path = Path(fileitem.path)

                        logger.debug(f"开始收集目录，根目录：{root_path}")
                        all_dirs.add(root_path)

                        for sub_file in file_list:
                            sub_path = Path(sub_file)
                            current_path = sub_path.parent
                            while (
                                    current_path != root_path
                                    and current_path.is_relative_to(root_path)
                            ):
                                all_dirs.add(current_path)
                                current_path = current_path.parent

                        if (
                                getattr(mediainfo, "type", None) == MediaType.TV
                                and root_path.parent != root_path
                                and (
                                        root_path.name in self.runtime_config.season_zero_names
                                        or MetaInfo(root_path.name).begin_season is not None
                                )
                        ):
                            # 整理事件可能以季目录为根，补回剧集根目录才能触发电视剧分支。
                            all_dirs.add(root_path.parent)

                        logger.debug(f"共收集到 {len(all_dirs)} 个目录")

                        # 2. 初始化一遍子目录，但不处理文件
                        for sub_dir in sorted(
                                all_dirs,
                                key=lambda item: (len(item.parts), item.as_posix()),
                        ):
                            sub_dir_item = self.storagechain.get_file_item(
                                storage=fileitem.storage, path=sub_dir
                            )
                            if sub_dir_item:
                                logger.info(f"为目录生成海报和nfo：{sub_dir}")
                                # 初始化目录元数据，但不处理文件
                                self.scrape_metadata(
                                    fileitem=sub_dir_item,
                                    mediainfo=mediainfo,
                                    init_folder=True,
                                    recursive=False,
                                    overwrite=overwrite,
                                )
                            else:
                                logger.warn(f"无法获取目录项：{sub_dir}")

                        # 3. 刮削每个文件
                        logger.info(f"开始刮削 {len(file_list)} 个文件")
                        for sub_file_path in sorted(file_list):
                            sub_file_item = self.storagechain.get_file_item(
                                storage=fileitem.storage, path=Path(sub_file_path)
                            )
                            if sub_file_item:
                                self.scrape_metadata(
                                    fileitem=sub_file_item,
                                    mediainfo=mediainfo,
                                    init_folder=False,
                                    overwrite=overwrite,
                                )
                            else:
                                logger.warn(f"无法获取文件项：{sub_file_path}")
                else:
                    # 执行全量刮削
                    logger.info(f"开始刮削目录 {fileitem.path} ...")
                    self.scrape_metadata(
                        fileitem=fileitem,
                        meta=meta,
                        init_folder=True,
                        mediainfo=mediainfo,
                        overwrite=overwrite,
                    )

    def _scrape_nfo_generic(
            self,
            current_fileitem: _SchemaFileItem,
            meta: MetaBase,
            mediainfo: MediaInfo,
            item_type: ScrapingTarget,
            parent_fileitem: Optional[_SchemaFileItem] = None,
            overwrite: bool = False,
            season_number: Optional[int] = None,
            episode_number: Optional[int] = None,
    ):
        """
        NFO 刮削
        """
        # 获取刮削选项
        nfo_option = self.scraping_policies.option(item_type, ScrapingMetadata.NFO)

        # 检查刮削开关
        if nfo_option.is_skip:
            logger.info(
                f"{item_type.value} {ScrapingMetadata.NFO.value} 刮削策略 {nfo_option.policy.value}"
            )
            return

        # 获取目标 FileItem (`base_item`) 和 Path (`nfo_path`)
        base_item, nfo_path = self._get_target_fileitem_and_path(
            current_fileitem=current_fileitem,
            item_type=item_type,
            metadata_type=ScrapingMetadata.NFO,
            parent_fileitem=parent_fileitem,
        )

        if not nfo_path:  # _get_target_fileitem_and_path 内部错误处理返回None
            return

        # 文件存在检查
        file_exists = self.storagechain.get_file_item(
            storage=base_item.storage, path=nfo_path
        )

        # 刮削决策
        if self._should_scrape(nfo_option, bool(file_exists), overwrite):
            # 生成 NFO 内容
            nfo_content = self.metadata_nfo(
                meta=meta,
                mediainfo=mediainfo,
                season=season_number,
                episode=episode_number,
            )
            if nfo_content:
                self._save_file(fileitem=base_item, path=nfo_path, content=nfo_content)
            else:
                logger.warn(f"{nfo_path.name} NFO 文件生成失败！")

    def _scrape_images_generic(
            self,
            current_fileitem: _SchemaFileItem,
            mediainfo: MediaInfo,
            item_type: ScrapingTarget,
            parent_fileitem: Optional[_SchemaFileItem] = None,
            overwrite: bool = False,
            season_number: Optional[int] = None,
            episode_number: Optional[int] = None,
    ):
        """
        图片刮削
        """
        # 获取图片 URL
        if item_type == ScrapingTarget.EPISODE:
            image_dict = self.metadata_img(
                mediainfo=mediainfo, season=season_number, episode=episode_number
            )
        elif item_type == ScrapingTarget.SEASON:
            image_dict = self.metadata_img(mediainfo=mediainfo, season=season_number)
        else:
            image_dict = self.metadata_img(mediainfo=mediainfo)

        if not image_dict:
            logger.info(f"未获取到 {item_type.value} 的图片信息，跳过图片刮削。")
            return

        # 遍历图片 image_name 和 image_url
        for image_name, image_url in image_dict.items():
            metadata_type = None
            # 对每个 image_name 查找匹配的 ScrapingMetadata
            for keyword, meta_type in self.IMAGE_METADATA_MAP.items():
                if keyword in image_name.lower():
                    metadata_type = meta_type
                    break

            if metadata_type:
                # 获取对应的 ScrapingOption
                option = self.scraping_policies.option(item_type, metadata_type)

                if option.is_skip:
                    logger.info(
                        f"{item_type.value} {option.metadata.value} 刮削策略 {option.policy.value}"
                    )
                    continue

                # 判断是否匹配当前刮削的季号
                if item_type == ScrapingTarget.TV and image_name.lower().startswith(
                        "season"
                ):
                    logger.info(f"当前为电视剧根目录刮削，跳过季图片：{image_name}")
                    continue
                if (
                        item_type == ScrapingTarget.SEASON
                        and season_number is not None
                        and image_name.lower().startswith("season")
                ):
                    # 检查是否只下载当前刮削季的图片
                    image_season_str = (
                        "00" if "specials" in image_name.lower() else image_name[6:8]
                    )

                    if image_season_str is not None and image_season_str != str(
                            season_number
                    ).rjust(2, "0"):
                        logger.info(
                            f"当前刮削季为：{season_number}，跳过非本季图片：{image_name}"
                        )
                        continue

                # 获取目标 FileItem 和 Path，季图片会同时写根目录和季目录。
                image_targets = self._get_target_fileitems_and_paths(
                    current_fileitem=current_fileitem,
                    item_type=item_type,
                    metadata_type=metadata_type,
                    filename_hint=image_name,
                    parent_fileitem=parent_fileitem,
                )

                # 扩展别名目标（如 backdrop→fanart, thumb→landscape）
                image_targets = self._expand_with_aliases(image_targets, item_type)

                for base_item, image_path in image_targets:
                    if not image_path:
                        continue

                    # 文件存在检查
                    file_exists = self.storagechain.get_file_item(
                        storage=base_item.storage, path=image_path
                    )

                    # 刮削决策
                    if self._should_scrape(option, bool(file_exists), overwrite):
                        self._download_and_save_image(
                            fileitem=base_item, path=image_path, url=image_url
                        )
            else:
                logger.debug(
                    f"未找到图片类型 {image_name} 对应的 ScrapingMetadata，跳过。"
                )

    def scrape_metadata(
            self,
            fileitem: _SchemaFileItem,
            meta: MetaBase = None,
            mediainfo: Union[MediaInfo, MusicInfo] = None,
            init_folder: bool = True,
            parent: _SchemaFileItem = None,
            overwrite: bool = False,
            recursive: bool = True,
            audio_files: Optional[list[_SchemaFileItem]] = None,
            media_by_path: Optional[dict[str, MusicInfo]] = None,
    ) -> tuple[bool, str]:
        """
        手动刮削媒体信息

        :param fileitem: 刮削目录或文件
        :param meta: 元数据
        :param mediainfo: 媒体信息
        :param init_folder: 是否刮削根目录
        :param parent: 上级目录
        :param overwrite: 是否覆盖已有文件
        :param recursive: 是否递归处理目录内文件
        :param audio_files: 音乐批次已确认成功的音频文件清单
        :param media_by_path: 音乐批次每个音频文件对应的标准身份
        """
        if not fileitem:
            return False, "未提供刮削文件"

        # 当前文件路径
        filepath = Path(fileitem.path)
        is_music = (
            getattr(mediainfo, "type", None) == MediaType.MUSIC
            or isinstance(meta, MetaMusic)
            or (
                fileitem.type == "file"
                and filepath.suffix.lower() in self.runtime_config.audio_extensions
            )
        )
        if is_music:
            music_info = (
                mediainfo
                if getattr(mediainfo, "type", None) == MediaType.MUSIC
                else None
            )
            music_kwargs: dict[str, Any] = {}
            if audio_files is not None:
                music_kwargs["audio_files"] = audio_files
            if media_by_path is not None:
                music_kwargs["media_by_path"] = media_by_path
            return self.scrape_music_metadata(
                fileitem=fileitem,
                mediainfo=music_info,
                overwrite=overwrite,
                **music_kwargs,
            )
        if fileitem.type == "file" and (
                not filepath.suffix
                or filepath.suffix.lower() not in self.runtime_config.video_extensions
        ):
            return False, "刮削路径不是支持的媒体文件"

        # 准备元数据和媒体信息
        if not meta:
            meta = MetaInfoPath(filepath)
        if not mediainfo:
            mediainfo = MediaChain().recognize_by_meta(meta)
        if not mediainfo:
            logger.warn(f"{filepath} 无法识别文件媒体信息！")
            return False, "未识别到媒体信息"

        logger.info(f"开始刮削：{filepath} ...")

        # 根据媒体类型分发处理逻辑
        if mediainfo.type == MediaType.MOVIE:
            self._handle_movie_scraping(
                fileitem=fileitem,
                meta=meta,
                mediainfo=mediainfo,
                init_folder=init_folder,
                parent=parent,
                overwrite=overwrite,
                recursive=recursive,
            )
        elif mediainfo.type == MediaType.TV:
            self._handle_tv_scraping(
                fileitem=fileitem,
                meta=meta,
                mediainfo=mediainfo,
                init_folder=init_folder,
                parent=parent,
                overwrite=overwrite,
                recursive=recursive,
            )
        else:
            logger.warn(f"{filepath} 媒体类型不支持刮削：{mediainfo.type}")
            return False, "媒体类型不支持刮削"

        logger.info(f"{filepath.name} 刮削完成")
        return True, f"{filepath.name} 刮削完成"

    def scrape_music_metadata(
            self,
            fileitem: _SchemaFileItem,
            mediainfo: Optional[MusicInfo] = None,
            overwrite: bool = True,
            media_source: Optional[MediaSource] = None,
            audio_files: Optional[list[_SchemaFileItem]] = None,
            media_by_path: Optional[dict[str, MusicInfo]] = None,
    ) -> tuple[bool, str]:
        """为音频文件或目录写入音乐标签和封面，应用系统刮削策略，复用现有存储下载上传能力。

        音乐识别由 MediaChain 提供，标签、封面和歌词写入仅由当前刮削链编排。

        :param audio_files: 已确认属于本批次的音频文件；为空时按 fileitem 展开
        :param media_by_path: 每个音频文件对应的音乐身份，避免批次内不同单曲互相覆盖
        """
        files = self._normalize_music_audio_fileitems(
            audio_files if audio_files is not None else self._music_audio_fileitems(fileitem)
        )
        if not files:
            return False, "刮削路径中没有支持的音频文件"
        normalized_media_by_path = {
            Path(path).as_posix(): info
            for path, info in (media_by_path or {}).items()
            if isinstance(info, MusicInfo)
        }
        file_media = [
            normalized_media_by_path.get(Path(item.path).as_posix(), mediainfo)
            for item in files
        ]
        distinct_recordings = {
            self._music_scrape_identity(info)
            for info in file_media
            if info and info.music_type != MUSIC_ENTITY_ALBUM
        }
        if len(files) > 1 and len(distinct_recordings) == 1 and all(file_media):
            return False, "单曲音乐 ID 仅支持刮削单个音频文件，整目录请选择专辑"

        # 三类音乐产物使用独立策略，允许只下载歌词而不改写音频标签。
        nfo_option = self.scraping_policies.option("music", "nfo")
        poster_option = self.scraping_policies.option("music", "poster")
        lyrics_option = self.scraping_policies.option("music", "lyrics")
        if nfo_option.is_skip and poster_option.is_skip and lyrics_option.is_skip:
            return False, "音乐标签、封面和歌词刮削策略均为跳过"

        with_cover = not poster_option.is_skip
        lyrics_chain = None
        if not lyrics_option.is_skip:
            lyrics_chain = LyricsChain(
                deadline=time.monotonic() + max(self.runtime_config.lyrics_batch_timeout, 0)
            )
        cover_cache: dict[str, tuple[Optional[bytes], str]] = {}
        album_cache: dict[tuple[str, str], Optional[MusicAlbumInfo]] = {}

        failures: list[str] = []
        lyrics_counts = {
            "saved": 0,
            "existing": 0,
            "missing": 0,
            "upgraded": 0,
            "protected": 0,
            "budget_exceeded": 0,
            "failed": 0,
        }
        metadata_failure_label = (
            "音乐标签和封面"
            if not nfo_option.is_skip and with_cover
            else "音乐标签" if not nfo_option.is_skip else "封面"
        )
        for audio_item, item_media in zip(files, file_media):
            item_cover = None
            cover_url = item_media.cover_url if item_media else None
            if with_cover and cover_url:
                if cover_url not in cover_cache:
                    cover_cache[cover_url] = self._download_music_cover(cover_url)
                item_cover = cover_cache[cover_url]
            album_info = None
            if (
                    item_media
                    and item_media.music_type == MUSIC_ENTITY_ALBUM
                    and item_media.media_source
                    and item_media.media_id
            ):
                album_key = (item_media.media_source, item_media.media_id)
                if album_key not in album_cache:
                    album_cache[album_key] = MediaChain().get_music_album(
                        media_source=item_media.media_source,
                        media_id=item_media.media_id,
                    )
                album_info = album_cache[album_key]
            result = self._scrape_music_file(
                audio_item,
                item_media,
                write_tags=not nfo_option.is_skip,
                tag_overwrite=overwrite or nfo_option.is_overwrite,
                with_cover=with_cover,
                cover_overwrite=overwrite or poster_option.is_overwrite,
                cover=item_cover,
                lyrics_option=lyrics_option,
                lyrics_overwrite=overwrite or lyrics_option.is_overwrite,
                lyrics_chain=lyrics_chain,
                album_info=album_info,
                media_source=media_source,
            )
            if not result.metadata_success:
                failures.append(
                    f"{audio_item.name or audio_item.path} {metadata_failure_label}写入失败"
                )
            if result.lyrics_status in lyrics_counts:
                lyrics_counts[result.lyrics_status] += 1
            if result.lyrics_status == "failed":
                failures.append(f"{audio_item.name or audio_item.path} 歌词保存失败")

        message = f"已刮削 {len(files)} 个音频文件"
        if not lyrics_option.is_skip:
            message += (
                f"，歌词新增 {lyrics_counts['saved']} 首"
                f"、升级 {lyrics_counts['upgraded']} 首"
                f"、已存在 {lyrics_counts['existing']} 首"
                f"、防降级保护 {lyrics_counts['protected']} 首"
                f"、未匹配 {lyrics_counts['missing']} 首"
            )
            if lyrics_counts["failed"]:
                message += f"、失败 {lyrics_counts['failed']} 首"
            if lyrics_counts["budget_exceeded"]:
                message += f"、预算耗尽 {lyrics_counts['budget_exceeded']} 首"
        if failures:
            return False, f"{message}；{'；'.join(failures[:3])}"
        return True, message

    @staticmethod
    def _music_scrape_identity(info: MusicInfo) -> tuple:
        """构造音乐刮削身份键，用于识别同一单曲被错误套用到多个文件。"""
        return (
            info.media_source,
            info.media_id,
            info.music_type,
            info.title,
            info.disc_number,
            info.track_number,
        )

    @staticmethod
    @cached(
        maxsize=64,
        ttl_provider=lambda: get_chain_runtime_config_snapshot().metadata_cache_ttl,
        skip_none=True,
    )
    def _request_music_cover(url: str) -> Optional[tuple[Optional[bytes], str]]:
        """下载并缓存音乐封面；仅稳定 404 与成功响应进入有界缓存。"""
        response = _scraping_http_snapshot().get(
            url,
            proxies=get_chain_runtime_config_snapshot().proxy,
            ua=get_chain_runtime_config_snapshot().normal_user_agent,
            timeout=20,
        )
        if response is None:
            return None
        try:
            if response.status_code == 404:
                return None, "image/jpeg"
            if response.status_code != 200:
                logger.warning(f"音乐封面下载失败：{response.status_code} {url}")
                return None
            mime = (response.headers.get("Content-Type") or "image/jpeg").split(";", 1)[0]
            return response.content, mime
        finally:
            response.close()

    @staticmethod
    def _download_music_cover(url: Optional[str]) -> tuple[Optional[bytes], str]:
        """通过有界缓存下载音乐封面，并统一返回图片内容与 MIME 类型。"""
        if not url:
            return None, "image/jpeg"
        return ScrapingChain._request_music_cover(url) or (None, "image/jpeg")

    @staticmethod
    def _is_music_audio_file(path: str) -> bool:
        """判断路径是否指向系统支持的音频文件。"""
        return Path(path).suffix.lower() in get_chain_runtime_config_snapshot().audio_extensions

    def _music_audio_fileitems(self, fileitem: _SchemaFileItem) -> list[_SchemaFileItem]:
        """展开待刮削目录并过滤系统支持的音频文件。"""
        if fileitem.type != "dir":
            return [fileitem] if self._is_music_audio_file(fileitem.path or "") else []
        return [
            item
            for item in self.storagechain.list_files(fileitem, recursion=True) or []
            if item.type == "file" and self._is_music_audio_file(item.path or "")
        ]

    @classmethod
    def _normalize_music_audio_fileitems(
            cls,
            fileitems: Iterable[_SchemaFileItem],
    ) -> list[_SchemaFileItem]:
        """过滤并按存储路径去重已选音频文件，保持调用方给出的顺序。"""
        normalized: list[_SchemaFileItem] = []
        seen: set[tuple[str, str]] = set()
        for item in fileitems or []:
            if (
                    not item
                    or item.type != "file"
                    or not cls._is_music_audio_file(item.path or "")
            ):
                continue
            key = (item.storage or "local", Path(item.path).as_posix())
            if key in seen:
                continue
            seen.add(key)
            normalized.append(item)
        return normalized

    def _music_event_audio_fileitems(
            self,
            root: _SchemaFileItem,
            file_list: Iterable[str],
    ) -> list[_SchemaFileItem]:
        """把刮削事件中的成功路径恢复为文件项，并限制在事件媒体根目录内。"""
        root_path = Path(root.path)
        selected: list[_SchemaFileItem] = []
        for raw_path in file_list or []:
            audio_path = Path(raw_path)
            if not self._is_music_audio_file(audio_path.as_posix()):
                continue
            if root.type == "dir" and not audio_path.is_relative_to(root_path):
                logger.warning(f"忽略媒体根目录外的音乐刮削路径：{audio_path}")
                continue
            item = self.storagechain.get_file_item(
                storage=root.storage,
                path=audio_path,
            )
            selected.append(item or _SchemaFileItem(
                storage=root.storage,
                path=audio_path.as_posix(),
                type="file",
                name=audio_path.name,
                basename=audio_path.stem,
                extension=audio_path.suffix.lstrip("."),
            ))
        return self._normalize_music_audio_fileitems(selected)

    def _scrape_music_file(
            self,
            fileitem: _SchemaFileItem,
            mediainfo: Optional[MusicInfo],
            write_tags: bool,
            tag_overwrite: bool,
            with_cover: bool = True,
            cover_overwrite: bool = True,
            cover: Optional[tuple[Optional[bytes], str]] = None,
            lyrics_option: Optional[ScrapingOption] = None,
            lyrics_overwrite: bool = False,
            lyrics_chain: Optional[LyricsChain] = None,
            album_info: Optional[MusicAlbumInfo] = None,
            media_source: Optional[MediaSource] = None,
    ) -> _MusicScrapeFileResult:
        """下载单个音轨并执行标签、封面和歌词刮削，远端产物写回原目录。"""
        storage = self.storagechain
        download_failure = _MusicScrapeFileResult(
            metadata_success=not (write_tags or with_cover),
            lyrics_status=(
                "failed"
                if lyrics_option and not lyrics_option.is_skip and lyrics_chain
                else "disabled"
            ),
        )
        if fileitem.storage == "local":
            local_path = storage.download_file(fileitem)
            if not local_path:
                return download_failure
            return self._apply_music_file_scrape(
                fileitem=fileitem,
                local_path=local_path,
                mediainfo=mediainfo,
                write_tags=write_tags,
                tag_overwrite=tag_overwrite,
                with_cover=with_cover,
                cover_overwrite=cover_overwrite,
                cover=cover,
                lyrics_option=lyrics_option,
                lyrics_overwrite=lyrics_overwrite,
                lyrics_chain=lyrics_chain,
                album_info=album_info,
                media_source=media_source,
            )

        with TemporaryDirectory(prefix="moviepilot-music-scrape-") as temp_dir:
            local_path = storage.download_file(fileitem, path=Path(temp_dir))
            if not local_path:
                return download_failure
            return self._apply_music_file_scrape(
                fileitem=fileitem,
                local_path=local_path,
                mediainfo=mediainfo,
                write_tags=write_tags,
                tag_overwrite=tag_overwrite,
                with_cover=with_cover,
                cover_overwrite=cover_overwrite,
                cover=cover,
                lyrics_option=lyrics_option,
                lyrics_overwrite=lyrics_overwrite,
                lyrics_chain=lyrics_chain,
                album_info=album_info,
                media_source=media_source,
            )

    def _apply_music_file_scrape(
            self,
            fileitem: _SchemaFileItem,
            local_path: Path,
            mediainfo: Optional[MusicInfo],
            write_tags: bool,
            tag_overwrite: bool,
            with_cover: bool,
            cover_overwrite: bool,
            cover: Optional[tuple[Optional[bytes], str]],
            lyrics_option: Optional[ScrapingOption],
            lyrics_overwrite: bool,
            lyrics_chain: Optional[LyricsChain],
            album_info: Optional[MusicAlbumInfo],
            media_source: Optional[MediaSource],
    ) -> _MusicScrapeFileResult:
        """在本地音轨副本上执行刮削，并将变更后的音频和歌词写回目标存储。"""
        scrape_info = self._resolve_music_scrape_info(local_path, mediainfo, media_source=media_source)
        metadata_requested = write_tags or with_cover
        metadata_success = True
        if metadata_requested:
            metadata_success = self._write_music_metadata(
                local_path=local_path,
                mediainfo=mediainfo,
                tag_overwrite=tag_overwrite,
                write_tags=write_tags,
                with_cover=with_cover,
                cover_overwrite=cover_overwrite,
                cover=cover,
                scrape_info=scrape_info,
                media_source=media_source,
            )

        lyrics_status = self._scrape_music_lyrics(
            fileitem=fileitem,
            local_path=local_path,
            scrape_info=scrape_info,
            lyrics_option=lyrics_option,
            overwrite=lyrics_overwrite,
            lyrics_chain=lyrics_chain,
            album_info=album_info,
        )

        if fileitem.storage != "local" and metadata_requested and metadata_success:
            parent = self.storagechain.get_parent_item(fileitem)
            if not parent:
                logger.warning(f"无法获取远端音频父目录：{fileitem.path}")
                metadata_success = False
            elif not self.storagechain.upload_file(
                    parent,
                    local_path,
                    new_name=fileitem.name or local_path.name,
            ):
                metadata_success = False
        return _MusicScrapeFileResult(
            metadata_success=metadata_success,
            lyrics_status=lyrics_status,
        )

    @staticmethod
    def _merge_music_album_metadata(local_meta: MetaMusic, album: MusicInfo) -> MetaMusic:
        """把专辑级字段合并到单个音轨标签，同时保留该文件自己的标题、艺术家和曲序。"""
        merged = deepcopy(local_meta)
        merged.artists = list(local_meta.artists or album.artists)
        merged.album = album.album or album.title or local_meta.album
        merged.album_artist = album.album_artist or album.artist or local_meta.album_artist
        merged.year = album.year or local_meta.year
        merged.total_tracks = album.total_tracks or local_meta.total_tracks
        merged.media_source = album.media_source or local_meta.media_source
        merged.media_id = album.media_id or local_meta.media_id
        return merged

    @classmethod
    def _match_music_album_track(
            cls,
            local_meta: MetaMusic,
            album_info: Optional[MusicAlbumInfo],
    ) -> Optional[MusicInfo]:
        """按碟号、曲序、标题、艺术家和时长为本地文件匹配专辑中的单个音轨。"""
        if not album_info or not album_info.tracks:
            return None
        local_title = cls._normalize_music_track_title(local_meta.title)
        local_artists = {
            cls._normalize_music_track_title(artist)
            for artist in local_meta.artists
            if artist
        }
        ranked: list[tuple[int, MusicInfo]] = []
        for track in album_info.tracks:
            score = 0
            if local_meta.track_number and track.track_number:
                if local_meta.track_number == track.track_number:
                    score += 6
                else:
                    continue
            if local_meta.disc_number and track.disc_number:
                if local_meta.disc_number == track.disc_number:
                    score += 3
                else:
                    continue
            if local_title and local_title == cls._normalize_music_track_title(track.title):
                score += 8
            track_artists = {
                cls._normalize_music_track_title(artist)
                for artist in track.artists
                if artist
            }
            if local_artists and track_artists and local_artists.intersection(track_artists):
                score += 3
            if local_meta.duration and track.duration:
                duration_delta = abs(local_meta.duration - track.duration)
                if duration_delta <= 2:
                    score += 4
                elif duration_delta > 5:
                    score -= 3
            if score >= 8:
                ranked.append((score, track))
        if not ranked:
            return None
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
            return None
        matched = deepcopy(ranked[0][1])
        matched.duration = matched.duration or local_meta.duration
        return matched

    @classmethod
    def _normalize_music_track_title(cls, value: Optional[str]) -> str:
        """规范化音轨标题并移除常见文件名前置碟号和曲序。"""
        title = cls._music_track_prefix_pattern.sub("", str(value or "").strip())
        return re.sub(r"[^\w]+", "", title.casefold(), flags=re.UNICODE)

    @classmethod
    def _resolve_music_scrape_info(
            cls,
            local_path: Path,
            mediainfo: Optional[MusicInfo],
            media_source: Optional[MediaSource] = None,
    ) -> Optional[MetaMusic | MusicInfo]:
        """在文件已下载到本地后解析刮削信息，专辑场景只覆盖专辑级标签。"""
        if mediainfo and mediainfo.music_type == MUSIC_ENTITY_ALBUM:
            return cls._merge_music_album_metadata(
                AudioMetadataHelper.read(local_path),
                mediainfo,
            )
        if mediainfo and mediainfo.music_type in (MUSIC_ENTITY_RECORDING, None, ""):
            return mediainfo
        if mediainfo:
            return None

        _, recognized = MediaChain().recognize_music_by_path(
            local_path,
            media_source=media_source,
        )
        return recognized

    def _write_music_metadata(
            self,
            local_path: Path,
            mediainfo: Optional[MusicInfo],
            tag_overwrite: bool,
            write_tags: bool,
            with_cover: bool,
            cover_overwrite: bool,
            cover: Optional[tuple[Optional[bytes], str]] = None,
            scrape_info: Optional[MetaMusic | MusicInfo] = None,
            media_source: Optional[MediaSource] = None,
    ) -> bool:
        """解析单个本地音轨并按独立策略写入标签和封面。"""
        scrape_info = scrape_info or self._resolve_music_scrape_info(
            local_path,
            mediainfo,
            media_source=media_source,
        )
        if not scrape_info or not scrape_info.title:
            logger.warning(f"无法识别音乐信息：{local_path}")
            return False
        if not with_cover:
            cover_data, cover_mime = None, "image/jpeg"
        elif cover is not None:
            cover_data, cover_mime = cover
        else:
            cover_url = getattr(mediainfo, "cover_url", None) or getattr(scrape_info, "cover_url", None)
            cover_data, cover_mime = self._download_music_cover(cover_url)
        return AudioMetadataHelper.write(
            local_path,
            scrape_info,
            cover_data=cover_data,
            cover_mime=cover_mime,
            overwrite=tag_overwrite,
            write_tags=write_tags,
            cover_overwrite=cover_overwrite,
        )

    def _scrape_music_lyrics(
            self,
            fileitem: _SchemaFileItem,
            local_path: Path,
            scrape_info: Optional[MetaMusic | MusicInfo],
            lyrics_option: Optional[ScrapingOption],
            overwrite: bool,
            lyrics_chain: Optional[LyricsChain],
            album_info: Optional[MusicAlbumInfo],
    ) -> str:
        """按歌词策略查询单个音轨并保存同名旁挂歌词文件。"""
        if not lyrics_option or lyrics_option.is_skip or not lyrics_chain:
            return "disabled"
        existing = self._find_music_lyrics_sidecar(fileitem)
        if existing and not overwrite and not getattr(lyrics_option, "is_upgrade", False):
            return "existing"
        if not scrape_info:
            return "missing"

        lookup_info: MetaMusic | MusicInfo = scrape_info
        if album_info:
            local_meta = AudioMetadataHelper.read(local_path)
            lookup_info = self._match_music_album_track(local_meta, album_info) or scrape_info
        embedded = AudioMetadataHelper.read_lyrics(local_path)
        lyrics = lyrics_chain.get_music_lyrics(
            lookup_info,
            local_candidates=[embedded] if embedded else None,
        )
        if lyrics_chain.budget_exceeded and not lyrics:
            return "budget_exceeded"
        if not lyrics or lyrics.instrumental or not lyrics.content or not lyrics.extension:
            return "missing"
        existing_quality = self._music_lyrics_sidecar_quality(existing)
        if existing_quality > lyrics.quality_rank:
            return "protected"
        if existing and existing_quality == lyrics.quality_rank and not overwrite:
            return "existing"
        status = "upgraded" if existing and lyrics.quality_rank > existing_quality else "saved"
        return (
            status
            if self._write_music_lyrics_sidecar(
                fileitem=fileitem,
                local_path=local_path,
                lyrics=lyrics,
                overwrite=overwrite,
            )
            else "failed"
        )

    def _find_music_lyrics_sidecar(
            self,
            fileitem: _SchemaFileItem,
    ) -> Optional[_SchemaFileItem]:
        """查找音轨旁已存在的同步或纯文本歌词文件。"""
        audio_path = Path(fileitem.path)
        for extension in self.MUSIC_LYRICS_EXTENSIONS:
            target_path = self._music_lyrics_path(audio_path, extension)
            item = self.storagechain.get_file_item(
                storage=fileitem.storage,
                path=target_path,
            )
            if item:
                return item
        return None

    @classmethod
    def _music_lyrics_path(cls, audio_path: Path, extension: str) -> Path:
        """构造普通歌词和双扩展名 Lyricsfile 的同名旁挂路径。"""
        return audio_path.with_suffix(extension)

    @staticmethod
    def _music_lyrics_sidecar_quality(fileitem: Optional[_SchemaFileItem]) -> int:
        """按旁挂扩展名估算质量，用于写入前执行防降级保护。"""
        if not fileitem:
            return 0
        path = str(fileitem.path or "").casefold()
        if path.endswith(".lyricsfile.yaml"):
            return 4
        if path.endswith(".lrc"):
            return 3
        if path.endswith(".txt"):
            return 1
        return 0

    def _write_music_lyrics_sidecar(
            self,
            fileitem: _SchemaFileItem,
            local_path: Path,
            lyrics: MusicLyrics,
            overwrite: bool,
    ) -> bool:
        """原子写入本地歌词或上传远端歌词，并在覆盖时清理旧格式旁挂文件。"""
        extension = lyrics.extension
        content = lyrics.content
        if not extension or not content:
            return False
        target_path = Path(fileitem.path).with_suffix(extension)
        target_name = target_path.name
        temp_path: Optional[Path] = None
        try:
            if fileitem.storage == "local":
                with NamedTemporaryFile(
                        mode="w",
                        encoding="utf-8",
                        dir=target_path.parent,
                        prefix=f".{target_name}.",
                        delete=False,
                ) as temp_file:
                    temp_file.write(f"{content.rstrip()}\n")
                    temp_path = Path(temp_file.name)
                temp_path.replace(target_path)
            else:
                parent = self.storagechain.get_parent_item(fileitem)
                if not parent:
                    logger.warning(f"无法获取远端歌词父目录：{fileitem.path}")
                    return False
                temp_path = local_path.with_suffix(extension)
                temp_path.write_text(f"{content.rstrip()}\n", encoding="utf-8")
                if not self.storagechain.upload_file(
                        parent,
                        temp_path,
                        new_name=target_name,
                ):
                    return False

            if lyrics.lyricsfile and not self._write_music_lyricsfile_sidecar(
                    fileitem=fileitem,
                    local_path=local_path,
                    content=lyrics.lyricsfile,
            ):
                return False

            if overwrite:
                self._remove_alternate_music_lyrics(fileitem, keep_extension=extension)
            return True
        except OSError as err:
            logger.warning(f"保存音乐歌词失败：{target_path} - {err}")
            return False
        finally:
            if temp_path and temp_path.exists() and temp_path != target_path:
                self._cleanup_temp_file(temp_path)

    def _write_music_lyricsfile_sidecar(
            self,
            fileitem: _SchemaFileItem,
            local_path: Path,
            content: str,
    ) -> bool:
        """保留来源返回的标准 Lyricsfile，同时由主写入流程生成播放器兼容歌词。"""
        target_path = self._music_lyrics_path(Path(fileitem.path), ".lyricsfile.yaml")
        try:
            if fileitem.storage == "local":
                target_path.write_text(f"{content.rstrip()}\n", encoding="utf-8")
                return True
            parent = self.storagechain.get_parent_item(fileitem)
            if not parent:
                return False
            temp_path = local_path.with_suffix(".lyricsfile.yaml")
            temp_path.write_text(f"{content.rstrip()}\n", encoding="utf-8")
            try:
                return bool(self.storagechain.upload_file(parent, temp_path, new_name=target_path.name))
            finally:
                self._cleanup_temp_file(temp_path)
        except OSError as err:
            logger.warning(f"保存 Lyricsfile 失败：{target_path} - {err}")
            return False

    def _remove_alternate_music_lyrics(
            self,
            fileitem: _SchemaFileItem,
            keep_extension: str,
    ) -> None:
        """覆盖歌词格式后删除同音轨的旧扩展名文件，避免播放器优先读取过期内容。"""
        audio_path = Path(fileitem.path)
        for extension in self.MUSIC_LYRICS_EXTENSIONS:
            if extension in (keep_extension, ".lyricsfile.yaml"):
                continue
            target_path = self._music_lyrics_path(audio_path, extension)
            item = self.storagechain.get_file_item(
                storage=fileitem.storage,
                path=target_path,
            )
            if item and not self.storagechain.delete_file(item):
                logger.warning(f"删除旧歌词文件失败：{item.path}")

    def _handle_movie_scraping(
            self,
            fileitem: _SchemaFileItem,
            meta: MetaBase,
            mediainfo: MediaInfo,
            init_folder: bool,
            parent: _SchemaFileItem,
            overwrite: bool,
            recursive: bool,
    ):
        """
        处理电影刮削
        """
        if fileitem.type == "file":
            # 电影文件始终处理 NFO，直接初始化文件时再补同级目录图片。
            self._scrape_nfo_generic(
                current_fileitem=fileitem,
                meta=meta,
                mediainfo=mediainfo,
                item_type=ScrapingTarget.MOVIE,
                parent_fileitem=parent,
                overwrite=overwrite,
            )
            if init_folder:
                self._scrape_images_generic(
                    current_fileitem=fileitem,
                    mediainfo=mediainfo,
                    item_type=ScrapingTarget.MOVIE,
                    parent_fileitem=parent,
                    overwrite=overwrite,
                )
        else:
            # 电影目录：递归处理文件并初始化目录
            self._handle_movie_directory(
                fileitem=fileitem,
                meta=meta,
                mediainfo=mediainfo,
                init_folder=init_folder,
                overwrite=overwrite,
                recursive=recursive,
            )

    def _handle_movie_directory(
            self,
            fileitem: _SchemaFileItem,
            meta: MetaBase,
            mediainfo: MediaInfo,
            init_folder: bool,
            overwrite: bool,
            recursive: bool,
    ):
        """
        处理电影目录刮削
        """
        files = self.storagechain.list_files(fileitem=fileitem) or []
        is_bluray_folder = self.storagechain.contains_bluray_subdirectories(files)

        # 递归处理文件（非蓝光原盘）
        if recursive and not is_bluray_folder:
            for file in files:
                if file.type == "dir":
                    continue
                self.scrape_metadata(
                    fileitem=file,
                    mediainfo=mediainfo,
                    init_folder=False,
                    parent=fileitem,
                    overwrite=overwrite,
                )

        # 初始化目录元数据
        if init_folder:
            if is_bluray_folder:
                # 蓝光原盘目录：仅处理 NFO
                self._scrape_nfo_generic(
                    current_fileitem=fileitem,
                    meta=meta,
                    mediainfo=mediainfo,
                    item_type=ScrapingTarget.MOVIE,
                    overwrite=overwrite,
                )
            # 电影目录：处理图片
            self._scrape_images_generic(
                current_fileitem=fileitem,
                mediainfo=mediainfo,
                item_type=ScrapingTarget.MOVIE,
                overwrite=overwrite,
            )

    def _handle_tv_scraping(
            self,
            fileitem: _SchemaFileItem,
            meta: MetaBase,
            mediainfo: MediaInfo,
            init_folder: bool,
            parent: _SchemaFileItem,
            overwrite: bool,
            recursive: bool,
    ):
        """
        处理电视剧刮削
        """
        filepath = Path(fileitem.path)

        if fileitem.type == "file":
            # 电视剧集文件：重新识别季集信息并刮削
            self._handle_tv_episode_file(
                fileitem=fileitem,
                filepath=filepath,
                mediainfo=mediainfo,
                parent=parent,
                overwrite=overwrite,
            )
        else:
            # 电视剧目录：递归处理并初始化目录
            self._handle_tv_directory(
                fileitem=fileitem,
                filepath=filepath,
                meta=meta,
                mediainfo=mediainfo,
                init_folder=init_folder,
                parent=parent,
                overwrite=overwrite,
                recursive=recursive,
            )

    def _handle_tv_episode_file(
            self,
            fileitem: _SchemaFileItem,
            filepath: Path,
            mediainfo: MediaInfo,
            parent: _SchemaFileItem,
            overwrite: bool,
    ):
        """
        处理电视剧集文件刮削
        """
        # 重新识别季集信息
        file_meta = MetaInfoPath(filepath)
        if not file_meta.begin_episode:
            logger.warn(f"{filepath.name} 无法识别文件集数！")
            return

        media_source, media_id = resolve_media_identity(media=mediainfo)
        if not media_source or not media_id:
            logger.warn(f"{filepath.name} 缺少完整媒体身份，无法识别剧集信息！")
            return
        file_mediainfo = MediaChain().recognize_media(
            meta=file_meta,
            media_source=media_source,
            media_id=media_id,
            episode_group=mediainfo.episode_group,
        )
        if not file_mediainfo:
            logger.warn(f"{filepath.name} 无法识别文件媒体信息！")
            return

        # 处理 NFO
        self._scrape_nfo_generic(
            current_fileitem=fileitem,
            meta=file_meta,
            mediainfo=file_mediainfo,
            item_type=ScrapingTarget.EPISODE,
            parent_fileitem=parent,
            overwrite=overwrite,
            season_number=file_meta.begin_season,
            episode_number=file_meta.begin_episode,
        )

        # 处理图片
        self._scrape_images_generic(
            current_fileitem=fileitem,
            mediainfo=file_mediainfo,
            item_type=ScrapingTarget.EPISODE,
            parent_fileitem=parent,
            overwrite=overwrite,
            season_number=file_meta.begin_season,
            episode_number=file_meta.begin_episode,
        )

    def _handle_tv_directory(
            self,
            fileitem: _SchemaFileItem,
            filepath: Path,
            meta: MetaBase,
            mediainfo: MediaInfo,
            init_folder: bool,
            parent: _SchemaFileItem,
            overwrite: bool,
            recursive: bool,
    ):
        """
        处理电视剧目录刮削
        """
        # 递归处理子目录和文件
        if recursive:
            files = self.storagechain.list_files(fileitem=fileitem) or []
            for file in files:
                if (
                        file.type == "dir"
                        and file.name not in self.runtime_config.season_zero_names
                        and MetaInfo(file.name).begin_season is None
                ):
                    # 电视剧不处理非季子目录
                    continue
                self.scrape_metadata(
                    fileitem=file,
                    mediainfo=mediainfo,
                    parent=fileitem if file.type == "file" else None,
                    init_folder=True if file.type == "dir" else False,
                    overwrite=overwrite,
                )

        # 初始化目录元数据
        if init_folder:
            self._initialize_tv_directory_metadata(
                fileitem=fileitem,
                filepath=filepath,
                meta=meta,
                mediainfo=mediainfo,
                parent=parent,
                overwrite=overwrite,
            )

    def _initialize_tv_directory_metadata(
            self,
            fileitem: _SchemaFileItem,
            filepath: Path,
            meta: MetaBase,
            mediainfo: MediaInfo,
            parent: _SchemaFileItem,
            overwrite: bool,
    ):
        """
        初始化电视剧目录元数据（识别季号并刮削）
        """
        # 识别文件夹名称
        season_meta = MetaInfo(filepath.name)

        # 特殊季目录处理（Specials/SPs）
        if filepath.name in self.runtime_config.season_zero_names:
            season_meta.begin_season = 0
        elif season_meta.name and season_meta.begin_season is not None:
            # 排除辅助词重新识别，避免误判根目录 (issue https://github.com/jxxghp/MoviePilot/issues/5501)
            season_meta_no_custom = MetaInfo(filepath.name, custom_words=["#"])
            if season_meta_no_custom.begin_season is None:
                # 季号由辅助词指定，按剧集根目录处理 (issue https://github.com/jxxghp/MoviePilot/issues/5373)
                season_meta.begin_season = None

        # 根据季号判断目录类型并刮削
        if season_meta.begin_season is not None:
            # 季目录：处理季 NFO 和图片
            self._scrape_nfo_generic(
                current_fileitem=fileitem,
                meta=meta,
                mediainfo=mediainfo,
                item_type=ScrapingTarget.SEASON,
                overwrite=overwrite,
                season_number=season_meta.begin_season,
            )
            self._scrape_images_generic(
                current_fileitem=fileitem,
                mediainfo=mediainfo,
                item_type=ScrapingTarget.SEASON,
                parent_fileitem=parent,
                overwrite=overwrite,
                season_number=season_meta.begin_season,
            )
        elif season_meta.name:
            # 剧集根目录：处理电视剧 NFO 和图片
            self._scrape_nfo_generic(
                current_fileitem=fileitem,
                meta=meta,
                mediainfo=mediainfo,
                item_type=ScrapingTarget.TV,
                overwrite=overwrite,
            )
            self._scrape_images_generic(
                current_fileitem=fileitem,
                mediainfo=mediainfo,
                item_type=ScrapingTarget.TV,
                overwrite=overwrite,
            )
        else:
            logger.warn("无法识别元数据，跳过")
