import re
from pathlib import Path
from typing import List, Optional, Tuple, Set

from app.chain import _ChainBase
from app.core import MediaInfo
from app.core import TorrentInfo, Context
from app.core.meta import MetaBase
from app.helper.torrent import TorrentHelper
from app.log import logger
from app.utils.string import StringUtils
from app.utils.types import MediaType


class CommonChain(_ChainBase):

    def __init__(self):
        super().__init__()
        self.torrent = TorrentHelper()

    def process(self, *args, **kwargs) -> Optional[Context]:
        pass

    def post_message(self, title: str, text: str = None, image: str = None, userid: str = None):
        """
        发送消息
        """
        self.run_module('post_message', title=title, text=text, image=image, userid=userid)

    def post_download_message(self, meta: MetaBase, mediainfo: MediaInfo, torrent: TorrentInfo, userid: str = None):
        """
        发送添加下载的消息
        """
        msg_text = ""
        if torrent.site_name:
            msg_text = f"站点：{torrent.site_name}"
        if meta.get_resource_type_string():
            msg_text = f"{msg_text}\n质量：{meta.get_resource_type_string()}"
        if torrent.size:
            if str(torrent.size).isdigit():
                size = StringUtils.str_filesize(torrent.size)
            else:
                size = torrent.size
            msg_text = f"{msg_text}\n大小：{size}"
        if torrent.title:
            msg_text = f"{msg_text}\n种子：{torrent.title}"
        if torrent.seeders:
            msg_text = f"{msg_text}\n做种数：{torrent.seeders}"
        msg_text = f"{msg_text}\n促销：{torrent.get_volume_factor_string()}"
        if torrent.hit_and_run:
            msg_text = f"{msg_text}\nHit&Run：是"
        if torrent.description:
            html_re = re.compile(r'<[^>]+>', re.S)
            description = html_re.sub('', torrent.description)
            torrent.description = re.sub(r'<[^>]+>', '', description)
            msg_text = f"{msg_text}\n描述：{torrent.description}"

        self.post_message(title=f"{mediainfo.get_title_string()}"
                                f"{meta.get_season_episode_string()} 开始下载",
                          text=msg_text,
                          image=mediainfo.get_message_image(),
                          userid=userid)

    def batch_download(self,
                       contexts: List[Context],
                       need_tvs: dict = None,
                       userid: str = None) -> Tuple[List[Context], dict]:
        """
        根据缺失数据，自动种子列表中组合择优下载
        :param contexts:  资源上下文列表
        :param need_tvs:  缺失的剧集信息
        :param userid:  用户ID
        :return: 已经下载的资源列表、剩余未下载到的剧集 no_exists[mediainfo.tmdb_id] = [
                    {
                        "season": season,
                        "episodes": episodes,
                        "total_episodes": len(episodes)
                    }
                ]
        """
        # 已下载的项目
        downloaded_list: list = []

        def __download_torrent(_torrent: TorrentInfo) -> Tuple[Optional[Path], list]:
            """
            下载种子文件
            :return: 种子路径，种子文件清单
            """
            torrent_file, _, _, files, error_msg = self.torrent.download_torrent(
                url=_torrent.enclosure,
                cookie=_torrent.site_cookie,
                ua=_torrent.site_ua,
                proxy=_torrent.site_proxy)
            if not torrent_file:
                logger.error(f"下载种子文件失败：{_torrent.title} - {_torrent.enclosure}")
                self.run_module('post_message',
                                title=f"{_torrent.title} 种子下载失败！",
                                text=f"错误信息：{error_msg}\n种子链接：{_torrent.enclosure}",
                                userid=userid)
                return None, []
            return torrent_file, files

        def __download(_context: Context, _torrent_file: Path = None, _episodes: Set[int] = None) -> Optional[str]:
            """
            下载及发送通知
            """
            _torrent = _context.torrent_info
            _media = _context.media_info
            _meta = _context.meta_info
            if not _torrent_file:
                # 下载种子文件
                _torrent_file, _ = __download_torrent(_torrent)
                if not _torrent_file:
                    return
            # 添加下载
            result: Optional[tuple] = self.run_module("download",
                                                      torrent_path=_torrent_file,
                                                      cookie=_torrent.site_cookie,
                                                      episodes=_episodes)
            if result:
                _hash, error_msg = result
            else:
                _hash, error_msg = None, "未知错误"

            if _hash:
                # 下载成功
                downloaded_list.append(_context)
                self.post_download_message(meta=_meta, mediainfo=_media, torrent=_torrent, userid=userid)
            else:
                # 下载失败
                logger.error(f"{_media.get_title_string()} 添加下载任务失败："
                             f"{_torrent.title} - {_torrent.enclosure}，{error_msg}")
                self.run_module('post_message',
                                title="添加下载任务失败：%s %s"
                                      % (_media.get_title_string(), _meta.get_season_episode_string()),
                                text=f"站点：{_torrent.site_name}\n"
                                     f"种子名称：{_meta.org_string}\n"
                                     f"种子链接：{_torrent.enclosure}\n"
                                     f"错误信息：{error_msg}",
                                image=_media.get_message_image(),
                                userid=userid)
            return _hash

        def __update_seasons(tmdbid, need, current):
            """
            更新need_tvs季数
            """
            need = list(set(need).difference(set(current)))
            for cur in current:
                for nt in need_tvs.get(tmdbid):
                    if cur == nt.get("season") or (cur == 1 and not nt.get("season")):
                        need_tvs[tmdbid].remove(nt)
            if not need_tvs.get(tmdbid):
                need_tvs.pop(tmdbid)
            return need

        def __update_episodes(tmdbid, seq, need, current):
            """
            更新need_tvs集数
            """
            need = list(set(need).difference(set(current)))
            if need:
                need_tvs[tmdbid][seq]["episodes"] = need
            else:
                need_tvs[tmdbid].pop(seq)
                if not need_tvs.get(tmdbid):
                    need_tvs.pop(tmdbid)
            return need

        def __get_season_episodes(tmdbid, season):
            """
            获取需要的季的集数
            """
            if not need_tvs.get(tmdbid):
                return 0
            for nt in need_tvs.get(tmdbid):
                if season == nt.get("season"):
                    return nt.get("total_episodes")
            return 0

        # 如果是电影，直接下载
        for context in contexts:
            if context.media_info.type == MediaType.MOVIE:
                __download(context)

        # 电视剧整季匹配
        if need_tvs:
            # 先把整季缺失的拿出来，看是否刚好有所有季都满足的种子
            need_seasons = {}
            for need_tmdbid, need_tv in need_tvs.items():
                for tv in need_tv:
                    if not tv:
                        continue
                    if not tv.get("episodes"):
                        if not need_seasons.get(need_tmdbid):
                            need_seasons[need_tmdbid] = []
                        need_seasons[need_tmdbid].append(tv.get("season") or 1)
            # 查找整季包含的种子，只处理整季没集的种子或者是集数超过季的种子
            for need_tmdbid, need_season in need_seasons.items():
                for context in contexts:
                    media = context.media_info
                    meta = context.meta_info
                    torrent = context.torrent_info
                    if media.type == MediaType.MOVIE:
                        continue
                    item_season = meta.get_season_list()
                    if meta.get_episode_list():
                        continue
                    if need_tmdbid == media.tmdb_id:
                        if set(item_season).issubset(set(need_season)):
                            if len(item_season) == 1:
                                # 只有一季的可能是命名错误，需要打开种子鉴别，只有实际集数大于等于总集数才下载
                                torrent_path, torrent_files = __download_torrent(torrent)
                                if not torrent_path:
                                    continue
                                torrent_episodes = self.torrent.get_torrent_episodes(torrent_files)
                                if not torrent_episodes \
                                        or len(torrent_episodes) >= __get_season_episodes(need_tmdbid, item_season[0]):
                                    _, download_id = __download(_context=context, _torrent_file=torrent_path)
                                else:
                                    logger.info(
                                        f"【Downloader】种子 {meta.org_string} 未含集数信息，解析文件数为 {len(torrent_episodes)}")
                                    continue
                            else:
                                download_id = __download(context)
                            if download_id:
                                # 更新仍需季集
                                need_season = __update_seasons(tmdbid=need_tmdbid,
                                                               need=need_season,
                                                               current=item_season)
        # 电视剧季内的集匹配
        if need_tvs:
            need_tv_list = list(need_tvs)
            for need_tmdbid in need_tv_list:
                need_tv = need_tvs.get(need_tmdbid)
                if not need_tv:
                    continue
                index = 0
                for tv in need_tv:
                    need_season = tv.get("season") or 1
                    need_episodes = tv.get("episodes")
                    total_episodes = tv.get("total_episodes")
                    # 缺失整季的转化为缺失集进行比较
                    if not need_episodes:
                        need_episodes = list(range(1, total_episodes + 1))
                    for context in contexts:
                        media = context.media_info
                        meta = context.meta_info
                        if media.type == MediaType.MOVIE:
                            continue
                        if media.tmdb_id == need_tmdbid:
                            if context in downloaded_list:
                                continue
                            # 只处理单季含集的种子
                            item_season = meta.get_season_list()
                            if len(item_season) != 1 or item_season[0] != need_season:
                                continue
                            item_episodes = meta.get_episode_list()
                            if not item_episodes:
                                continue
                            # 为需要集的子集则下载
                            if set(item_episodes).issubset(set(need_episodes)):
                                download_id = __download(context)
                                if download_id:
                                    # 更新仍需集数
                                    need_episodes = __update_episodes(tmdbid=need_tmdbid,
                                                                      need=need_episodes,
                                                                      seq=index,
                                                                      current=item_episodes)
                    index += 1

        # 仍然缺失的剧集，从整季中选择需要的集数文件下载，仅支持QB和TR
        if need_tvs:
            need_tv_list = list(need_tvs)
            for need_tmdbid in need_tv_list:
                need_tv = need_tvs.get(need_tmdbid)
                if not need_tv:
                    continue
                index = 0
                for tv in need_tv:
                    need_season = tv.get("season") or 1
                    need_episodes = tv.get("episodes")
                    if not need_episodes:
                        continue
                    for context in contexts:
                        media = context.media_info
                        meta = context.meta_info
                        torrent = context.torrent_info
                        if media.type == MediaType.MOVIE:
                            continue
                        if context in downloaded_list:
                            continue
                        if not need_episodes:
                            break
                        # 选中一个单季整季的或单季包括需要的所有集的
                        if media.tmdb_id == need_tmdbid \
                                and (not meta.get_episode_list()
                                     or set(meta.get_episode_list()).intersection(set(need_episodes))) \
                                and len(meta.get_season_list()) == 1 \
                                and meta.get_season_list()[0] == need_season:
                            # 检查种子看是否有需要的集
                            torrent_path, torrent_files = __download_torrent(torrent)
                            if not torrent_path:
                                continue
                            # 种子全部集
                            torrent_episodes = self.torrent.get_torrent_episodes(torrent_files)
                            # 选中的集
                            selected_episodes = set(torrent_episodes).intersection(set(need_episodes))
                            if not selected_episodes:
                                logger.info(f"{meta.org_string} 没有需要的集，跳过...")
                                continue
                            # 添加下载
                            download_id = __download(_context=context,
                                                     _torrent_file=torrent_path,
                                                     _episodes=selected_episodes)
                            if not download_id:
                                continue
                            # 更新仍需集数
                            need_episodes = __update_episodes(tmdbid=need_tmdbid,
                                                              need=need_episodes,
                                                              seq=index,
                                                              current=selected_episodes)
                index += 1

        # 返回下载的资源，剩下没下完的
        return downloaded_list, need_tvs

    def get_no_exists_info(self, mediainfo: MediaInfo, no_exists: dict = None) -> Tuple[bool, dict]:
        """
        检查媒体库，查询是否存在，对于剧集同时返回不存在的季集信息
        :param mediainfo: 已识别的媒体信息
        :param no_exists: 在调用该方法前已经存储的不存在的季集信息，有传入时该函数搜索的内容将会叠加后输出
        :return: 当前媒体是否缺失，各标题总的季集和缺失的季集
        """

        def __append_no_exists(_season: int, _episodes: list):
            """
            添加不存在的季集信息
            """
            if not no_exists.get(mediainfo.tmdb_id):
                no_exists[mediainfo.tmdb_id] = [
                    {
                        "season": season,
                        "episodes": episodes,
                        "total_episodes": len(episodes)
                    }
                ]
            else:
                no_exists[mediainfo.tmdb_id].append({
                    "season": season,
                    "episodes": episodes,
                    "total_episodes": len(episodes)
                })

        if not no_exists:
            no_exists = {}
        if mediainfo.type == MediaType.MOVIE:
            # 电影
            exists_movies: Optional[dict] = self.run_module("media_exists", mediainfo)
            if exists_movies:
                logger.info(f"媒体库中已存在电影：{mediainfo.get_title_string()}")
                return True, {}
            return False, {}
        else:
            if not mediainfo.seasons:
                logger.error(f"媒体信息中没有季集信息：{mediainfo.get_title_string()}")
                return False, {}
            # 电视剧
            exists_tvs: Optional[dict] = self.run_module("media_exists", mediainfo)
            if not exists_tvs:
                # 所有剧集均缺失
                for season, episodes in mediainfo.seasons.items():
                    # 添加不存在的季集信息
                    __append_no_exists(season, episodes)
                return False, no_exists
            else:
                # 存在一些，检查缺失的季集
                for season, episodes in mediainfo.seasons.items():
                    exist_seasons = exists_tvs.get("seasons")
                    if exist_seasons.get(season):
                        # 取差集
                        episodes = set(episodes).difference(set(exist_seasons[season]))
                    # 添加不存在的季集信息
                    __append_no_exists(season, episodes)
            # 存在不完整的剧集
            if no_exists:
                return False, no_exists
            # 全部存在
            return True, no_exists
