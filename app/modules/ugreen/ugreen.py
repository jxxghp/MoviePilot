import hashlib
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Generator, List, Mapping, Optional, Union
from urllib.parse import parse_qs, urlparse

from app import schemas
from app.db.systemconfig_oper import SystemConfigOper
from app.log import logger
from app.modules.ugreen.api import Api
from app.schemas import MediaType
from app.schemas.types import SystemConfigKey
from app.utils.url import UrlUtils


class Ugreen:
    _username: Optional[str] = None
    _password: Optional[str] = None

    _userinfo: Optional[dict] = None
    _host: Optional[str] = None
    _playhost: Optional[str] = None

    _libraries: dict[str, dict] = {}
    _library_paths: dict[str, str] = {}
    _sync_libraries: List[str] = []
    _scan_type: int = 2
    _verify_ssl: bool = True

    _api: Optional[Api] = None

    def __init__(
        self,
        host: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        play_host: Optional[str] = None,
        sync_libraries: Optional[list] = None,
        scan_mode: Optional[Union[str, int]] = None,
        scan_type: Optional[Union[str, int]] = None,
        verify_ssl: Optional[Union[bool, str, int]] = True,
        **kwargs,
    ):
        if not host or not username or not password:
            logger.error("绿联影视配置不完整！！")
            return

        self._host = host
        self._username = username
        self._password = password
        self._sync_libraries = sync_libraries or []
        # 绿联媒体库扫描模式：
        # 1 新添加和修改、2 补充缺失、3 覆盖扫描
        self._scan_type = self.__resolve_scan_type(scan_mode=scan_mode, scan_type=scan_type)
        # HTTPS 证书校验开关：默认开启，仅兼容自签证书等场景下可关闭。
        self._verify_ssl = self.__resolve_verify_ssl(verify_ssl)

        if play_host:
            self._playhost = UrlUtils.standardize_base_url(play_host).rstrip("/")

        if not self.reconnect():
            logger.error(f"请检查服务端地址 {host}")

    @property
    def api(self) -> Optional[Api]:
        return self._api

    def close(self):
        self.disconnect()

    def is_configured(self) -> bool:
        return bool(self._host and self._username and self._password)

    def is_authenticated(self) -> bool:
        return (
            self.is_configured()
            and self._api is not None
            and self._api.token is not None
            and self._userinfo is not None
        )

    def is_inactive(self) -> bool:
        if not self.is_authenticated():
            return True
        self._userinfo = self._api.current_user() if self._api else None
        return self._userinfo is None

    def __session_cache_key(self) -> str:
        """
        生成当前绿联实例的会话缓存键（基于 host + username）。
        """
        normalized_host = UrlUtils.standardize_base_url(self._host or "").rstrip("/").lower()
        username = (self._username or "").strip().lower()
        raw = f"{normalized_host}|{username}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def __password_digest(self) -> str:
        """
        存储密码摘要用于检测配置是否变更，避免明文落盘。
        """
        return hashlib.sha256((self._password or "").encode("utf-8")).hexdigest()

    @staticmethod
    def __load_all_session_cache() -> dict:
        sessions = SystemConfigOper().get(SystemConfigKey.UgreenSessionCache)
        return sessions if isinstance(sessions, dict) else {}

    @staticmethod
    def __save_all_session_cache(sessions: dict):
        SystemConfigOper().set(SystemConfigKey.UgreenSessionCache, sessions)

    def __remove_persisted_session(self):
        cache_key = self.__session_cache_key()
        sessions = self.__load_all_session_cache()
        if cache_key in sessions:
            sessions.pop(cache_key, None)
            self.__save_all_session_cache(sessions)

    def __save_persisted_session(self):
        if not self._api:
            return
        session_state = self._api.export_session_state()
        if not session_state:
            return

        sessions = self.__load_all_session_cache()
        cache_key = self.__session_cache_key()
        sessions[cache_key] = {
            **session_state,
            "host": UrlUtils.standardize_base_url(self._host or "").rstrip("/"),
            "username": self._username,
            "password_digest": self.__password_digest(),
            "updated_at": int(datetime.now().timestamp()),
        }
        self.__save_all_session_cache(sessions)

    def __restore_persisted_session(self) -> bool:
        cache_key = self.__session_cache_key()
        sessions = self.__load_all_session_cache()
        cached = sessions.get(cache_key)
        if not isinstance(cached, Mapping):
            return False

        # 配置变更（尤其密码变更）后，不复用旧会话
        if cached.get("password_digest") != self.__password_digest():
            logger.info(f"绿联影视 {self._username} 检测到密码变更，清理旧会话缓存")
            self.__remove_persisted_session()
            return False

        api = Api(host=self._host, verify_ssl=self._verify_ssl)
        if not api.import_session_state(cached):
            api.close()
            self.__remove_persisted_session()
            return False

        userinfo = api.current_user()
        if not userinfo:
            # 会话失效，清理缓存并走正常登录
            api.close()
            self.__remove_persisted_session()
            logger.info(f"绿联影视 {self._username} 持久化会话已失效，准备重新登录")
            return False

        self._api = api
        self._userinfo = userinfo
        logger.debug(f"{self._username} 已复用绿联影视持久化会话")
        return True

    def reconnect(self) -> bool:
        if not self.is_configured():
            return False

        # 关闭旧连接（不主动登出，避免破坏可复用会话）
        self.disconnect(logout=False)

        if self.__restore_persisted_session():
            self.get_librarys()
            return True

        self._api = Api(host=self._host, verify_ssl=self._verify_ssl)
        if self._api.login(self._username, self._password) is None:
            self.__remove_persisted_session()
            return False

        self._userinfo = self._api.current_user()
        if not self._userinfo:
            self.__remove_persisted_session()
            return False

        # 登录成功后持久化参数，下次优先复用
        self.__save_persisted_session()
        logger.debug(f"{self._username} 成功登录绿联影视")
        self.get_librarys()
        return True

    def disconnect(self, logout: bool = False):
        if self._api:
            if logout:
                # 显式登出时同步清理本地缓存
                self._api.logout()
                self.__remove_persisted_session()
            self._api.close()
            self._api = None
            self._userinfo = None
            logger.debug(f"{self._username} 已断开绿联影视")

    @staticmethod
    def __normalize_dir_path(path: Union[str, Path, None]) -> str:
        if path is None:
            return ""
        value = str(path).replace("\\", "/").rstrip("/")
        return value

    @staticmethod
    def __is_subpath(path: Union[str, Path, None], parent: Union[str, Path, None]) -> bool:
        path_str = Ugreen.__normalize_dir_path(path)
        parent_str = Ugreen.__normalize_dir_path(parent)
        if not path_str or not parent_str:
            return False
        return path_str == parent_str or path_str.startswith(parent_str + "/")

    def __build_image_stream_url(self, source_url: str, size: int = 1) -> Optional[str]:
        """
        通过绿联 getImaStream 中转图片，规避 scraper.ugnas.com 403 问题。
        """
        if not self._api:
            return None

        auth_token = self._api.static_token or self._api.token
        if not auth_token:
            return None

        params = {
            "app_name": "web",
            "name": source_url,
            "size": size,
        }
        if self._api.is_ugk:
            params["ugk"] = auth_token
        else:
            params["token"] = auth_token

        return UrlUtils.combine_url(
            host=self._api.host,
            path="/ugreen/v2/video/getImaStream",
            query=params,
        )

    def __resolve_image(self, path: Optional[str]) -> Optional[str]:
        if not path:
            return None
        if path.startswith("http://") or path.startswith("https://"):
            parsed = urlparse(path)
            if parsed.netloc.lower() == "scraper.ugnas.com":
                # scraper 链接优先改为本机 getImaStream，避免签名过期导致 403
                if image_stream_url := self.__build_image_stream_url(path):
                    return image_stream_url

            # 绿联返回的 scraper.ugnas.com 图片常带 auth_key 时效签名，
            # 过期后会直接 403。这里提前过滤，避免前端出现裂图。
            if self.__is_expired_signed_image(path):
                return None
            return path
        # 绿联本地图片路径需要额外鉴权头，MP图片代理当前仅支持Cookie，故先忽略本地路径。
        return None

    @staticmethod
    def __is_expired_signed_image(url: str) -> bool:
        """
        判断绿联 scraper 签名图是否已过期。

        auth_key 结构通常为：
        `{过期时间戳}-{随机串}-...`
        """
        try:
            parsed = urlparse(url)
            if parsed.netloc.lower() != "scraper.ugnas.com":
                return False
            auth_key = parse_qs(parsed.query).get("auth_key", [None])[0]
            if not auth_key:
                return False
            expire_part = str(auth_key).split("-", 1)[0]
            expire_ts = int(expire_part)
            now_ts = int(datetime.now().timestamp())
            return expire_ts <= now_ts
        except Exception:
            return False

    @staticmethod
    def __parse_year(video_info: dict) -> Optional[Union[str, int]]:
        year = video_info.get("year")
        if isinstance(year, int) and year > 0:
            return year
        release_date = video_info.get("release_date")
        if isinstance(release_date, (int, float)) and release_date > 0:
            try:
                return datetime.fromtimestamp(release_date).year
            except Exception:
                return None
        return None

    @staticmethod
    def __map_item_type(video_type: Any) -> Optional[str]:
        if video_type == 2:
            return "Series"
        if video_type == 1:
            return "Movie"
        if video_type == 3:
            return "Collection"
        if video_type == 0:
            return "Folder"
        return "Video"

    @staticmethod
    def __build_media_server_item(video_info: dict, play_status: Optional[dict] = None):
        user_state = schemas.MediaServerItemUserState()
        if isinstance(play_status, dict):
            progress = play_status.get("progress")
            watch_status = play_status.get("watch_status")
            if watch_status == 2:
                user_state.played = True
            if isinstance(progress, (int, float)) and progress > 0:
                user_state.resume = progress < 1
                user_state.percentage = progress * 100.0
            last_play_time = play_status.get("last_access_time") or play_status.get("LastPlayTime")
            if isinstance(last_play_time, (int, float)) and last_play_time > 0:
                user_state.last_played_date = str(int(last_play_time))

        tmdb_id = video_info.get("tmdb_id")
        if not isinstance(tmdb_id, int) or tmdb_id <= 0:
            tmdb_id = None

        item_id = video_info.get("ug_video_info_id")
        if item_id is None:
            return None

        return schemas.MediaServerItem(
            server="ugreen",
            library=video_info.get("media_lib_set_id"),
            item_id=str(item_id),
            item_type=Ugreen.__map_item_type(video_info.get("type")),
            title=video_info.get("name"),
            original_title=video_info.get("original_name"),
            year=Ugreen.__parse_year(video_info),
            tmdbid=tmdb_id,
            user_state=user_state,
        )

    def __build_root_url(self) -> str:
        """
        统一返回 NAS Web 根地址作为跳转链接，避免失效深链。
        """
        host = self._playhost or (self._api.host if self._api else "")
        if not host:
            return ""
        return f"{host.rstrip('/')}/"

    def __build_play_url(self, item_id: Union[str, int], video_type: Any, media_lib_set_id: Any) -> str:
        # 绿联深链在部分版本会失效，统一回落到 NAS 根地址。
        return self.__build_root_url()

    def __build_play_item_from_wrapper(self, wrapper: dict) -> Optional[schemas.MediaServerPlayItem]:
        video_info = wrapper.get("video_info") if isinstance(wrapper.get("video_info"), dict) else wrapper
        if not isinstance(video_info, dict):
            return None

        item_id = video_info.get("ug_video_info_id")
        if item_id is None:
            return None

        play_status = wrapper.get("play_status") if isinstance(wrapper.get("play_status"), dict) else {}
        progress = play_status.get("progress") if isinstance(play_status.get("progress"), (int, float)) else 0

        if video_info.get("type") == 2:
            subtitle = play_status.get("tv_name") or "剧集"
            media_type = MediaType.TV.value
        else:
            subtitle = "电影" if video_info.get("type") == 1 else "视频"
            media_type = MediaType.MOVIE.value

        image = self.__resolve_image(video_info.get("poster_path")) or self.__resolve_image(
            video_info.get("backdrop_path")
        )

        return schemas.MediaServerPlayItem(
            id=str(item_id),
            title=video_info.get("name"),
            subtitle=subtitle,
            type=media_type,
            image=image,
            link=self.__build_play_url(item_id, video_info.get("type"), video_info.get("media_lib_set_id")),
            percent=max(0.0, min(100.0, progress * 100.0)),
            server_type="ugreen",
            use_cookies=False,
        )

    @staticmethod
    def __infer_library_type(name: str, path: Optional[str]) -> str:
        name = name or ""
        path = path or ""
        if "电视剧" in path or any(key in name for key in ["剧", "综艺", "动漫", "纪录片"]):
            return MediaType.TV.value
        if "电影" in path or "电影" in name:
            return MediaType.MOVIE.value
        return MediaType.UNKNOWN.value

    def __is_library_blocked(self, library_id: str) -> bool:
        return (
            True
            if (
                self._sync_libraries
                and "all" not in self._sync_libraries
                and str(library_id) not in self._sync_libraries
            )
            else False
        )

    @staticmethod
    def __resolve_scan_type(
        scan_mode: Optional[Union[str, int]] = None,
        scan_type: Optional[Union[str, int]] = None,
    ) -> int:
        """
        解析绿联扫描模式并转为 `media_lib_scan_type`。

        支持值：
        - 1 / new_and_modified: 新添加和修改
        - 2 / supplement_missing: 补充缺失
        - 3 / full_override: 覆盖扫描
        """
        # 优先使用显式 scan_type 数值配置。
        for value in (scan_type, scan_mode):
            try:
                parsed = int(value)  # type: ignore[arg-type]
                if parsed in (1, 2, 3):
                    return parsed
            except Exception:
                pass

        mode = str(scan_mode or "").strip().lower()
        mode_map = {
            "new_and_modified": 1,
            "new_modified": 1,
            "add": 1,
            "added": 1,
            "new": 1,
            "scan_new_modified": 1,
            "supplement_missing": 2,
            "supplement": 2,
            "additional": 2,
            "missing": 2,
            "scan_missing": 2,
            "full_override": 3,
            "override": 3,
            "cover": 3,
            "replace": 3,
            "scan_override": 3,
        }
        return mode_map.get(mode, 2)

    @staticmethod
    def __resolve_verify_ssl(verify_ssl: Optional[Union[bool, str, int]]) -> bool:
        if isinstance(verify_ssl, bool):
            return verify_ssl
        if verify_ssl is None:
            return True
        value = str(verify_ssl).strip().lower()
        if value in {"1", "true", "yes", "on"}:
            return True
        if value in {"0", "false", "no", "off"}:
            return False
        return True

    def __scan_library(self, library_id: str, scan_type: Optional[int] = None) -> bool:
        if not self._api:
            return False
        return self._api.scan(
            media_lib_set_id=library_id,
            scan_type=scan_type or self._scan_type,
            op_type=2,
        )

    def __load_library_paths(self) -> dict[str, str]:
        if not self._api:
            return {}

        paths: dict[str, str] = {}
        page = 1
        while True:
            data = self._api.poster_wall_get_folder(page=page, page_size=100)
            if not data:
                break

            for folder in data.get("folder_arr") or []:
                lib_id = folder.get("media_lib_set_id")
                lib_path = folder.get("path")
                if lib_id is not None and lib_path:
                    paths[str(lib_id)] = str(lib_path)

            if data.get("is_last_page"):
                break
            page += 1

        return paths

    def get_librarys(self, hidden: Optional[bool] = False) -> List[schemas.MediaServerLibrary]:
        if not self.is_authenticated() or not self._api:
            return []

        media_libs = self._api.media_list()
        self._library_paths = self.__load_library_paths()
        libraries = []
        self._libraries = {}

        for lib in media_libs:
            lib_id = str(lib.get("media_lib_set_id"))
            if hidden and self.__is_library_blocked(lib_id):
                continue

            lib_name = lib.get("media_name") or ""
            lib_path = self._library_paths.get(lib_id)
            library_type = self.__infer_library_type(lib_name, lib_path)

            poster_paths = lib.get("poster_paths") or []
            backdrop_paths = lib.get("backdrop_paths") or []
            image_list = list(
                filter(
                    None,
                    [self.__resolve_image(p) for p in [*poster_paths, *backdrop_paths]],
                )
            )

            self._libraries[lib_id] = {
                "id": lib_id,
                "name": lib_name,
                "path": lib_path,
                "type": library_type,
                "video_count": lib.get("video_count") or 0,
            }

            libraries.append(
                schemas.MediaServerLibrary(
                    server="ugreen",
                    id=lib_id,
                    name=lib_name,
                    type=library_type,
                    path=lib_path,
                    image_list=image_list,
                    link=self.__build_root_url(),
                    server_type="ugreen",
                    use_cookies=False,
                )
            )

        return libraries

    def get_user_count(self) -> int:
        if not self.is_authenticated() or not self._api:
            return 0
        users = self._api.media_lib_users()
        return len(users)

    def get_medias_count(self) -> schemas.Statistic:
        if not self.is_authenticated() or not self._api:
            return schemas.Statistic()

        movie_data = self._api.video_all(classification=-102, page=1, page_size=1) or {}
        tv_data = self._api.video_all(classification=-103, page=1, page_size=1) or {}

        return schemas.Statistic(
            movie_count=int(movie_data.get("total_num") or 0),
            tv_count=int(tv_data.get("total_num") or 0),
            # 绿联当前不统计剧集总数，返回 None 由前端展示“未获取”。
            episode_count=None,
        )

    def authenticate(self, username: str, password: str) -> Optional[str]:
        if not username or not password or not self._host:
            return None

        api = Api(self._host, verify_ssl=self._verify_ssl)
        try:
            return api.login(username, password)
        finally:
            api.logout()
            api.close()

    @staticmethod
    def __extract_video_info_list(bucket: Any) -> list[dict]:
        if not isinstance(bucket, Mapping):
            return []
        video_arr = bucket.get("video_arr")
        if not isinstance(video_arr, list):
            return []
        result = []
        for item in video_arr:
            if not isinstance(item, Mapping):
                continue
            info = item.get("video_info")
            if isinstance(info, Mapping):
                result.append(dict(info))
        return result

    def get_movies(
        self, title: str, year: Optional[str] = None, tmdb_id: Optional[int] = None
    ) -> Optional[List[schemas.MediaServerItem]]:
        if not self.is_authenticated() or not self._api or not title:
            return None

        data = self._api.search(title)
        if not data:
            return []

        movies = []
        for info in self.__extract_video_info_list(data.get("movies_list")):
            info_tmdb = info.get("tmdb_id")
            if tmdb_id and tmdb_id != info_tmdb:
                continue
            if title not in [info.get("name"), info.get("original_name")]:
                continue
            item_year = info.get("year")
            if year and str(item_year) != str(year):
                continue
            media_item = self.__build_media_server_item(info)
            if media_item:
                movies.append(media_item)
        return movies

    def __search_tv_item(self, title: str, year: Optional[str] = None, tmdb_id: Optional[int] = None) -> Optional[dict]:
        if not self._api:
            return None
        data = self._api.search(title)
        if not data:
            return None

        for info in self.__extract_video_info_list(data.get("tv_list")):
            if tmdb_id and tmdb_id != info.get("tmdb_id"):
                continue
            if title not in [info.get("name"), info.get("original_name")]:
                continue
            item_year = info.get("year")
            if year and str(item_year) != str(year):
                continue
            return info
        return None

    def get_tv_episodes(
        self,
        item_id: Optional[str] = None,
        title: Optional[str] = None,
        year: Optional[str] = None,
        tmdb_id: Optional[int] = None,
        season: Optional[int] = None,
    ) -> tuple[Optional[str], Optional[Dict[int, list]]]:
        """
        根据标题、年份、TMDB ID和季号查询绿联媒体库中的电视剧已入库集数。
        :param item_id: 绿联媒体库中的剧集ID，存在缓存ID时优先使用
        :param title: 标题
        :param year: 年份
        :param tmdb_id: TMDB ID
        :param season: 季号
        :return: 命中的剧集ID及每季已入库集数
        """
        if not self.is_authenticated() or not self._api:
            return None, None

        cached_item_id = item_id
        if not item_id:
            if not title:
                return None, None
            if not (tv_info := self.__search_tv_item(title, year, tmdb_id)):
                return None, None
            found_item_id = tv_info.get("ug_video_info_id")
            if found_item_id is None:
                return None, None
            item_id = str(found_item_id)
        else:
            item_id = str(item_id)

        item_info = self.get_iteminfo(item_id)
        if not item_info and cached_item_id and title:
            # 媒体删除后重新入库会导致缓存ID失效，回退到标题搜索避免误判整部剧缺失。
            logger.warning(f"绿联缓存的电视剧媒体ID {cached_item_id} 已失效，尝试按标题重新搜索：{title}")
            if not (tv_info := self.__search_tv_item(title, year, tmdb_id)):
                return None, {}
            found_item_id = tv_info.get("ug_video_info_id")
            if found_item_id is None:
                return None, {}
            item_id = str(found_item_id)
            item_info = self.get_iteminfo(item_id)
        if not item_info:
            return None, {}
        if tmdb_id and item_info.tmdbid and tmdb_id != item_info.tmdbid:
            return None, {}

        tv_detail = self._api.get_tv(item_id, folder_path="ALL")
        if not tv_detail:
            return None, {}

        season_map = {}
        for info in tv_detail.get("season_info") or []:
            if not isinstance(info, dict):
                continue
            category_id = info.get("category_id")
            season_num = info.get("season_num")
            if category_id and isinstance(season_num, int):
                season_map[str(category_id)] = season_num

        season_episodes: Dict[int, list] = {}
        for ep in tv_detail.get("tv_info") or []:
            if not isinstance(ep, dict):
                continue
            episode = ep.get("episode")
            if not isinstance(episode, int):
                continue
            season_num = season_map.get(str(ep.get("category_id")), 1)
            if season is not None and season_num != season:
                continue
            season_episodes.setdefault(season_num, []).append(episode)

        for season_num in list(season_episodes.keys()):
            season_episodes[season_num] = sorted(set(season_episodes[season_num]))

        return item_id, season_episodes

    def refresh_root_library(self, scan_mode: Optional[Union[str, int]] = None) -> Optional[bool]:
        if not self.is_authenticated() or not self._api:
            return None

        if not self._libraries:
            self.get_librarys()

        scan_type = (
            self.__resolve_scan_type(scan_mode=scan_mode)
            if scan_mode is not None
            else self._scan_type
        )
        results = []
        for lib_id in self._libraries.keys():
            logger.info(
                f"刷新媒体库：{self._libraries[lib_id].get('name')}（扫描模式: {scan_type}）"
            )
            results.append(self.__scan_library(library_id=lib_id, scan_type=scan_type))

        return all(results) if results else True

    def __match_library_id_by_path(self, path: Optional[Path]) -> Optional[str]:
        if path is None:
            return None

        path_str = self.__normalize_dir_path(path)
        if not self._library_paths:
            self.get_librarys()

        for lib_id, lib_path in self._library_paths.items():
            if self.__is_subpath(path_str, lib_path):
                return lib_id
        return None

    def refresh_library_by_items(
        self,
        items: List[schemas.RefreshMediaItem],
        scan_mode: Optional[Union[str, int]] = None,
    ) -> Optional[bool]:
        if not self.is_authenticated() or not self._api:
            return None

        scan_type = (
            self.__resolve_scan_type(scan_mode=scan_mode)
            if scan_mode is not None
            else self._scan_type
        )
        library_ids = set()
        for item in items:
            library_id = self.__match_library_id_by_path(item.target_path)
            if library_id is None:
                return self.refresh_root_library(scan_mode=scan_mode)
            library_ids.add(library_id)

        for library_id in library_ids:
            lib_name = self._libraries.get(library_id, {}).get("name", library_id)
            logger.info(f"刷新媒体库：{lib_name}（扫描模式: {scan_type}）")
            if not self.__scan_library(library_id=library_id, scan_type=scan_type):
                return self.refresh_root_library(scan_mode=scan_mode)

        return True

    @staticmethod
    def get_webhook_message(body: Any) -> Optional[schemas.WebhookEventInfo]:
        return None

    def get_iteminfo(self, itemid: str) -> Optional[schemas.MediaServerItem]:
        if not self.is_authenticated() or not self._api or not itemid:
            return None

        info = self._api.recently_played_info(itemid)
        if not info:
            return None

        video_info = info.get("video_info") if isinstance(info.get("video_info"), dict) else None
        if not video_info or not video_info.get("ug_video_info_id"):
            return None

        return self.__build_media_server_item(video_info, info.get("play_status"))

    def _iter_library_videos(self, root_path: str, page_size: int = 100):
        if not self._api or not root_path:
            return

        queue = deque([root_path])
        visited: set[str] = set()
        max_paths = 20000

        while queue and len(visited) < max_paths:
            current_path = queue.popleft()
            if current_path in visited:
                continue
            visited.add(current_path)

            page = 1
            while True:
                data = self._api.poster_wall_get_folder(
                    path=current_path,
                    page=page,
                    page_size=page_size,
                    sort_type=1,
                    order_type=1,
                )
                if not data:
                    break

                for video in data.get("video_arr") or []:
                    if isinstance(video, dict):
                        yield video

                for folder in data.get("folder_arr") or []:
                    if not isinstance(folder, dict):
                        continue
                    sub_path = folder.get("path")
                    if sub_path and sub_path not in visited:
                        queue.append(str(sub_path))

                if data.get("is_last_page"):
                    break
                page += 1

    def get_items(
        self,
        parent: Union[str, int],
        start_index: Optional[int] = 0,
        limit: Optional[int] = -1,
    ) -> Generator[schemas.MediaServerItem | None | Any, Any, None]:
        if not self.is_authenticated() or not self._api:
            return None

        library_id = str(parent)
        if not self._library_paths:
            self.get_librarys()

        root_path = self._library_paths.get(library_id)
        if not root_path:
            return None

        skip = max(0, start_index or 0)
        remain = -1 if limit in [None, -1] else max(0, limit)

        for video in self._iter_library_videos(root_path=root_path):
            video_type = video.get("type")
            if video_type not in [1, 2]:
                continue

            if skip > 0:
                skip -= 1
                continue

            item = self.__build_media_server_item(video)
            if item:
                yield item
                if remain != -1:
                    remain -= 1
                    if remain <= 0:
                        break

        return None

    def get_play_url(self, item_id: str) -> Optional[str]:
        if not self.is_authenticated() or not self._api:
            return None

        info = self._api.recently_played_info(item_id)
        if not info:
            return None

        video_info = info.get("video_info") if isinstance(info.get("video_info"), dict) else None
        if not video_info:
            return None

        return self.__build_play_url(
            item_id=item_id,
            video_type=video_info.get("type"),
            media_lib_set_id=video_info.get("media_lib_set_id"),
        )

    def get_resume(self, num: Optional[int] = 12) -> Optional[List[schemas.MediaServerPlayItem]]:
        if not self.is_authenticated() or not self._api:
            return None

        page_size = max(1, num or 12)
        data = self._api.recently_played(page=1, page_size=page_size)
        if not data:
            return []

        ret_resume = []
        for item in data.get("video_arr") or []:
            if len(ret_resume) == page_size:
                break
            if not isinstance(item, dict):
                continue
            video_info = item.get("video_info") if isinstance(item.get("video_info"), dict) else {}
            library_id = str(video_info.get("media_lib_set_id") or "")
            if self.__is_library_blocked(library_id):
                continue
            play_item = self.__build_play_item_from_wrapper(item)
            if play_item:
                ret_resume.append(play_item)

        return ret_resume

    def get_latest(self, num: int = 20) -> Optional[List[schemas.MediaServerPlayItem]]:
        if not self.is_authenticated() or not self._api:
            return None

        page_size = max(1, num)
        data = self._api.recently_updated(page=1, page_size=page_size)
        if not data:
            return []

        latest = []
        for item in data.get("video_arr") or []:
            if len(latest) == page_size:
                break
            if not isinstance(item, dict):
                continue
            video_info = item.get("video_info") if isinstance(item.get("video_info"), dict) else {}
            library_id = str(video_info.get("media_lib_set_id") or "")
            if self.__is_library_blocked(library_id):
                continue
            play_item = self.__build_play_item_from_wrapper(item)
            if play_item:
                latest.append(play_item)

        return latest

    def get_latest_backdrops(self, num: int = 20, remote: bool = False) -> Optional[List[str]]:
        if not self.is_authenticated() or not self._api:
            return None

        data = self._api.recently_updated(page=1, page_size=max(1, num))
        if not data:
            return []

        images: List[str] = []
        for item in data.get("video_arr") or []:
            if len(images) == num:
                break
            if not isinstance(item, dict):
                continue

            video_info = item.get("video_info") if isinstance(item.get("video_info"), dict) else {}
            library_id = str(video_info.get("media_lib_set_id") or "")
            if self.__is_library_blocked(library_id):
                continue

            image = self.__resolve_image(video_info.get("backdrop_path")) or self.__resolve_image(
                video_info.get("poster_path")
            )
            if image:
                images.append(image)

        return images

    @staticmethod
    def get_image_cookies(image_url: str):
        # 绿联图片流接口依赖加密鉴权头，当前图片代理仅支持Cookie注入。
        return None
