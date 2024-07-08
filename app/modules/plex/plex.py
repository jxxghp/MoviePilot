import json
from pathlib import Path
from typing import List, Optional, Dict, Tuple, Generator, Any
from urllib.parse import quote_plus

from cachetools import TTLCache, cached
from plexapi import media
from plexapi.server import PlexServer
from requests import Response, Session

from app import schemas
from app.core.config import settings
from app.log import logger
from app.schemas import MediaType
from app.utils.http import RequestUtils


class Plex:
    _plex = None
    _session = None

    def __init__(self):
        self._host = settings.PLEX_HOST
        if self._host:
            self._host = RequestUtils.standardize_base_url(self._host)
        self._playhost = settings.PLEX_PLAY_HOST
        if self._playhost:
            self._playhost = RequestUtils.standardize_base_url(self._playhost)
        self._token = settings.PLEX_TOKEN
        if self._host and self._token:
            try:
                self._plex = PlexServer(self._host, self._token)
                self._libraries = self._plex.library.sections()
            except Exception as e:
                self._plex = None
                logger.error(f"Plex服务器连接失败：{str(e)}")
            self._session = self.__adapt_plex_session()

    def is_inactive(self) -> bool:
        """
        判断是否需要重连
        """
        if not self._host or not self._token:
            return False
        return True if not self._plex else False

    def reconnect(self):
        """
        重连
        """
        try:
            self._plex = PlexServer(self._host, self._token)
            self._libraries = self._plex.library.sections()
        except Exception as e:
            self._plex = None
            logger.error(f"Plex服务器连接失败：{str(e)}")

    @cached(cache=TTLCache(maxsize=100, ttl=86400))
    def __get_library_images(self, library_key: str, mtype: int) -> Optional[List[str]]:
        """
        获取媒体服务器最近添加的媒体的图片列表
        param: library_key
        param: type type的含义: 1 电影 2 剧集 详见 plexapi/utils.py中SEARCHTYPES的定义
        """
        if not self._plex:
            return None
        # 返回结果
        poster_urls = {}
        # 页码计数
        container_start = 0
        # 需要的总条数/每页的条数
        total_size = 4
        # 如果总数不足,接续获取下一页
        while len(poster_urls) < total_size:
            items = self._plex.fetchItems(f"/hubs/home/recentlyAdded?type={mtype}&sectionID={library_key}",
                                          container_start=container_start,
                                          container_size=8,
                                          maxresults=8)
            for item in items:
                if item.type == "episode":
                    # 如果是剧集的单集,则去找上级的图片
                    if item.parentThumb is not None:
                        poster_urls[item.parentThumb] = None
                else:
                    # 否则就用自己的图片
                    if item.thumb is not None:
                        poster_urls[item.thumb] = None
                if len(poster_urls) == total_size:
                    break
            if len(items) < total_size:
                break
            container_start += total_size
        return [f"{self._host.rstrip('/') + url}?X-Plex-Token={self._token}" for url in
                list(poster_urls.keys())[:total_size]]

    def get_librarys(self) -> List[schemas.MediaServerLibrary]:
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
        black_list = (settings.MEDIASERVER_SYNC_BLACKLIST or '').split(",")
        for library in self._libraries:
            if library.title in black_list:
                continue
            match library.type:
                case "movie":
                    library_type = MediaType.MOVIE.value
                    image_list = self.__get_library_images(library.key, 1)
                case "show":
                    library_type = MediaType.TV.value
                    image_list = self.__get_library_images(library.key, 2)
                case _:
                    continue
            libraries.append(
                schemas.MediaServerLibrary(
                    id=library.key,
                    name=library.title,
                    path=library.locations,
                    type=library_type,
                    image_list=image_list,
                    link=f"{self._playhost or self._host}web/index.html#!/media/{self._plex.machineIdentifier}"
                         f"/com.plexapp.plugins.library?source={library.key}"
                )
            )
        return libraries

    def get_medias_count(self) -> schemas.Statistic:
        """
        获得电影、电视剧、动漫媒体数量
        :return: MovieCount SeriesCount SongCount
        """
        if not self._plex:
            return schemas.Statistic()
        sections = self._plex.library.sections()
        MovieCount = SeriesCount = EpisodeCount = 0
        # 媒体库白名单
        allow_library = [lib.id for lib in self.get_librarys()]
        for sec in sections:
            if str(sec.key) not in allow_library:
                continue
            if sec.type == "movie":
                MovieCount += sec.totalSize
            if sec.type == "show":
                SeriesCount += sec.totalSize
                EpisodeCount += sec.totalViewSize(libtype='episode')
        return schemas.Statistic(
            movie_count=MovieCount,
            tv_count=SeriesCount,
            episode_count=EpisodeCount
        )

    def get_movies(self,
                   title: str,
                   original_title: str = None,
                   year: str = None,
                   tmdb_id: int = None) -> Optional[List[schemas.MediaServerItem]]:
        """
        根据标题和年份，检查电影是否在Plex中存在，存在则返回列表
        :param title: 标题
        :param original_title: 原产地标题
        :param year: 年份，为空则不过滤
        :param tmdb_id: TMDB ID
        :return: 含title、year属性的字典列表
        """
        if not self._plex:
            return None
        ret_movies = []
        if year:
            movies = self._plex.library.search(title=title,
                                               year=year,
                                               libtype="movie")
            # 根据原标题再查一遍
            if original_title and str(original_title) != str(title):
                movies.extend(self._plex.library.search(title=original_title,
                                                        year=year,
                                                        libtype="movie"))
        else:
            movies = self._plex.library.search(title=title,
                                               libtype="movie")
            if original_title and str(original_title) != str(title):
                movies.extend(self._plex.library.search(title=original_title,
                                                        libtype="movie"))
        for item in set(movies):
            ids = self.__get_ids(item.guids)
            if tmdb_id and ids['tmdb_id']:
                if str(ids['tmdb_id']) != str(tmdb_id):
                    continue
            path = None
            if item.locations:
                path = item.locations[0]
            ret_movies.append(
                schemas.MediaServerItem(
                    server="plex",
                    library=item.librarySectionID,
                    item_id=item.key,
                    item_type=item.type,
                    title=item.title,
                    original_title=item.originalTitle,
                    year=item.year,
                    tmdbid=ids['tmdb_id'],
                    imdbid=ids['imdb_id'],
                    tvdbid=ids['tvdb_id'],
                    path=path,
                )
            )
        return ret_movies

    def get_tv_episodes(self,
                        item_id: str = None,
                        title: str = None,
                        original_title: str = None,
                        year: str = None,
                        tmdb_id: int = None,
                        season: int = None) -> Tuple[Optional[str], Optional[Dict[int, list]]]:
        """
        根据标题、年份、季查询电视剧所有集信息
        :param item_id: 媒体ID
        :param title: 标题
        :param original_title: 原产地标题
        :param year: 年份，可以为空，为空时不按年份过滤
        :param tmdb_id: TMDB ID
        :param season: 季号，数字
        :return: 所有集的列表
        """
        if not self._plex:
            return None, {}
        if item_id:
            videos = self._plex.fetchItem(item_id)
        else:
            # 兼容年份为空的场景
            kwargs = {"year": year} if year else {}
            # 根据标题和年份模糊搜索，该结果不够准确
            videos = self._plex.library.search(title=title,
                                               libtype="show",
                                               **kwargs)
            if (not videos
                    and original_title
                    and str(original_title) != str(title)):
                videos = self._plex.library.search(title=original_title,
                                                   libtype="show",
                                                   **kwargs)

        if not videos:
            return None, {}
        if isinstance(videos, list):
            videos = videos[0]
        video_tmdbid = self.__get_ids(videos.guids).get('tmdb_id')
        if tmdb_id and video_tmdbid:
            if str(video_tmdbid) != str(tmdb_id):
                return None, {}
        episodes = videos.episodes()
        season_episodes = {}
        for episode in episodes:
            if season and episode.seasonNumber != int(season):
                continue
            if episode.seasonNumber not in season_episodes:
                season_episodes[episode.seasonNumber] = []
            season_episodes[episode.seasonNumber].append(episode.index)
        return videos.key, season_episodes

    def get_remote_image_by_id(self, item_id: str, image_type: str, depth: int = 0) -> Optional[str]:
        """
        根据ItemId从Plex查询图片地址
        :param item_id: 在Plex中的ID
        :param image_type: 图片的类型，Poster或者Backdrop等
        :param depth: 当前递归深度，默认为0
        :return: 图片对应在TMDB中的URL
        """
        if not self._plex or depth > 2 or not item_id:
            return None
        try:
            image_url = None
            ekey = f"/library/metadata/{item_id}"
            item = self._plex.fetchItem(ekey=ekey)
            if not item:
                return None
            # 如果配置了外网播放地址以及Token，则默认从Plex媒体服务器获取图片，否则返回有外网地址的图片资源
            if settings.PLEX_PLAY_HOST and settings.PLEX_TOKEN:
                query = {"X-Plex-Token": settings.PLEX_TOKEN}
                if image_type == "Poster":
                    if item.thumb:
                        image_url = RequestUtils.combine_url(host=settings.PLEX_PLAY_HOST, path=item.thumb, query=query)
                else:
                    # 默认使用art也就是Backdrop进行处理
                    if item.art:
                        image_url = RequestUtils.combine_url(host=settings.PLEX_PLAY_HOST, path=item.art, query=query)
                    # 这里对episode进行特殊处理，实际上episode的Backdrop是Poster
                    # 也有个别情况，比如机智的凡人小子episode就是Poster，因此这里把episode的优先级降低，默认还是取art
                    if not image_url and item.TYPE == "episode" and item.thumb:
                        image_url = RequestUtils.combine_url(host=settings.PLEX_PLAY_HOST, path=item.thumb, query=query)
            else:
                if image_type == "Poster":
                    images = self._plex.fetchItems(ekey=f"{ekey}/posters",
                                                   cls=media.Poster)
                else:
                    # 默认使用art也就是Backdrop进行处理
                    images = self._plex.fetchItems(ekey=f"{ekey}/arts",
                                                   cls=media.Art)
                    # 这里对episode进行特殊处理，实际上episode的Backdrop是Poster
                    # 也有个别情况，比如机智的凡人小子episode就是Poster，因此这里把episode的优先级降低，默认还是取art
                    if not images and item.TYPE == "episode":
                        images = self._plex.fetchItems(ekey=f"{ekey}/posters",
                                                       cls=media.Poster)
                for image in images:
                    if hasattr(image, "key") and image.key.startswith("http"):
                        image_url = image.key
                        break
                # 如果最后还是找不到，则递归父级进行查找
                if not image_url and hasattr(item, "parentRatingKey"):
                    return self.get_remote_image_by_id(item_id=item.parentRatingKey,
                                                       image_type=image_type,
                                                       depth=depth + 1)
            return image_url
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

    def refresh_library_by_items(self, items: List[schemas.RefreshMediaItem]) -> bool:
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
                self._plex.query(f'/library/sections/{lib_key}/refresh?path={quote_plus(str(Path(path).parent))}')

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
                            return lib.key, str(path)
        except Exception as err:
            logger.error(f"查找媒体库出错：{str(err)}")
        return "", ""

    def get_iteminfo(self, itemid: str) -> Optional[schemas.MediaServerItem]:
        """
        获取单个项目详情
        """
        if not self._plex:
            return None
        try:
            item = self._plex.fetchItem(itemid)
            ids = self.__get_ids(item.guids)
            path = None
            if item.locations:
                path = item.locations[0]
            return schemas.MediaServerItem(
                server="plex",
                library=item.librarySectionID,
                item_id=item.key,
                item_type=item.type,
                title=item.title,
                original_title=item.originalTitle,
                year=item.year,
                tmdbid=ids['tmdb_id'],
                imdbid=ids['imdb_id'],
                tvdbid=ids['tvdb_id'],
                path=path,
            )
        except Exception as err:
            logger.error(f"获取项目详情出错：{str(err)}")
        return None

    @staticmethod
    def __get_ids(guids: List[Any]) -> dict:
        def parse_tmdb_id(value: str) -> (bool, int):
            """尝试将TMDB ID字符串转换为整数。如果成功，返回(True, int)，失败则返回(False, None)。"""
            try:
                int_value = int(value)
                return True, int_value
            except ValueError:
                return False, None

        guid_mapping = {
            "imdb://": "imdb_id",
            "tmdb://": "tmdb_id",
            "tvdb://": "tvdb_id"
        }
        ids = {varname: None for varname in guid_mapping.values()}
        for guid in guids:
            guid_id = guid['id'] if isinstance(guid, dict) else guid.id
            for prefix, varname in guid_mapping.items():
                if guid_id.startswith(prefix):
                    clean_id = guid_id[len(prefix):]
                    if varname == "tmdb_id":
                        # tmdb_id为int，Plex可能存在脏数据，特别处理tmdb_id
                        success, parsed_id = parse_tmdb_id(clean_id)
                        if success:
                            ids[varname] = parsed_id
                    else:
                        ids[varname] = clean_id
                    break

        return ids

    def get_items(self, parent: str) -> Generator:
        """
        获取媒体服务器所有媒体库列表
        """
        if not parent:
            yield None
        if not self._plex:
            yield None
        try:
            section = self._plex.library.sectionByID(int(parent))
            if section:
                for item in section.all():
                    try:
                        if not item:
                            continue
                        ids = self.__get_ids(item.guids)
                        path = None
                        if item.locations:
                            path = item.locations[0]
                        yield schemas.MediaServerItem(
                            server="plex",
                            library=item.librarySectionID,
                            item_id=item.key,
                            item_type=item.type,
                            title=item.title,
                            original_title=item.originalTitle,
                            year=item.year,
                            tmdbid=ids['tmdb_id'],
                            imdbid=ids['imdb_id'],
                            tvdbid=ids['tvdb_id'],
                            path=path,
                        )
                    except Exception as e:
                        logger.error(f"处理媒体项目时出错：{str(e)}, 跳过此项目。")
                        continue
        except Exception as err:
            logger.error(f"获取媒体库列表出错：{str(err)}")
        yield None

    def get_webhook_message(self, form: any) -> Optional[schemas.WebhookEventInfo]:
        """
        解析Plex报文
        eventItem  字段的含义
        event      事件类型
        item_type  媒体类型 TV,MOV
        item_name  TV:琅琊榜 S1E6 剖心明志 虎口脱险
                   MOV:猪猪侠大冒险(2001)
        overview   剧情描述
        {
          "event": "media.scrobble",
          "user": false,
          "owner": true,
          "Account": {
            "id": 31646104,
            "thumb": "https://plex.tv/users/xx",
            "title": "播放"
          },
          "Server": {
            "title": "Media-Server",
            "uuid": "xxxx"
          },
          "Player": {
            "local": false,
            "publicAddress": "xx.xx.xx.xx",
            "title": "MagicBook",
            "uuid": "wu0uoa1ujfq90t0c5p9f7fw0"
          },
          "Metadata": {
            "librarySectionType": "show",
            "ratingKey": "40294",
            "key": "/library/metadata/40294",
            "parentRatingKey": "40291",
            "grandparentRatingKey": "40275",
            "guid": "plex://episode/615580a9fa828e7f1a0caabd",
            "parentGuid": "plex://season/615580a9fa828e7f1a0caab8",
            "grandparentGuid": "plex://show/60e81fd8d8000e002d7d2976",
            "type": "episode",
            "title": "The World's Strongest Senior",
            "titleSort": "World's Strongest Senior",
            "grandparentKey": "/library/metadata/40275",
            "parentKey": "/library/metadata/40291",
            "librarySectionTitle": "动漫剧集",
            "librarySectionID": 7,
            "librarySectionKey": "/library/sections/7",
            "grandparentTitle": "范马刃牙",
            "parentTitle": "Combat Shadow Fighting Saga / Great Prison Battle Saga",
            "originalTitle": "Baki Hanma",
            "contentRating": "TV-MA",
            "summary": "The world is shaken by news",
            "index": 1,
            "parentIndex": 1,
            "audienceRating": 8.5,
            "viewCount": 1,
            "lastViewedAt": 1694320444,
            "year": 2021,
            "thumb": "/library/metadata/40294/thumb/1693544504",
            "art": "/library/metadata/40275/art/1693952979",
            "parentThumb": "/library/metadata/40291/thumb/1691115271",
            "grandparentThumb": "/library/metadata/40275/thumb/1693952979",
            "grandparentArt": "/library/metadata/40275/art/1693952979",
            "duration": 1500000,
            "originallyAvailableAt": "2021-09-30",
            "addedAt": 1691115281,
            "updatedAt": 1693544504,
            "audienceRatingImage": "themoviedb://image.rating",
            "Guid": [
              {
                "id": "imdb://tt14765720"
              },
              {
                "id": "tmdb://3087250"
              },
              {
                "id": "tvdb://8530933"
              }
            ],
            "Rating": [
              {
                "image": "themoviedb://image.rating",
                "value": 8.5,
                "type": "audience"
              }
            ],
            "Director": [
              {
                "id": 115144,
                "filter": "director=115144",
                "tag": "Keiya Saito",
                "tagKey": "5f401c8d04a86500409ea6c1"
              }
            ],
            "Writer": [
              {
                "id": 115135,
                "filter": "writer=115135",
                "tag": "Tatsuhiko Urahata",
                "tagKey": "5d7768e07a53e9001e6db1ce",
                "thumb": "https://metadata-static.plex.tv/f/people/f6f90dc89fa87d459f85d40a09720c05.jpg"
              }
            ]
          }
        }
        """
        if not form:
            return None
        payload = form.get("payload")
        if not payload:
            return None
        try:
            message = json.loads(payload)
        except Exception as e:
            logger.debug(f"解析plex webhook出错：{str(e)}")
            return None
        eventType = message.get('event')
        if not eventType:
            return None
        logger.debug(f"接收到plex webhook：{message}")
        eventItem = schemas.WebhookEventInfo(event=eventType, channel="plex")
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

                if (message.get('Metadata', {}).get('summary')
                        and len(message.get('Metadata', {}).get('summary')) > 100):
                    eventItem.overview = str(message.get('Metadata', {}).get('summary'))[:100] + "..."
                else:
                    eventItem.overview = message.get('Metadata', {}).get('summary')
            else:
                eventItem.item_type = "MOV" if message.get('Metadata',
                                                           {}).get('type') == 'movie' else "SHOW"
                eventItem.item_name = "%s %s" % (
                    message.get('Metadata', {}).get('title'),
                    "(" + str(message.get('Metadata', {}).get('year')) + ")")
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

    def get_play_url(self, item_id: str) -> str:
        """
        拼装媒体播放链接
        :param item_id: 媒体的的ID
        """
        return f'{self._playhost or self._host}web/index.html#!/server/{self._plex.machineIdentifier}/details?key={item_id}'

    def get_resume(self, num: int = 12) -> Optional[List[schemas.MediaServerPlayItem]]:
        """
        获取继续观看的媒体
        """
        if not self._plex:
            return []
        # 媒体库白名单
        allow_library = ",".join([lib.id for lib in self.get_librarys()])
        params = {"contentDirectoryID": allow_library}
        items = self._plex.fetchItems("/hubs/continueWatching/items",
                                      container_start=0,
                                      container_size=num,
                                      maxresults=num,
                                      params=params)
        ret_resume = []
        for item in items:
            item_type = MediaType.MOVIE.value if item.TYPE == "movie" else MediaType.TV.value
            if item_type == MediaType.MOVIE.value:
                title = item.title
                subtitle = item.year
            else:
                title = item.grandparentTitle
                subtitle = f"S{item.parentIndex}:E{item.index} - {item.title}"
            link = self.get_play_url(item.key)
            image = item.artUrl
            ret_resume.append(schemas.MediaServerPlayItem(
                id=item.key,
                title=title,
                subtitle=subtitle,
                type=item_type,
                image=image,
                link=link,
                percent=item.viewOffset / item.duration * 100 if item.viewOffset and item.duration else 0
            ))
        return ret_resume[:num]

    def get_latest(self, num: int = 20) -> Optional[List[schemas.MediaServerPlayItem]]:
        """
        获取最近添加媒体
        """
        if not self._plex:
            return None
        # 请求参数（除黑名单）
        allow_library = ",".join([lib.id for lib in self.get_librarys()])
        params = {
            "contentDirectoryID": allow_library,
            "count": num,
            "excludeContinueWatching": 1
        }
        ret_resume = []
        sub_result = []
        offset = 0
        while True:
            if len(ret_resume) >= num:
                break
            # 获取所有资料库
            hubs = self._plex.fetchItems(
                '/hubs/promoted',
                container_start=offset,
                container_size=num,
                maxresults=num,
                params=params
            )
            if len(hubs) == 0:
                break

            # 合并排序
            for hub in hubs:
                for item in hub.items:
                    sub_result.append(item)
            sub_result.sort(key=lambda x: x.addedAt, reverse=True)

            for item in sub_result:
                if len(ret_resume) >= num:
                    break
                item_type, title, image = "", "", ""
                if item.TYPE == "movie":
                    item_type = MediaType.MOVIE.value
                    title = item.title
                    image = item.posterUrl
                elif item.TYPE == "season":
                    item_type = MediaType.TV.value
                    title = "%s 第%s季" % (item.parentTitle, item.index)
                    image = item.posterUrl
                elif item.TYPE == "episode":
                    item_type = MediaType.TV.value
                    title = "%s 第%s季 第%s集" % (item.grandparentTitle, item.parentIndex, item.index)
                    thumb = (item.parentThumb or item.grandparentThumb or '').lstrip('/')
                    image = (self._host + thumb + f"?X-Plex-Token={self._token}")
                elif item.TYPE == "show":
                    item_type = MediaType.TV.value
                    title = "%s 共%s季" % (item.title, item.seasonCount)
                    image = item.posterUrl
                link = self.get_play_url(item.key)
                ret_resume.append(schemas.MediaServerPlayItem(
                    id=item.key,
                    title=title,
                    subtitle=item.year,
                    type=item_type,
                    image=image,
                    link=link
                ))
            offset += num
        return ret_resume[:num]

    def get_data(self, endpoint: str, **kwargs) -> Optional[Response]:
        """
        自定义从媒体服务器获取数据
        :param endpoint: 端点
        :param kwargs: 其他请求参数，如headers, cookies, proxies等
        """
        return self.__request(method="get", endpoint=endpoint, **kwargs)

    def post_data(self, endpoint: str, **kwargs) -> Optional[Response]:
        """
        自定义从媒体服务器获取数据
        :param endpoint: 端点
        :param kwargs: 其他请求参数，如headers, cookies, proxies等
        """
        return self.__request(method="post", endpoint=endpoint, **kwargs)

    def put_data(self, endpoint: str, **kwargs) -> Optional[Response]:
        """
        自定义从媒体服务器获取数据
        :param endpoint: 端点
        :param kwargs: 其他请求参数，如headers, cookies, proxies等
        """
        return self.__request(method="put", endpoint=endpoint, **kwargs)

    def __request(self, method: str, endpoint: str, **kwargs) -> Optional[Response]:
        """
        自定义从媒体服务器获取数据
        :param method: HTTP方法，如 get, post, put 等
        :param endpoint: 端点
        :param kwargs: 其他请求参数，如headers, cookies, proxies等
        """
        if not self._session:
            return
        try:
            url = RequestUtils.adapt_request_url(host=self._host, endpoint=endpoint)
            kwargs.setdefault("headers", self.__get_request_headers())
            kwargs.setdefault("raise_exception", True)
            request_method = getattr(RequestUtils(session=self._session), f"{method}_res", None)
            if request_method:
                return request_method(url=url, **kwargs)
            else:
                logger.error(f"方法 {method} 不存在")
                return None
        except Exception as e:
            logger.error(f"连接Plex出错：" + str(e))
            return None

    @staticmethod
    def __get_request_headers() -> dict:
        """获取请求头"""
        return {
            "X-Plex-Token": settings.PLEX_TOKEN,
            "Accept": "application/json",
            "Content-Type": "application/json"
        }

    @staticmethod
    def __adapt_plex_session() -> Session:
        """
        创建并配置一个针对Plex服务的requests.Session实例
        这个会话包括特定的头部信息，用于处理所有的Plex请求
        """
        # 设置请求头部，通常包括验证令牌和接受/内容类型头部
        headers = Plex.__get_request_headers()
        session = Session()
        session.headers = headers
        return session
