import traceback
from abc import abstractmethod
from pathlib import Path
from typing import Optional, Any, Tuple, List, Set, Union, Dict

from ruamel.yaml import CommentedMap

from app.core.context import Context
from app.core.event import EventManager
from app.core.module import ModuleManager
from app.core.context import MediaInfo, TorrentInfo
from app.core.meta import MetaBase
from app.log import logger
from app.schemas.context import TransferInfo, TransferTorrent, ExistMediaInfo, DownloadingTorrent
from app.utils.singleton import AbstractSingleton, Singleton
from app.schemas.types import TorrentStatus, MediaType


class ChainBase(AbstractSingleton, metaclass=Singleton):
    """
    处理链基类
    """

    def __init__(self):
        """
        公共初始化
        """
        self.modulemanager = ModuleManager()
        self.eventmanager = EventManager()

    @abstractmethod
    def process(self, *args, **kwargs) -> Optional[Context]:
        """
        处理链，返回上下文
        """
        pass

    def run_module(self, method: str, *args, **kwargs) -> Any:
        """
        运行包含该方法的所有模块，然后返回结果
        """

        def is_result_empty(ret):
            """
            判断结果是否为空
            """
            if isinstance(ret, tuple):
                return all(value is None for value in ret)
            else:
                return result is None

        logger.debug(f"请求模块执行：{method} ...")
        result = None
        modules = self.modulemanager.get_modules(method)
        for module in modules:
            try:
                if is_result_empty(result):
                    result = getattr(module, method)(*args, **kwargs)
                else:
                    if isinstance(result, tuple):
                        temp = getattr(module, method)(*result)
                    else:
                        temp = getattr(module, method)(result)
                    if temp:
                        result = temp
            except Exception as err:
                logger.error(f"运行模块 {method} 出错：{module.__class__.__name__} - {err}\n{traceback.print_exc()}")
        return result

    def prepare_recognize(self, title: str,
                          subtitle: str = None) -> Tuple[str, str]:
        return self.run_module("prepare_recognize", title=title, subtitle=subtitle)

    def recognize_media(self, meta: MetaBase = None,
                        mtype: MediaType = None,
                        tmdbid: int = None) -> Optional[MediaInfo]:
        return self.run_module("recognize_media", meta=meta, mtype=mtype, tmdbid=tmdbid)

    def obtain_image(self, mediainfo: MediaInfo) -> Optional[MediaInfo]:
        return self.run_module("obtain_image", mediainfo=mediainfo)

    def douban_info(self, doubanid: str) -> Optional[dict]:
        return self.run_module("douban_info", doubanid=doubanid)

    def tvdb_info(self, tvdbid: int) -> Optional[dict]:
        return self.run_module("tvdb_info", tvdbid=tvdbid)

    def message_parser(self, body: Any, form: Any, args: Any) -> Optional[dict]:
        return self.run_module("message_parser", body=body, form=form, args=args)

    def webhook_parser(self, body: Any, form: Any, args: Any) -> Optional[dict]:
        return self.run_module("webhook_parser", body=body, form=form, args=args)

    def search_medias(self, meta: MetaBase) -> Optional[List[MediaInfo]]:
        return self.run_module("search_medias", meta=meta)

    def search_torrents(self, mediainfo: Optional[MediaInfo], sites: List[CommentedMap],
                        keyword: str = None) -> Optional[List[TorrentInfo]]:
        return self.run_module("search_torrents", mediainfo=mediainfo, sites=sites, keyword=keyword)

    def refresh_torrents(self, sites: List[CommentedMap]) -> Optional[List[TorrentInfo]]:
        return self.run_module("refresh_torrents", sites=sites)

    def filter_torrents(self, torrent_list: List[TorrentInfo],
                        season_episodes: Dict[int, list] = None) -> List[TorrentInfo]:
        return self.run_module("filter_torrents", torrent_list=torrent_list, season_episodes=season_episodes)

    def download(self, torrent_path: Path, cookie: str,
                 episodes: Set[int] = None) -> Optional[Tuple[Optional[str], str]]:
        return self.run_module("download", torrent_path=torrent_path, cookie=cookie, episodes=episodes)

    def download_added(self, context: Context, torrent_path: Path) -> None:
        return self.run_module("download_added", context=context, torrent_path=torrent_path)

    def list_torrents(self, status: TorrentStatus = None,
                      hashs: Union[list, str] = None) -> Optional[List[Union[TransferTorrent, DownloadingTorrent]]]:
        return self.run_module("list_torrents", status=status, hashs=hashs)

    def transfer(self, path: Path, mediainfo: MediaInfo) -> Optional[TransferInfo]:
        return self.run_module("transfer", path=path, mediainfo=mediainfo)

    def transfer_completed(self, hashs: Union[str, list], transinfo: TransferInfo) -> None:
        return self.run_module("transfer_completed", hashs=hashs, transinfo=transinfo)

    def remove_torrents(self, hashs: Union[str, list]) -> bool:
        return self.run_module("remove_torrents", hashs=hashs)

    def media_exists(self, mediainfo: MediaInfo) -> Optional[ExistMediaInfo]:
        return self.run_module("media_exists", mediainfo=mediainfo)

    def refresh_mediaserver(self, mediainfo: MediaInfo, file_path: Path) -> Optional[bool]:
        return self.run_module("refresh_mediaserver", mediainfo=mediainfo, file_path=file_path)

    def post_message(self, title: str, text: str = None,
                     image: str = None, userid: Union[str, int] = None) -> Optional[bool]:
        return self.run_module("post_message", title=title, text=text, image=image, userid=userid)

    def post_medias_message(self, title: str, items: List[MediaInfo],
                            userid: Union[str, int] = None) -> Optional[bool]:
        return self.run_module("post_medias_message", title=title, items=items, userid=userid)

    def post_torrents_message(self, title: str, items: List[Context],
                              mediainfo: MediaInfo,
                              userid: Union[str, int] = None) -> Optional[bool]:
        return self.run_module("post_torrents_message", title=title, mediainfo=mediainfo,
                               items=items, userid=userid)

    def scrape_metadata(self, path: Path, mediainfo: MediaInfo) -> None:
        return self.run_module("scrape_metadata", path=path, mediainfo=mediainfo)

    def register_commands(self, commands: dict) -> None:
        return self.run_module("register_commands", commands=commands)
