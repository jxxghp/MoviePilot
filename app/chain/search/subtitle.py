"""字幕搜索、匹配与结果投影 owner。"""

import asyncio
import random
from typing import Any, AsyncIterator, Dict, List, Optional, cast

from app.application.torrent.download import TorrentHelper
from app.chain.media import MediaChain
from app.chain.search.contract import _SearchOwnerBase as _SearchOwnerBase
from app.domain.context import MediaInfo, SubtitleInfo, TorrentInfo
from app.domain.meta.metabase import MetaBase
from app.domain.metainfo import MetaInfo
from app.runtime.execution import run_in_threadpool
from app.runtime.log import logger
from app.runtime.stop import runtime_stop_state
from app.schemas.media import build_media_key, resolve_media_identity
from app.schemas.mediaserver import NotExistMediaInfo
from app.schemas.types import (
    MediaSource,
    MediaType,
)


class SearchSubtitleOwner(_SearchOwnerBase):
    """字幕搜索、匹配与结果投影 owner。"""

    async def async_search_subtitles_by_title(
        self,
        title: str,
        page: Optional[int] = 0,
        sites: Optional[List[int]] = None,
        cache_local: Optional[bool] = False,
    ) -> List[SubtitleInfo]:
        """
        根据标题异步搜索字幕，不识别不过滤，直接返回站点字幕内容。
        :param title: 标题关键词
        :param page: 页码
        :param sites: 站点ID列表
        :param cache_local: 是否缓存到本地
        """
        if cache_local:
            self.cancel_ai_recommend()
            await self.async_save_last_search_params(
                keyword=title,
                area="title",
                sites=sites,
                result_type="subtitle",
            )
        logger.info(f"开始搜索字幕，关键词：{title} ...")
        subtitles = await self._async_search_subtitles_all_sites(keyword=title, sites=sites, page=page) or []
        if not subtitles:
            logger.warning(f"{title} 未搜索到字幕")
            return []
        if cache_local:
            await self._async_save_subtitles(subtitles)
        return subtitles

    async def async_search_subtitles_by_title_stream(
        self,
        title: str,
        page: Optional[int] = 0,
        sites: Optional[List[int]] = None,
        cache_local: Optional[bool] = False,
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        根据标题渐进式搜索字幕，不识别不过滤，按站点完成顺序返回结果。
        """
        if cache_local:
            self.cancel_ai_recommend()
            await self.async_save_last_search_params(
                keyword=title,
                area="title",
                sites=sites,
                result_type="subtitle",
            )
        logger.info(f"开始渐进式搜索字幕，关键词：{title} ...")

        subtitles: List[SubtitleInfo] = []
        async for event in self._async_search_subtitles_all_sites_stream(keyword=title, sites=sites, page=page):
            result = event.pop("items", []) or []
            if result:
                subtitles.extend(result)
            yield {
                **event,
                "type": "append",
                "items": [subtitle.to_dict() for subtitle in result],
                "total_items": len(subtitles),
            }

        if cache_local:
            await self._async_save_subtitles(subtitles)

        if not subtitles:
            logger.warning(f"{title} 未搜索到字幕")
        yield {
            "type": "done",
            "stage": "done",
            "text": f"搜索完成，共 {len(subtitles)} 个字幕",
            "items": [cast(Any, subtitle).to_dict() for subtitle in subtitles],
            "total_items": len(subtitles),
        }

    async def async_search_subtitles_by_id(
        self,
        media_source: MediaSource,
        media_id: str,
        mtype: Optional[MediaType] = None,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        sites: Optional[List[int]] = None,
        cache_local: bool = False,
    ) -> List[SubtitleInfo]:
        """
        根据数据源媒体 ID 异步精确搜索字幕，不应用过滤规则。
        :param media_source: 媒体数据源
        :param media_id: 数据源原生 ID
        :param mtype: 媒体，电影 or 电视剧
        :param season: 季数
        :param episode: 集数
        :param sites: 站点ID列表
        :param cache_local: 是否缓存到本地
        """
        if cache_local:
            self.cancel_ai_recommend()
            await self.async_save_last_search_params(
                media_source=media_source,
                media_id=media_id,
                mtype=mtype,
                area="title",
                season=season,
                episode=episode,
                sites=sites,
                result_type="subtitle",
            )
        mediainfo = await MediaChain().async_recognize_media(
            media_source=media_source,
            media_id=media_id,
            mtype=mtype,
        )
        if not mediainfo:
            logger.error(f"{self._build_search_keyword(media_source, media_id)} 媒体信息识别失败！")
            return []
        subtitles = await self._async_search_subtitles_for_media(
            mediainfo=mediainfo,
            media_source=media_source,
            media_id=media_id,
            season=season,
            episode=episode,
            sites=sites,
        )
        if cache_local:
            await self._async_save_subtitles(subtitles)
        return subtitles

    async def async_search_subtitles_by_id_stream(
        self,
        media_source: MediaSource,
        media_id: str,
        mtype: Optional[MediaType] = None,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        sites: Optional[List[int]] = None,
        cache_local: bool = False,
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        根据数据源媒体 ID 渐进式精确搜索字幕，先返回站点候选，再返回标题和剧集匹配后的结果。
        """
        if cache_local:
            self.cancel_ai_recommend()
            await self.async_save_last_search_params(
                media_source=media_source,
                media_id=media_id,
                mtype=mtype,
                area="title",
                season=season,
                episode=episode,
                sites=sites,
                result_type="subtitle",
            )
        mediainfo = await MediaChain().async_recognize_media(
            media_source=media_source,
            media_id=media_id,
            mtype=mtype,
        )
        if not mediainfo:
            logger.error(f"{self._build_search_keyword(media_source, media_id)} 媒体信息识别失败！")
            yield {"type": "error", "success": False, "message": "媒体信息识别失败"}
            return

        subtitles: List[SubtitleInfo] = []
        async for event in self._async_search_subtitles_for_media_stream(
            mediainfo=mediainfo,
            media_source=media_source,
            media_id=media_id,
            season=season,
            episode=episode,
            sites=sites,
        ):
            if event.get("type") == "done":
                subtitles = event.get("subtitles") or []
                event = {key: value for key, value in event.items() if key != "subtitles"}
            yield event

        if cache_local:
            await self._async_save_subtitles(subtitles)

    @staticmethod
    def _build_subtitle_season_episodes(
        mediainfo: MediaInfo, season: Optional[int] = None, episode: Optional[int] = None
    ) -> Optional[Dict[int, List[int]]]:
        """
        构造字幕匹配用季集约束，未指定集数时只约束到同一季。
        """
        if mediainfo.type != MediaType.TV:
            return None
        media_season = season if season is not None else mediainfo.season
        if media_season is None:
            return None
        return {media_season: [episode] if episode is not None else []}

    @staticmethod
    def _build_subtitle_torrent(subtitle: SubtitleInfo, title: Optional[str] = None) -> TorrentInfo:
        """
        将字幕结果转换为轻量资源对象，复用既有标题匹配逻辑。
        """
        return TorrentInfo(
            site=subtitle.site,
            site_name=subtitle.site_name,
            site_cookie=subtitle.site_cookie,
            site_ua=subtitle.site_ua,
            site_proxy=subtitle.site_proxy,
            site_order=subtitle.site_order,
            title=title or subtitle.title or subtitle.file_name,
            description=subtitle.description,
            enclosure=subtitle.enclosure,
            page_url=subtitle.page_url,
            size=subtitle.size,
            grabs=subtitle.grabs,
            pubdate=subtitle.pubdate,
            date_elapsed=subtitle.date_elapsed,
        )

    @staticmethod
    def _build_subtitle_names(subtitle: SubtitleInfo) -> List[str]:
        """
        提取字幕标题、下载文件名和描述，作为精确匹配的名称候选。
        """
        return list(
            dict.fromkeys(
                name.strip()
                for name in (subtitle.title, subtitle.file_name, subtitle.description)
                if name and name.strip()
            )
        )

    @staticmethod
    def _build_subtitle_meta(
        title: str,
        subtitle: SubtitleInfo,
        custom_words: Optional[List[str]] = None,
    ) -> MetaBase:
        """
        识别字幕名称。
        """
        return MetaInfo(
            title=title,
            subtitle=subtitle.description,
            custom_words=custom_words or [],
        )

    @staticmethod
    def _match_subtitle_episode(
        meta: MetaBase,
        season_episodes: Optional[Dict[int, List[int]]],
        episode: Optional[int] = None,
    ) -> bool:
        """
        判断字幕识别出的季集是否落在目标媒体季集内。
        """
        if not season_episodes:
            return True
        subtitle_torrent = TorrentInfo(title=meta.org_string or "")
        if not TorrentHelper.match_season_episodes(
            torrent=subtitle_torrent, meta=meta, season_episodes=season_episodes
        ):
            return False
        if episode is not None:
            return bool(meta.episode_list) and episode in meta.episode_list
        return True

    def _parse_subtitle_result(
        self,
        subtitles: List[SubtitleInfo],
        mediainfo: MediaInfo,
        keyword: Optional[str] = None,
        season_episodes: Optional[Dict[int, List[int]]] = None,
        episode: Optional[int] = None,
        custom_words: Optional[List[str]] = None,
    ) -> List[SubtitleInfo]:
        """
        识别并精确匹配字幕搜索结果，不使用任何过滤规则。
        """
        if not subtitles:
            logger.warning(f"{keyword or mediainfo.title} 未搜索到字幕")
            return []

        match_subtitles = []
        logger.info(
            f"开始匹配字幕 标题：{mediainfo.title}，原标题：{mediainfo.original_title}，别名：{mediainfo.names}"
        )
        for subtitle in subtitles:
            if runtime_stop_state.is_system_stopped:
                break
            subtitle_names = self._build_subtitle_names(subtitle)
            if not subtitle_names:
                continue

            for subtitle_name in subtitle_names:
                subtitle_meta = self._build_subtitle_meta(
                    title=subtitle_name,
                    subtitle=subtitle,
                    custom_words=custom_words,
                )
                if not self._match_subtitle_episode(
                    meta=subtitle_meta, season_episodes=season_episodes, episode=episode
                ):
                    continue

                subtitle_torrent = self._build_subtitle_torrent(
                    subtitle=subtitle,
                    title=subtitle_name,
                )
                if TorrentHelper.match_torrent(
                    mediainfo=mediainfo, torrent_meta=subtitle_meta, torrent=subtitle_torrent
                ):
                    match_subtitles.append(subtitle)
                    break

        logger.info(f"字幕匹配完成，共匹配到 {len(match_subtitles)} 个字幕")
        return self._remove_duplicate_subtitles(match_subtitles)

    @staticmethod
    def _remove_duplicate_subtitles(subtitles: List[SubtitleInfo]) -> List[SubtitleInfo]:
        """
        去除重复的字幕结果。
        """
        return list(
            {
                f"{subtitle.site_name}_{subtitle.torrent_id}_{subtitle.subtitle_id}_{subtitle.title}_{subtitle.enclosure}": subtitle
                for subtitle in subtitles
            }.values()
        )

    async def _async_search_subtitles_for_media(
        self,
        mediainfo: MediaInfo,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        sites: Optional[List[int]] = None,
        custom_words: Optional[List[str]] = None,
    ) -> List[SubtitleInfo]:
        """
        根据媒体信息搜索并精确匹配字幕结果。
        """
        mediainfo = self._copy_media_input(mediainfo)
        if not mediainfo.tmdb_id:
            meta = MetaInfo(title=mediainfo.title)
            mediainfo.title = meta.name
            mediainfo.season = cast(int, meta.begin_season)
        logger.info(f"开始精确搜索字幕，关键词：{mediainfo.title} ...")

        if not mediainfo.names:
            recognized_media = await MediaChain().async_recognize_media(
                mtype=mediainfo.type,
                **self._media_recognize_kwargs(mediainfo),
            )
            if not recognized_media:
                logger.error("媒体信息识别失败！")
                return []
            mediainfo = recognized_media

        no_exists = None
        if season is not None:
            media_source, media_id = resolve_media_identity(
                media=mediainfo,
                media_source=media_source,
                media_id=media_id,
            )
            no_exists = {
                build_media_key(media_source, media_id): {
                    season: NotExistMediaInfo(episodes=[episode] if episode is not None else [])
                }
            }
        season_episodes, keywords = self._prepare_params(
            mediainfo=mediainfo,
            no_exists=no_exists,
        )
        season_episodes = (
            self._build_subtitle_season_episodes(
                mediainfo=mediainfo,
                season=season,
                episode=episode,
            )
            or season_episodes
        )

        subtitles: List[SubtitleInfo] = []
        search_count = 0
        for search_word in keywords:
            if search_count > 0:
                logger.info(f"已搜索 {search_count} 次，强制休眠 1-10 秒 ...")
                await asyncio.sleep(random.randint(1, 10))
            subtitles.extend(
                await self._async_search_subtitles_all_sites(
                    keyword=search_word,
                    sites=sites,
                )
                or []
            )
            search_count += 1
            if not self.runtime_config.search_multiple_name and subtitles:
                logger.info(f"共搜索到 {len(subtitles)} 个字幕，停止搜索")
                break

        return cast(
            List[SubtitleInfo],
            await run_in_threadpool(
                self._parse_subtitle_result,
                subtitles=subtitles,
                mediainfo=mediainfo,
                keyword=mediainfo.title,
                season_episodes=season_episodes,
                episode=episode,
                custom_words=custom_words,
            ),
        )

    async def _async_search_subtitles_for_media_stream(
        self,
        mediainfo: MediaInfo,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        sites: Optional[List[int]] = None,
        custom_words: Optional[List[str]] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        根据媒体信息渐进式搜索并精确匹配字幕结果。
        """
        mediainfo = self._copy_media_input(mediainfo)
        if not mediainfo.tmdb_id:
            meta = MetaInfo(title=mediainfo.title)
            mediainfo.title = meta.name
            mediainfo.season = cast(int, meta.begin_season)
        logger.info(f"开始渐进式精确搜索字幕，关键词：{mediainfo.title} ...")

        if not mediainfo.names:
            recognized_media = await MediaChain().async_recognize_media(
                mtype=mediainfo.type,
                **self._media_recognize_kwargs(mediainfo),
            )
            if not recognized_media:
                logger.error("媒体信息识别失败！")
                yield {"type": "error", "success": False, "message": "媒体信息识别失败"}
                return
            mediainfo = recognized_media

        no_exists = None
        if season is not None:
            media_source, media_id = resolve_media_identity(
                media=mediainfo,
                media_source=media_source,
                media_id=media_id,
            )
            no_exists = {
                build_media_key(media_source, media_id): {
                    season: NotExistMediaInfo(episodes=[episode] if episode is not None else [])
                }
            }
        season_episodes, keywords = self._prepare_params(
            mediainfo=mediainfo,
            no_exists=no_exists,
        )
        season_episodes = (
            self._build_subtitle_season_episodes(
                mediainfo=mediainfo,
                season=season,
                episode=episode,
            )
            or season_episodes
        )

        subtitles: List[SubtitleInfo] = []
        search_count = 0
        for search_word in keywords:
            if search_count > 0:
                logger.info(f"已搜索 {search_count} 次，强制休眠 1-10 秒 ...")
                await asyncio.sleep(random.randint(1, 10))

            async for event in self._async_search_subtitles_all_sites_stream(keyword=search_word, sites=sites):
                result = event.pop("items", []) or []
                subtitles.extend(result)
                yield {
                    **event,
                    "type": "append",
                    "stage": "searching",
                    "items": [subtitle.to_dict() for subtitle in result],
                    "total_items": len(subtitles),
                }

            search_count += 1
            if not self.runtime_config.search_multiple_name and subtitles:
                logger.info(f"共搜索到 {len(subtitles)} 个字幕，停止搜索")
                break

        yield {
            "type": "progress",
            "stage": "filtering",
            "value": 98,
            "text": f"正在识别匹配 {len(subtitles)} 个候选字幕 ...",
        }

        match_subtitles = await run_in_threadpool(
            self._parse_subtitle_result,
            subtitles=subtitles,
            mediainfo=mediainfo,
            keyword=mediainfo.title,
            season_episodes=season_episodes,
            episode=episode,
            custom_words=custom_words,
        )
        final_items = [subtitle.to_dict() for subtitle in match_subtitles]
        yield {
            "type": "replace",
            "stage": "filtered",
            "value": 100,
            "text": f"识别匹配完成，共 {len(match_subtitles)} 个字幕",
            "items": final_items,
            "total_items": len(match_subtitles),
        }
        yield {
            "type": "done",
            "stage": "done",
            "text": f"搜索完成，共 {len(match_subtitles)} 个字幕",
            "items": final_items,
            "total_items": len(match_subtitles),
            "subtitles": match_subtitles,
        }
