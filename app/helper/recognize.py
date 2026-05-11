import json
from typing import Optional

from app.core.config import settings
from app.core.context import MediaInfo
from app.core.meta import MetaBase
from app.log import logger
from app.schemas.types import MediaType, media_type_to_agent
from app.utils.http import RequestUtils, AsyncRequestUtils
from app.utils.singleton import WeakSingleton


class MediaRecognizeShareHelper(metaclass=WeakSingleton):
    """
    共享媒体识别帮助类
    """

    _default_path = "/recognize/share"

    @classmethod
    def _normalize_media_type(cls, media_type: Optional[object]) -> Optional[str]:
        """
        统一媒体类型，兼容枚举、中文值和 agent 风格字符串
        """
        normalized = media_type_to_agent(media_type)
        if normalized in {"movie", "tv"}:
            return normalized
        if isinstance(media_type, str):
            if media_type == MediaType.MOVIE.value:
                return "movie"
            if media_type == MediaType.TV.value:
                return "tv"
        return None

    @staticmethod
    def _extract_keyword(meta: Optional[MetaBase]) -> Optional[str]:
        """
        提取识别关键字
        """
        if not meta:
            return None
        keyword = meta.original_name or meta.name
        if keyword:
            keyword = str(keyword).strip()
        return keyword or None

    @classmethod
    def _extract_media_type(
            cls,
            meta: Optional[MetaBase] = None,
            mtype: Optional[MediaType] = None,
            mediainfo: Optional[MediaInfo] = None,
    ) -> Optional[str]:
        """
        提取媒体类型
        """
        media_type = cls._normalize_media_type(mtype)
        if media_type:
            return media_type
        if mediainfo and mediainfo.type in {MediaType.MOVIE, MediaType.TV}:
            return mediainfo.type.to_agent()
        if meta and meta.type in {MediaType.MOVIE, MediaType.TV}:
            return meta.type.to_agent()
        if meta and (meta.begin_season is not None or meta.begin_episode is not None):
            return "tv"
        return None

    @classmethod
    def _extract_season(
            cls,
            media_type: Optional[str],
            meta: Optional[MetaBase] = None,
            mediainfo: Optional[MediaInfo] = None,
    ) -> Optional[int]:
        """
        提取季信息，仅电视剧使用
        """
        if media_type != "tv":
            return None
        season = meta.begin_season if meta else None
        if season is None and mediainfo:
            season = mediainfo.season
        try:
            return int(season) if season is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _extract_year(
            meta: Optional[MetaBase] = None,
            mediainfo: Optional[MediaInfo] = None,
    ) -> Optional[str]:
        """
        提取年份
        """
        year = (meta.year if meta else None) or (mediainfo.year if mediainfo else None)
        if year is None:
            return None
        year_text = str(year).strip()
        return year_text or None

    @classmethod
    def _build_api_url(cls) -> Optional[str]:
        """
        获取共享识别API地址
        """
        custom_api = (settings.MEDIA_RECOGNIZE_SHARE_API or "").strip()
        if custom_api:
            return custom_api.rstrip("/")
        server_host = (settings.MP_SERVER_HOST or "").strip().rstrip("/")
        if not server_host:
            return None
        return f"{server_host}{cls._default_path}"

    @classmethod
    def _build_query_params(
            cls,
            meta: Optional[MetaBase],
            mtype: Optional[MediaType] = None,
            keyword_meta: Optional[MetaBase] = None,
    ) -> Optional[dict]:
        """
        组装共享识别查询参数
        """
        keyword = cls._extract_keyword(keyword_meta or meta)
        if not keyword:
            return None

        media_type = cls._extract_media_type(meta=meta, mtype=mtype)
        params = {
            "keyword": keyword,
        }
        if media_type:
            params["type"] = media_type
        if year := cls._extract_year(meta=meta):
            params["year"] = year
        if season := cls._extract_season(media_type=media_type, meta=meta):
            params["season"] = season
        return params

    @classmethod
    def _build_report_payload(
            cls,
            meta: Optional[MetaBase],
            mediainfo: Optional[MediaInfo],
            keyword_meta: Optional[MetaBase] = None,
    ) -> Optional[dict]:
        """
        组装共享识别上报载荷
        """
        if not meta or not mediainfo:
            return None

        keyword = cls._extract_keyword(keyword_meta or meta)
        media_type = cls._extract_media_type(meta=meta, mediainfo=mediainfo)
        if not keyword or not media_type:
            return None
        if not any([mediainfo.tmdb_id, mediainfo.douban_id, mediainfo.bangumi_id]):
            return None

        return {
            "keyword": keyword,
            "type": media_type,
            "title": mediainfo.title or keyword,
            "year": cls._extract_year(meta=meta, mediainfo=mediainfo),
            "season": cls._extract_season(
                media_type=media_type,
                meta=meta,
                mediainfo=mediainfo,
            ),
            "tmdbid": mediainfo.tmdb_id,
            "doubanid": mediainfo.douban_id,
            "bangumiid": mediainfo.bangumi_id,
        }

    @staticmethod
    def _parse_response_item(data: Optional[dict]) -> Optional[dict]:
        """
        解析服务端返回的共享识别数据
        """
        if not isinstance(data, dict):
            return None
        item = (data.get("data") or {}).get("item")
        if not isinstance(item, dict):
            return None
        return item

    @staticmethod
    def _response_message(response) -> str:
        """
        获取响应消息，兼容非JSON响应
        """
        try:
            payload = response.json()
            return str(payload.get("message") or "")
        except (json.JSONDecodeError, ValueError, AttributeError):
            return ""

    @staticmethod
    def _is_enabled() -> bool:
        """
        是否启用共享识别
        """
        return bool(settings.MEDIA_RECOGNIZE_SHARE)

    def query(
            self,
            meta: Optional[MetaBase],
            mtype: Optional[MediaType] = None,
            keyword_meta: Optional[MetaBase] = None,
    ) -> Optional[dict]:
        """
        查询共享识别结果
        """
        if not self._is_enabled():
            return None

        api_url = self._build_api_url()
        params = self._build_query_params(
            meta=meta,
            mtype=mtype,
            keyword_meta=keyword_meta,
        )
        if not api_url or not params:
            return None

        response = RequestUtils(proxies=settings.PROXY or {}, timeout=5).get_res(
            api_url,
            params=params,
        )
        if not response or response.status_code != 200:
            if response is not None:
                logger.warn(
                    f"查询共享媒体识别失败：status={response.status_code} "
                    f"message={self._response_message(response)}"
                )
            return None

        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as err:
            logger.warn(f"解析共享媒体识别响应失败：{err}")
            return None

        if payload.get("code") != 0:
            return None

        item = self._parse_response_item(payload)
        if item:
            logger.info(f"共享媒体识别命中：{params.get('keyword')} - {item}")
        return item

    async def async_query(
            self,
            meta: Optional[MetaBase],
            mtype: Optional[MediaType] = None,
            keyword_meta: Optional[MetaBase] = None,
    ) -> Optional[dict]:
        """
        异步查询共享识别结果
        """
        if not self._is_enabled():
            return None

        api_url = self._build_api_url()
        params = self._build_query_params(
            meta=meta,
            mtype=mtype,
            keyword_meta=keyword_meta,
        )
        if not api_url or not params:
            return None

        response = await AsyncRequestUtils(
            proxies=settings.PROXY or {},
            timeout=5,
        ).get_res(api_url, params=params)
        if not response or response.status_code != 200:
            if response is not None:
                logger.warn(
                    f"异步查询共享媒体识别失败：status={response.status_code} "
                    f"message={self._response_message(response)}"
                )
            return None

        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as err:
            logger.warn(f"解析共享媒体识别响应失败：{err}")
            return None

        if payload.get("code") != 0:
            return None

        item = self._parse_response_item(payload)
        if item:
            logger.info(f"共享媒体识别命中：{params.get('keyword')} - {item}")
        return item

    def report(
            self,
            meta: Optional[MetaBase],
            mediainfo: Optional[MediaInfo],
            keyword_meta: Optional[MetaBase] = None,
    ) -> bool:
        """
        上报共享识别结果
        """
        if not self._is_enabled():
            return False

        api_url = self._build_api_url()
        payload = self._build_report_payload(
            meta=meta,
            mediainfo=mediainfo,
            keyword_meta=keyword_meta,
        )
        if not api_url or not payload:
            return False

        response = RequestUtils(
            proxies=settings.PROXY or {},
            timeout=5,
            content_type="application/json",
        ).post_res(api_url, json=payload)
        if not response or response.status_code != 200:
            if response is not None:
                logger.warn(
                    f"上报共享媒体识别失败：status={response.status_code} "
                    f"message={self._response_message(response)}"
                )
            return False

        try:
            result = response.json()
        except (json.JSONDecodeError, ValueError) as err:
            logger.warn(f"解析共享媒体识别上报响应失败：{err}")
            return False

        return result.get("code") == 0

    async def async_report(
            self,
            meta: Optional[MetaBase],
            mediainfo: Optional[MediaInfo],
            keyword_meta: Optional[MetaBase] = None,
    ) -> bool:
        """
        异步上报共享识别结果
        """
        if not self._is_enabled():
            return False

        api_url = self._build_api_url()
        payload = self._build_report_payload(
            meta=meta,
            mediainfo=mediainfo,
            keyword_meta=keyword_meta,
        )
        if not api_url or not payload:
            return False

        response = await AsyncRequestUtils(
            proxies=settings.PROXY or {},
            timeout=5,
            content_type="application/json",
        ).post_res(api_url, json=payload)
        if not response or response.status_code != 200:
            if response is not None:
                logger.warn(
                    f"异步上报共享媒体识别失败：status={response.status_code} "
                    f"message={self._response_message(response)}"
                )
            return False

        try:
            result = response.json()
        except (json.JSONDecodeError, ValueError) as err:
            logger.warn(f"解析共享媒体识别上报响应失败：{err}")
            return False

        return result.get("code") == 0

    @classmethod
    def to_recognize_params(cls, item: Optional[dict]) -> Optional[dict]:
        """
        将服务端返回的共享识别结果转成本地识别参数
        """
        if not isinstance(item, dict):
            return None

        media_type = cls._normalize_media_type(item.get("type"))
        mtype = MediaType.from_agent(media_type) if media_type else None
        tmdbid = item.get("tmdbid")
        doubanid = item.get("doubanid")
        bangumiid = item.get("bangumiid")
        if not any([tmdbid, doubanid, bangumiid]):
            return None

        return {
            "mtype": mtype,
            "tmdbid": tmdbid,
            "doubanid": doubanid,
            "bangumiid": bangumiid,
            "season": item.get("season"),
        }
