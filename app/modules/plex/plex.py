import json
from pathlib import Path
from typing import List, Optional, Dict, Tuple, Generator, Any
from urllib.parse import quote_plus

from plexapi import media
from plexapi.server import PlexServer

from app.core.config import settings
from app.log import logger
from app.schemas import RefreshMediaItem, MediaType, WebhookEventInfo
from app.utils.singleton import Singleton


class Plex(metaclass=Singleton):

    def __init__(self):
        self._host = settings.PLEX_HOST
        if self._host:
            if not self._host.endswith("/"):
                self._host += "/"
            if not self._host.startswith("http"):
                self._host = "http://" + self._host
        self._token = settings.PLEX_TOKEN
        if self._host and self._token:
            try:
                self._plex = PlexServer(self._host, self._token)
                self._libraries = self._plex.library.sections()
            except Exception as e:
                self._plex = None
                logger.error(f"Plex服务器连接失败：{str(e)}")

    def get_librarys(self):
        """
        获取媒体服务器所有媒体库列表
        """
        if not self._plex:
            return []
        try:
            self._libraries = self._plex.library.sections()
        except Exception as err:
            logger.error(f"获取媒体服务器所有媒体库列表出错：{str(err)}")
            return []
        libraries = []
        for library in self._libraries:
            match library.type:
                case "movie":
                    library_type = MediaType.MOVIE.value
                case "show":
                    library_type = MediaType.TV.value
                case _:
                    continue
            libraries.append({
                "id": library.key,
                "name": library.title,
                "path": library.locations,
                "type": library_type
            })
        return libraries

    def get_activity_log(self, num: int = 30) -> Optional[List[dict]]:
        """
        获取Plex活动记录
        """
        if not self._plex:
            return []
        ret_array = []
        try:
            # type的含义: 1 电影 4 剧集单集 详见 plexapi/utils.py中SEARCHTYPES的定义
            # 根据最后播放时间倒序获取数据
            historys = self._plex.library.search(sort='lastViewedAt:desc', limit=num, type='1,4')
            for his in historys:
                # 过滤掉最后播放时间为空的
                if his.lastViewedAt:
                    if his.type == "episode":
                        event_title = "%s %s%s %s" % (
                            his.grandparentTitle,
                            "S" + str(his.parentIndex),
                            "E" + str(his.index),
                            his.title
                        )
                        event_str = "开始播放剧集 %s" % event_title
                    else:
                        event_title = "%s %s" % (
                            his.title, "(" + str(his.year) + ")")
                        event_str = "开始播放电影 %s" % event_title

                    event_type = "PL"
                    event_date = his.lastViewedAt.strftime('%Y-%m-%d %H:%M:%S')
                    activity = {"type": event_type, "event": event_str, "date": event_date}
                    ret_array.append(activity)
        except Exception as e:
            logger.error(f"连接System/ActivityLog/Entries出错：" + str(e))
            return []
        if ret_array:
            ret_array = sorted(ret_array, key=lambda x: x['date'], reverse=True)
        return ret_array

    def get_medias_count(self) -> dict:
        """
        获得电影、电视剧、动漫媒体数量
        :return: MovieCount SeriesCount SongCount
        """
        if not self._plex:
            return {}
        sections = self._plex.library.sections()
        MovieCount = SeriesCount = SongCount = EpisodeCount = 0
        for sec in sections:
            if sec.type == "movie":
                MovieCount += sec.totalSize
            if sec.type == "show":
                SeriesCount += sec.totalSize
                EpisodeCount += sec.totalViewSize(libtype='episode')
            if sec.type == "artist":
                SongCount += sec.totalSize
        return {
            "MovieCount": MovieCount,
            "SeriesCount": SeriesCount,
            "SongCount": SongCount,
            "EpisodeCount": EpisodeCount
        }

    def get_movies(self, title: str, year: str = None) -> Optional[List[dict]]:
        """
        根据标题和年份，检查电影是否在Plex中存在，存在则返回列表
        :param title: 标题
        :param year: 年份，为空则不过滤
        :return: 含title、year属性的字典列表
        """
        if not self._plex:
            return None
        ret_movies = []
        if year:
            movies = self._plex.library.search(title=title, year=year, libtype="movie")
        else:
            movies = self._plex.library.search(title=title, libtype="movie")
        for movie in movies:
            ret_movies.append({'title': movie.title, 'year': movie.year})
        return ret_movies

    def get_tv_episodes(self,
                        item_ids: List[str] = [],
                        title: str = None,
                        year: str = None,
                        season: int = None) -> Optional[Dict[int, list]]:
        """
        根据标题、年份、季查询电视剧所有集信息
        :param item_id: 媒体ID列表
        :param title: 标题
        :param year: 年份，可以为空，为空时不按年份过滤
        :param season: 季号，数字
        :return: 所有集的列表
        """
        if not self._plex:
            return {}
        if item_ids:
            videos = self._plex.library.sectionByID(item_ids[0]).all()
        else:
            videos = self._plex.library.search(title=title, year=year, libtype="show")
        if not videos:
            return {}
        episodes = videos[0].episodes()
        season_episodes = {}
        for episode in episodes:
            if season and episode.seasonNumber != int(season):
                continue
            if episode.seasonNumber not in season_episodes:
                season_episodes[episode.seasonNumber] = []
            season_episodes[episode.seasonNumber].append(episode.index)
        return season_episodes

    def get_remote_image_by_id(self, item_id: str, image_type: str) -> Optional[str]:
        """
        根据ItemId从Plex查询图片地址
        :param item_id: 在Emby中的ID
        :param image_type: 图片的类型，Poster或者Backdrop等
        :return: 图片对应在TMDB中的URL
        """
        if not self._plex:
            return None
        try:
            if image_type == "Poster":
                images = self._plex.fetchItems('/library/metadata/%s/posters' % item_id, cls=media.Poster)
            else:
                images = self._plex.fetchItems('/library/metadata/%s/arts' % item_id, cls=media.Art)
            for image in images:
                if hasattr(image, 'key') and image.key.startswith('http'):
                    return image.key
        except Exception as e:
            logger.error(f"获取封面出错：" + str(e))
        return None

    def refresh_root_library(self) -> bool:
        """
        通知Plex刷新整个媒体库
        """
        if not self._plex:
            return False
        return self._plex.library.update()

    def refresh_library_by_items(self, items: List[RefreshMediaItem]) -> bool:
        """
        按路径刷新媒体库 item: target_path
        """
        if not self._plex:
            return False
        result_dict = {}
        for item in items:
            file_path = item.target_path
            lib_key, path = self.__find_librarie(file_path, self._libraries)
            # 如果存在同一剧集的多集,key(path)相同会合并
            result_dict[path] = lib_key
        if "" in result_dict:
            # 如果有匹配失败的,刷新整个库
            self._plex.library.update()
        else:
            # 否则一个一个刷新
            for path, lib_key in result_dict.items():
                logger.info(f"刷新媒体库：{lib_key} - {path}")
                self._plex.query(f'/library/sections/{lib_key}/refresh?path={quote_plus(path)}')

    @staticmethod
    def __find_librarie(path: Path, libraries: List[Any]) -> Tuple[str, str]:
        """
        判断这个path属于哪个媒体库
        多个媒体库配置的目录不应有重复和嵌套,
        """

        def is_subpath(_path: Path, _parent: Path) -> bool:
            """
            判断_path是否是_parent的子目录下
            """
            _path = _path.resolve()
            _parent = _parent.resolve()
            return _path.parts[:len(_parent.parts)] == _parent.parts

        if path is None:
            return "", ""

        try:
            for lib in libraries:
                if hasattr(lib, "locations") and lib.locations:
                    for location in lib.locations:
                        if is_subpath(path, Path(location)):
                            return lib.key, location
        except Exception as err:
            logger.error(f"查找媒体库出错：{err}")
        return "", ""

    def get_iteminfo(self, itemid: str) -> dict:
        """
        获取单个项目详情
        """
        if not self._plex:
            return {}
        try:
            item = self._plex.fetchItem(itemid)
            ids = self.__get_ids(item.guids)
            return {'ProviderIds': {'Tmdb': ids['tmdb_id'], 'Imdb': ids['imdb_id']}}
        except Exception as err:
            logger.error(f"获取项目详情出错：{err}")
            return {}

    @staticmethod
    def __get_ids(guids: List[Any]) -> dict:
        guid_mapping = {
            "imdb://": "imdb_id",
            "tmdb://": "tmdb_id",
            "tvdb://": "tvdb_id"
        }
        ids = {}
        for prefix, varname in guid_mapping.items():
            ids[varname] = None
        for guid in guids:
            for prefix, varname in guid_mapping.items():
                if isinstance(guid, dict):
                    if guid['id'].startswith(prefix):
                        # 找到匹配的ID
                        ids[varname] = guid['id'][len(prefix):]
                        break
                else:
                    if guid.id.startswith(prefix):
                        # 找到匹配的ID
                        ids[varname] = guid.id[len(prefix):]
                        break
        return ids

    def get_items(self, parent: str) -> Generator:
        """
        获取媒体服务器所有媒体库列表
        """
        if not parent:
            yield {}
        if not self._plex:
            yield {}
        try:
            section = self._plex.library.sectionByID(int(parent))
            if section:
                for item in section.all():
                    if not item:
                        continue
                    ids = self.__get_ids(item.guids)
                    path = None
                    if item.locations:
                        path = item.locations[0]
                    yield {"id": item.key,
                           "library": item.librarySectionID,
                           "type": item.type,
                           "title": item.title,
                           "original_title": item.originalTitle,
                           "year": item.year,
                           "tmdbid": ids['tmdb_id'],
                           "imdbid": ids['imdb_id'],
                           "tvdbid": ids['tvdb_id'],
                           "path": path}
        except Exception as err:
            logger.error(f"获取媒体库列表出错：{err}")
        yield {}

    def get_webhook_message(self, message_str: str) -> WebhookEventInfo:
        """
        解析Plex报文
        eventItem  字段的含义
        event      事件类型
        item_type  媒体类型 TV,MOV
        item_name  TV:琅琊榜 S1E6 剖心明志 虎口脱险
                   MOV:猪猪侠大冒险(2001)
        overview   剧情描述
        """
        message = json.loads(message_str)
        eventItem = WebhookEventInfo(event=message.get('Event', ''), channel="plex")
        if message.get('Metadata'):
            if message.get('Metadata', {}).get('type') == 'episode':
                eventItem.item_type = "TV"
                eventItem.item_name = "%s %s%s %s" % (
                    message.get('Metadata', {}).get('grandparentTitle'),
                    "S" + str(message.get('Metadata', {}).get('parentIndex')),
                    "E" + str(message.get('Metadata', {}).get('index')),
                    message.get('Metadata', {}).get('title'))
                eventItem.item_id = message.get('Metadata', {}).get('ratingKey')
                eventItem.season_id = message.get('Metadata', {}).get('parentIndex')
                eventItem.episode_id = message.get('Metadata', {}).get('index')

                if message.get('Metadata', {}).get('summary') and len(message.get('Metadata', {}).get('summary')) > 100:
                    eventItem.overview = str(message.get('Metadata', {}).get('summary'))[:100] + "..."
                else:
                    eventItem.overview = message.get('Metadata', {}).get('summary')
            else:
                eventItem.item_type = "MOV" if message.get('Metadata', {}).get('type') == 'movie' else "SHOW"
                eventItem.item_name = "%s %s" % (
                    message.get('Metadata', {}).get('title'), "(" + str(message.get('Metadata', {}).get('year')) + ")")
                eventItem.item_id = message.get('Metadata', {}).get('ratingKey')
                if len(message.get('Metadata', {}).get('summary')) > 100:
                    eventItem.overview = str(message.get('Metadata', {}).get('summary'))[:100] + "..."
                else:
                    eventItem.overview = message.get('Metadata', {}).get('summary')
        if message.get('Player'):
            eventItem.ip = message.get('Player').get('publicAddress')
            eventItem.client = message.get('Player').get('title')
            # 这里给个空,防止拼消息的时候出现None
            eventItem.device_name = ' '
        if message.get('Account'):
            eventItem.user_name = message.get("Account").get('title')

        # 获取消息图片
        if eventItem.item_id:
            # 根据返回的item_id去调用媒体服务器获取
            eventItem.image_url = self.get_remote_image_by_id(item_id=eventItem.item_id,
                                                              image_type="Backdrop")

        return eventItem

    def get_plex(self):
        """
        获取plex对象，以便直接操作
        """
        return self._plex
